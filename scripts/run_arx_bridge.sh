#!/usr/bin/env bash
# Attach to existing observation topics. Never launch robot/camera controllers.
set -euo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
arx_root="${ARX_CAPTURE_REPOSITORY:-/opt/ROS2_AC-one_Play}"
arx_python="${ARX_CAPTURE_PYTHON:-$arx_root/tools/.collect_venv/bin/python}"
config="${1:-configs/client.arx-x5.json}"
export PYTHONNOUSERSITE=1
export RMW_IMPLEMENTATION="${RMW_IMPLEMENTATION:-rmw_cyclonedds_cpp}"
# ROS setup scripts are not safe with nounset.
set +u
source "/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash"
set -u
cd "$repo_dir"
export PYTHONPATH="$repo_dir${PYTHONPATH:+:$PYTHONPATH}"
exec "$arx_python" -m pipeline.cli bridge --config "$config" --host 127.0.0.1 --port "${ARX_BRIDGE_PORT:-8091}"
