#!/usr/bin/env bash
# Re-prepare an episode slice with publisher-declared camera mounting.
#
# Argus infers a camera's mounting from its name. When the dataset says a camera
# sits on a wrist while its name only carries a side, that fact has to be handed
# to the reader explicitly. The pipeline patch reads it from
# PIPELINE_ARGUS_CAMERA_MOUNTING as JSON: {view: mounting}.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="${ARGUS_ROOT:-$REPO/.runtime/argus}"
DATASET="${1:?usage: argus_prepare_mounted.sh DATASET [OUT]}"
OUT="${2:-$ROOT/mounted/episodes}"
MOUNTING="${PIPELINE_ARGUS_CAMERA_MOUNTING:-}"
# ffmpeg may live beside the Argus checkout rather than on PATH.
export PATH="$ROOT/tools/bin:$PATH"
export PYTHONPATH="$ROOT/src"
export PIPELINE_ARGUS_CAMERA_MOUNTING="$MOUNTING"

exec "$ROOT/src/.venv/bin/python" -m prepare lerobot prepare \
  --root "$DATASET" --rig "${ARGUS_RIG:-teleop_arms}" --out "$OUT"
