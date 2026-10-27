#!/usr/bin/env bash
set -euo pipefail
# Run on the SERVER, not in the ROS2 environment. Requires uv and git.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOOL_ENV="$ROOT/.runtime/letools-venv"
SOURCE="$ROOT/.runtime/vendor/letools"
REV=1c417cc9c205f85cd01a5e5860646f1594738354
PROVIDERS_SOURCE="$ROOT/.runtime/vendor/data-providers"
PROVIDERS_REV=487418f5270a77722820d104015cda907fa6facd
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
if [[ ! -d "$PROVIDERS_SOURCE/.git" ]]; then
  git clone --filter=blob:none https://github.com/XieWeikai/data-providers.git "$PROVIDERS_SOURCE"
fi
if [[ -n "$(git -C "$PROVIDERS_SOURCE" status --porcelain)" ]]; then
  echo "Refusing to overwrite modified data-providers checkout" >&2; exit 1
fi
if [[ "$(git -C "$PROVIDERS_SOURCE" rev-parse HEAD)" != "$PROVIDERS_REV" ]]; then
  git -C "$PROVIDERS_SOURCE" fetch origin "$PROVIDERS_REV"
  git -C "$PROVIDERS_SOURCE" checkout --detach "$PROVIDERS_REV"
fi
if [[ ! -f "$PROVIDERS_SOURCE/tianji_provider/pyproject.toml" ]]; then
  echo "Pinned data-providers checkout does not contain tianji_provider" >&2
  exit 1
fi
# Python dependencies and the native-only package index use our dedicated lock.
UV_PROJECT_ENVIRONMENT="$TOOL_ENV" uv sync \
  --project "$ROOT/environments/letools" --locked --no-dev --python "${UV_PYTHON:-3.12}"
uv pip install --python "$TOOL_ENV/bin/python" \
  --editable "$PROVIDERS_SOURCE/tianji_provider"
"$TOOL_ENV/bin/letools" doctor environment
"$TOOL_ENV/bin/letools" providers list
printf '\nSet server config letools_bin to: %s\n' "$TOOL_ENV/bin/letools"
printf 'Set server config tianji_provider_bin to: %s\n' "$TOOL_ENV/bin/tianji-provider"
printf 'Visualizer additionally requires Bun 1.3.9; setup runs on first preview.\n'
