#!/usr/bin/env bash
set -euo pipefail
# Run on the SERVER, not in the ROS2 environment. Requires uv and git.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOOL_ENV="$ROOT/.runtime/letools-venv"
SOURCE="$ROOT/.runtime/vendor/letools"
REV=1c417cc9c205f85cd01a5e5860646f1594738354
command -v uv >/dev/null || { echo "Install uv before running this script" >&2; exit 1; }
mkdir -p "$ROOT/.runtime/vendor"
if [[ ! -d "$SOURCE/.git" ]]; then
  git clone --filter=blob:none https://github.com/XieWeikai/letools.git "$SOURCE"
fi
if [[ -n "$(git -C "$SOURCE" status --porcelain)" ]]; then
  echo "Refusing to overwrite modified letools checkout" >&2; exit 1
fi
# Reuse an already verified checkout without requiring GitHub on every sync.
if [[ "$(git -C "$SOURCE" rev-parse HEAD)" != "$REV" ]]; then
  git -C "$SOURCE" fetch origin "$REV"
  git -C "$SOURCE" checkout --detach "$REV"
fi
git -C "$SOURCE" submodule update --init --depth 1
if [[ -n "$(git -C "$SOURCE" status --porcelain)" ]]; then
  echo "letools submodules must be clean and match the pinned commit" >&2; exit 1
fi
# Python dependencies and the native-only package index use our dedicated lock.
UV_PROJECT_ENVIRONMENT="$TOOL_ENV" uv sync \
  --project "$ROOT/environments/letools" --locked --no-dev --python "${UV_PYTHON:-3.12}"
"$TOOL_ENV/bin/letools" doctor environment
printf '\nSet server config letools_bin to: %s\n' "$TOOL_ENV/bin/letools"
printf 'Visualizer additionally requires Bun 1.3.9; setup runs on first preview.\n'
