"""Run the installed tool's original RosOperator with command-only inputs.

This file is executed by the robot's existing data Python environment. ROS and
the original module are imported only inside main(), never by the Client API.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from types import SimpleNamespace


def tool_arguments(dataset, fps):
    values = {"datasetDir": str(dataset), "publishIndex": -1, "publish_rate": fps}
    for stream in (
        "camera_color",
        "camera_depth",
        "camera_point_cloud",
        "arm_end_pose",
        "localization_pose",
        "gripper_encoder",
        "imu_9axis",
        "lidar_point_cloud",
        "robot_base_vel",
        "lift_motor",
    ):
        values[f"{stream}_names"] = []
        values[f"{stream}_topics"] = []
    values["arm_end_pose_orients"] = []
    values["arm_joint_state_names"] = ["masterLeft", "masterRight"]
    values["arm_joint_state_topics"] = ["/joint_left_states", "/joint_right_states"]
    return SimpleNamespace(**values)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--fps", required=True, type=float)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location(
        "installed_aloha_data_publish", args.tool
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.rclpy.init()
    node = None
    try:
        node = module.RosOperator(tool_arguments(args.dataset, args.fps))
        module.rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if module.rclpy.ok():
            module.rclpy.shutdown()
        if node is not None:
            node.destroy_node()


if __name__ == "__main__":
    main()
