#!/usr/bin/env bash
set -euo pipefail
# Install the Chinese / light-theme Visualizer integration patch into the
# letools toolchain that BodyPipeline already provisioned.
#
# Usage: bash scripts/install_visualizer_zh_patch.sh
# Requires: bash scripts/install_letools.sh has been run at least once.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOOL_ENV="$ROOT/.runtime/letools-venv"
PATCH="$ROOT/patches/letools-visualizer/0002-zh-cn-light-theme.patch"

[[ -x "$TOOL_ENV/bin/python" ]] || {
  echo "letools environment missing; run bash scripts/install_letools.sh first" >&2
  exit 1
}
[[ -f "$PATCH" ]] || { echo "patch not found: $PATCH" >&2; exit 1; }

# Ask letools where it looks for Visualizer integration patches so this works
# for both the git-checkout layout and the packaged-wheel layout.
TARGET="$("$TOOL_ENV/bin/python" - <<'PY'
from pathlib import Path
from letools.external import visualizer_patches

patches = visualizer_patches()
if patches:
    print(patches[0].parent)
PY
)"

if [[ -z "${TARGET:-}" ]]; then
  echo "could not resolve the letools Visualizer patch directory" >&2
  exit 1
fi
if [[ ! -d "$TARGET" ]]; then
  echo "resolved patch directory does not exist: $TARGET" >&2
  exit 1
fi

install -m 0644 "$PATCH" "$TARGET/0002-zh-cn-light-theme.patch"
echo "installed patch into: $TARGET"

# The Visualizer is prepared into a cache directory keyed by a source
# fingerprint that includes the patch set, so a stale cache is rebuilt on the
# next preview. Report the cache locations we know about.
for cache in "$ROOT/.runtime/server/viewer-cache" "$ROOT/.runtime/viewer-cache"; do
  [[ -d "$cache" ]] && echo "visualizer cache (rebuilt on demand): $cache"
done

echo "Restart the Server, then open a preview to rebuild the Visualizer."
