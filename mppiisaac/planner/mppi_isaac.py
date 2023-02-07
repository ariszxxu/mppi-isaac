from isaacgym import gymapi
from isaacgym import gymtorch
from dataclasses import dataclass
import torch

from mppiisaac.planner.mppi import MPPIPlanner


@dataclass
class IsaacSimConfig(object):
    dt: float = 0.05
    substeps: int = 2
    use_gpu_pipeline: bool = True
    num_client_threads: int = 0
    viewer: bool = False


def parse_isaacsim_config(cfg: IsaacSimConfig) -> gymapi.SimParams:
    sim_params = gymapi.SimParams()
    sim_params.dt = cfg.dt
    sim_params.substeps = cfg.substeps
    sim_params.use_gpu_pipeline = cfg.use_gpu_pipeline
    sim_params.num_client_threads = cfg.num_client_threads

    sim_params.up_axis = gymapi.UP_AXIS_Z
    sim_params.gravity = gymapi.Vec3(0.0, 0.0, -9.8)
    sim_params.physx.contact_offset = 0.001
    sim_params.physx.rest_offset = 0.0
    sim_params.physx.friction_offset_threshold = 0.01
    sim_params.physx.friction_correlation_distance = 0.001

    # return the configured params
    return sim_params


class MPPIisaacPlanner(MPPIPlanner):
    """
    Wrapper class that inherits from the MPPIPlanner and implements the required functions:
        dynamics, running_cost, and terminal_cost
    """

    def __init__(self, cfg):
        super().__init__(cfg.mppi)

        self.gym = gymapi.acquire_gym()
        self.sim = self.gym.create_sim(
            compute_device=0,
            graphics_device=0,
            type=gymapi.SIM_PHYSX,
            params=parse_isaacsim_config(cfg.isaacsim),
        )

        if cfg.isaacsim.viewer:
            self.viewer = self.gym.create_viewer(self.sim, gymapi.CameraProperties())
        else:
            self.viewer = None

        # Adds groundplane
        plane_params = gymapi.PlaneParams()
        plane_params.normal = gymapi.Vec3(0, 0, 1)  # z-up!
        plane_params.distance = 0
        plane_params.static_friction = 1
        plane_params.dynamic_friction = 1
        plane_params.restitution = 0
        self.gym.add_ground(self.sim, plane_params)

        # Adds robots
        asset_root = "../assets"
        asset_file = "urdf/pointRobot.urdf"
        asset_options = gymapi.AssetOptions()
        asset_options.fix_base_link = True
        asset_options.armature = 0.01
        robot_asset = self.gym.load_asset(
            self.sim, asset_root, asset_file, asset_options
        )

        spacing = 3
        num_per_row = int(cfg.mppi.num_samples**0.5)
        robot_init_pose = gymapi.Transform()
        robot_init_pose.p = gymapi.Vec3(0.0, 0.0, 0.05)
        for i in range(cfg.mppi.num_samples):
            env = self.gym.create_env(
                self.sim,
                gymapi.Vec3(-spacing, 0.0, -spacing),
                gymapi.Vec3(spacing, spacing, spacing),
                num_per_row,
            )
            robot_handle = self.gym.create_actor(
                env, robot_asset, robot_init_pose, "robot", i, 1
            )
            # Update point bot dynamics / control mode
            props = self.gym.get_asset_dof_properties(robot_asset)
            props["driveMode"].fill(gymapi.DOF_MODE_VEL)
            props["stiffness"].fill(0.0)
            props["damping"].fill(600.0)
            self.gym.set_actor_dof_properties(env, robot_handle, props)
        self.gym.prepare_sim(self.sim)

        self.root_state = gymtorch.wrap_tensor(
            self.gym.acquire_actor_root_state_tensor(self.sim)
        )
        self.dof_state = gymtorch.wrap_tensor(
            self.gym.acquire_dof_state_tensor(self.sim)
        )
        self.gym.refresh_actor_root_state_tensor(self.sim)
        self.gym.refresh_dof_state_tensor(self.sim)

        self.actor_positions = self.root_state[:, 0:2]  # [x, y]
        self.actor_velocities = self.root_state[:, 3:5]  # [vx, vy]

        # nav_goal
        self.nav_goal = torch.tensor(cfg.goal, device=cfg.mppi.device)

    def dynamics(self, state, u, t=None):
        self.gym.set_dof_velocity_target_tensor(self.sim, gymtorch.unwrap_tensor(u))
        self.gym.simulate(self.sim)
        self.gym.fetch_results(self.sim, True)
        self.gym.refresh_actor_root_state_tensor(self.sim)
        self.gym.refresh_dof_state_tensor(self.sim)

        if self.viewer is not None:
            self.gym.step_graphics(self.sim)
            self.gym.draw_viewer(self.viewer, self.sim, False)
            self.gym.sync_frame_time(self.sim)

        # return torch.cat((self.actor_positions, self.actor_velocities), axis=1), u
        return self.dof_state.view(-1, 4), u

    def running_cost(self, state, u):
        state_pos = torch.cat((state[:, 0].unsqueeze(1), state[:, 2].unsqueeze(1)), 1)

        return self._get_navigation_cost(state_pos, self.nav_goal)

    @staticmethod
    def _get_navigation_cost(pos, goal_pos):
        return torch.clamp(
            torch.linalg.norm(pos - goal_pos, axis=1) - 0.05, min=0, max=1999
        )

    def compute_action(self, q, qdot):
        state = (
            torch.tensor([q[0], qdot[0], q[1], qdot[1]])
            .type(torch.float32)
            .to(self.cfg.device)
        )  # [x, vx, y, vy]
        state = state.repeat(self.K, 1)
        print(q)
        self.gym.set_dof_state_tensor(self.sim, gymtorch.unwrap_tensor(state))

        c = self.command(
            torch.cat((self.actor_positions, self.actor_velocities), axis=1)
        )
        return c.cpu()
