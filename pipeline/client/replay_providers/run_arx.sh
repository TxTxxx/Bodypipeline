#!/usr/bin/env bash
set -euo pipefail
arx_python="$1"
shift
# Keep the installed ROS tool environment's NumPy/h5py ABI intact.
export PYTHONNOUSERSITE=1
export PYTHONUNBUFFERED=1
exec "$arx_python" -u "$(dirname -- "${BASH_SOURCE[0]}")/arx_runner.py" "$@"
