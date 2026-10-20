#!/usr/bin/env bash
# Install the pinned Argus analysis toolchain for BodyPipeline.
#
# Run on the SERVER (the host that runs `pipeline.cli server`). Creates a
# separate Python 3.11 environment, because Argus pins 3.11 while letools pins
# 3.12; mixing them would break one of the two.
#
# The checkout is a real git repository at the pinned commit with the
# integration patch committed, because Argus refuses paid runs from a working
# tree with uncommitted changes.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARGUS_ROOT="${ARGUS_ROOT:-$ROOT/.runtime/argus}"
SRC="$ARGUS_ROOT/src"
REPO="https://github.com/Pantheon-Industries-Inc/argus"
REV="e1d2f678ffd1cbb51a8cb7bf949f34913bce4281"
PATCH="$ROOT/patches/argus/0001-pipeline-gateway.py"

command -v uv >/dev/null || { echo "Install uv before running this script" >&2; exit 1; }
command -v ffmpeg >/dev/null || {
  echo "ffmpeg 5.1+ is required; install it or put a static build on PATH" >&2
  exit 1
}
command -v ffprobe >/dev/null || {
  echo "ffprobe is required alongside ffmpeg" >&2
  exit 1
}
[[ -f "$PATCH" ]] || { echo "patch not found: $PATCH" >&2; exit 1; }

mkdir -p "$ARGUS_ROOT"
if [[ ! -d "$SRC/.git" ]]; then
  git clone --filter=blob:none "$REPO" "$SRC"
fi
git -C "$SRC" fetch origin "$REV" --depth 1 || true
git -C "$SRC" checkout --detach "$REV"
git -C "$SRC" submodule update --init --depth 1 || true

# Apply the integration patch and commit it, so `git status` is clean and Argus
# will accept a paid run.
if ! grep -q "pipeline-argus-integration:v1" "$SRC/label/harness.py"; then
  python3 "$PATCH" "$SRC"
  git -C "$SRC" -c user.name=bodypipeline -c user.email=bodypipeline@local \
    commit -am "integration: BodyPipeline gateway, mounting and pricing hooks"
fi

UV_PROJECT_ENVIRONMENT="$ARGUS_ROOT/venv" uv sync \
  --project "$SRC" --frozen --python "${ARGUS_PYTHON:-3.11}"

"$ARGUS_ROOT/venv/bin/python" - <<'PY'
import av, numpy, pandas, pyarrow, scipy  # noqa: F401
print("argus imports ok")
PY

cat <<EOF

Argus installed.

  argus_src     = $SRC
  argus_python  = $ARGUS_ROOT/venv/bin/python

Add these to the Server config:

  "argus_src": "$SRC",
  "argus_python": "$ARGUS_ROOT/venv/bin/python",
  "argus_model": "openai/gpt-6-astra",
  "argus_per_episode_budget_usd": 1.0,
  "argus_max_tokens": 12000

Paid labelling needs PIPELINE_ARGUS_API_KEY in the Server environment.
EOF
