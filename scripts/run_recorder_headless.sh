#!/usr/bin/env bash
set -euo pipefail
# This script only starts the recording service, not camera/arm/teleop drivers.
# Run manually after establishing exclusive ownership; never auto-run during installation.
# ROS setup scripts inspect optional variables that may be unset in systemd.
set +u
source /opt/ros/humble/setup.bash
source /path/to/robot_ws/install/setup.bash
set -u
# The installed recorder requires an explicit nonempty string-array override.
# This sentinel matches no real stream; all configured joints remain mandatory.
exec ros2 run data_tools data_tools_dataCapture --ros-args \
  --params-file /path/to/robot_ws/src/data_tools/config/aloha_data_params.yaml \
  -p "optionalArmJointStateTopics:=['/unused_optional_topic']" \
  -p useService:=true -p hz:=15 -p timeout:=2 -p cropTime:=0.0 \
  -p 'instructions:=\[null\]'
