#!/usr/bin/env bash
# Load the same ROS/data environment as the installed ALOHA desktop launcher.
set -eo pipefail
robot_home="$1"
shift
source /opt/ros/humble/setup.bash
source "$robot_home/agilex_ws/install/setup.bash"
source "$robot_home/piper_ros/install/setup.bash"
source "$robot_home/data_ros/install/setup.bash"
source "$robot_home/miniforge3/etc/profile.d/conda.sh"
conda activate data
export PYTHONUNBUFFERED=1
exec python -u "$(dirname -- "${BASH_SOURCE[0]}")/agilex_runner.py" "$@"
