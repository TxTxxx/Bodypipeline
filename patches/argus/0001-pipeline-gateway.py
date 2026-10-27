"""BodyPipeline integration patch for the pinned Argus harness.

Applied to the pinned Argus checkout by ``scripts/install_argus.sh``. Kept as a
separate module rather than editing upstream files so the patch is reviewable
and version-checked.

What it changes:

1. ``label.harness.OPENROUTER_URL`` / ``OPENAI_URL`` can be pointed at any
   Chat Completions compatible gateway through ``PIPELINE_ARGUS_BASE_URL``.
2. ``label.harness._post`` uses ``PIPELINE_ARGUS_API_KEY`` when the gateway
   wants a single key instead of the upstream per-provider keys.
3. Request bodies drop provider-specific fields that a generic gateway rejects
   (``reasoning``, ``usage.include``, ``provider.max_price``) when
   ``PIPELINE_ARGUS_GENERIC_GATEWAY=1``.
4. ``label.pricing.quote`` is bypassed when the gateway has no pricing
   endpoint; the caller then relies on pipeline-side budget accounting.

The patch is deliberately conservative: with none of the environment variables
set, Argus behaves exactly as upstream.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

VERSION = 2
MARKER = "# pipeline-argus-integration:v1"


def _insert_after(text: str, anchor: str, addition: str) -> str:
    index = text.index(anchor) + len(anchor)
    return text[:index] + addition + text[index:]


def patch_harness(path: Path) -> None:
    text = path.read_text()
    if MARKER in text:
        return

    anchor = 'OPENAI_URL = "https://api.openai.com/v1/chat/completions"\n'
    if anchor not in text:
        raise RuntimeError("unexpected Argus harness layout: OPENAI_URL anchor missing")
    text = text.replace(
        anchor,
        anchor
        + MARKER
        + "\n"
        + "# BodyPipeline: allow any Chat Completions compatible gateway.\n"
        + '_GATEWAY_BASE = (os.environ.get("PIPELINE_ARGUS_BASE_URL") or "").rstrip("/")\n'
        + 'if _GATEWAY_BASE:\n'
        + '    OPENROUTER_URL = _GATEWAY_BASE + "/chat/completions"\n'
        + '    OPENAI_URL = _GATEWAY_BASE + "/chat/completions"\n'
        + '_GENERIC = os.environ.get("PIPELINE_ARGUS_GENERIC_GATEWAY") == "1"\n',
        1,
    )

    # A single gateway key replaces the upstream per-provider key selection.
    anchor = "def is_openrouter_key(key: str) -> bool:"
    if anchor not in text:
        raise RuntimeError("unexpected Argus harness layout: is_openrouter_key missing")
    text = text.replace(
        anchor,
        'def _gateway_key():\n'
        '    return os.environ.get("PIPELINE_ARGUS_API_KEY") or ""\n'
        "\n\n"
        + anchor,
        1,
    )

    # Drop fields a generic gateway rejects, and route through the gateway key.
    anchor = '    body = {\n        "model": model,\n'
    if anchor not in text:
        raise RuntimeError("unexpected Argus harness layout: OpenRouter body missing")
    text = text.replace(
        anchor,
        '    if _GENERIC:\n'
        '        body = {\n'
        '            "model": model,\n'
        '            "messages": [{"role": "user", "content": content}],\n'
        '            "max_tokens": max_tokens,\n'
        '            "response_format": {"type": "json_object"},\n'
        '        }\n'
        '        return _post(OPENROUTER_URL, body, _gateway_key() or api_key, timeout, attempts=attempts)\n'
        + anchor,
        1,
    )

    anchor = '    body = {"model": model.split("/", 1)[1],\n'
    if anchor in text:
        text = text.replace(
            anchor,
            '    if _GENERIC:\n'
            '        body = {\n'
            '            "model": model,\n'
            '            "messages": [{"role": "user", "content": content}],\n'
            '            "max_tokens": max_tokens,\n'
            '            "response_format": {"type": "json_object"},\n'
            '        }\n'
            '        resp = _post(OPENAI_URL, body, _gateway_key() or api_key, timeout, attempts=attempts)\n'
            '        resp.setdefault("provider", "gateway")\n'
            '        return resp\n'
            + anchor,
            1,
        )

    path.write_text(text)


def patch_pricing(path: Path) -> None:
    """Skip the OpenRouter catalogue lookup when a generic gateway is configured."""

    text = path.read_text()
    if MARKER in text:
        return
    anchor = "def quote(model: str, timeout: int) -> dict:\n"
    if anchor not in text:
        raise RuntimeError("unexpected Argus pricing layout: quote() missing")
    text = text.replace(
        anchor,
        anchor
        + '    if os.environ.get("PIPELINE_ARGUS_GENERIC_GATEWAY") == "1":\n'
        + '        raise ValueError("generic gateway: no catalogue pricing, use pipeline budget")\n',
        1,
    )
    if "\nimport os\n" not in text:
        text = text.replace("import json\n", "import json\nimport os\n", 1)
    path.write_text(text)


def patch_label_module(root: Path) -> None:
    """Expose the patch through the package the CLI already imports."""

    path = root / "label" / "__init__.py"
    text = path.read_text() if path.exists() else ""
    if MARKER in text:
        return
    path.write_text(text + "\n" + MARKER + "\n")


MOUNTING_MARKER = "# pipeline-argus-mounting:v2"

MOUNTING_BLOCK = '''
# pipeline-argus-mounting:v2
# BodyPipeline: let the dataset publisher declare where each camera is mounted.
# Argus otherwise infers mounting from the camera's name alone, so a camera
# called "left" is treated as unspecified even when the dataset metadata (or the
# robot profile) states it is mounted on the wrist. The declaration arrives in
# PIPELINE_ARGUS_CAMERA_MOUNTING as {"left_wrist": "wrist_left", ...}. Camera
# names are presentation slots; mounting is a separate, explicitly stated fact.
MOUNTING_WORDS = {
    "wrist": "actor", "wrist_left": "actor", "wrist_right": "actor",
    "gripper": "actor", "hand": "actor", "actor": "actor",
    "head": "head", "head_left": "head", "head_right": "head",
    "ego": "head", "egocentric": "head",
    "fixed": "fixed", "scene": "fixed",
}


def _pipeline_mounting() -> dict:
    raw = os.environ.get("PIPELINE_ARGUS_CAMERA_MOUNTING") or ""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _mounting_camera(cameras: dict, view: str):
    cam = cameras.get(view)
    if isinstance(cam, dict):
        return cam
    # Profiles name their own cameras (left_wrist, head_left, egocentric); Argus
    # names presentation slots (left, right, exo, extra1). Match the declared id
    # against the recorded camera key, whole or as a name segment, after the same
    # normalisation Argus applies (observation.images.<name> -> <name>).
    wanted = str(view).lower()
    if not wanted:
        return None
    for candidate in cameras.values():
        if not isinstance(candidate, dict):
            continue
        base = str(candidate.get("key") or candidate.get("name") or "")
        base = base.rsplit("/", 1)[-1]
        base = re.sub(r"^observation\.images?\.", "", base)
        base = re.sub(r"[^A-Za-z0-9_]+", "_", base).strip("_").lower()
        if base and (
            base == wanted
            or base.startswith(wanted + "_")
            or base.endswith("_" + wanted)
            or f"_{wanted}_" in base
        ):
            return candidate
    return None


def apply_declared_mounting(ep: dict) -> None:
    """Overwrite presentation-only camera entries with the publisher's stated mounting."""

    declared = _pipeline_mounting()
    if not declared:
        return
    cameras = ep.get("cameras") or {}
    for view, spec in declared.items():
        cam = _mounting_camera(cameras, view)
        if not isinstance(cam, dict):
            continue
        mounting = MOUNTING_WORDS.get(str(spec).lower())
        if mounting is None:
            continue
        side = cam.get("key") or cam.get("name") or view
        cam["mounting"] = mounting
        cam["role_provenance"] = "adapter"
        cam["desc"] = (
            f"recorded camera {side}; the dataset declares it is mounted as {spec} "
            "(publisher metadata, not inferred from its name)"
        )
'''


def patch_mounting(root: Path) -> None:
    """Let a publisher-declared camera mounting reach the checks and the prompt."""

    path = root / "prepare" / "formats.py"
    text = path.read_text()
    if MOUNTING_MARKER in text:
        return

    anchor = "def describe(cam: dict, view: str, name: str, rig: str) -> dict:"
    if anchor not in text:
        raise RuntimeError("unexpected Argus layout: describe() missing")
    text = text.replace(anchor, MOUNTING_BLOCK.strip() + "\n\n\n" + anchor, 1)

    # Both LeRobot paths build their context and then call set_unshown(ctx, ...).
    # Injecting just before that call covers every episode the reader emits, and
    # keeps the declared mounting visible to everything downstream.
    anchor = "set_unshown(ctx, lerobot_unshown("
    count = text.count(anchor)
    if count != 2:
        raise RuntimeError(
            f"unexpected Argus layout: expected 2 set_unshown(ctx, ...) calls, found {count}"
        )
    out = []
    for line in text.splitlines(keepends=True):
        if anchor in line:
            indent = line[: len(line) - len(line.lstrip())]
            out.append(f"{indent}apply_declared_mounting(ctx)\n")
        out.append(line)
    path.write_text("".join(out))


def apply(root: Path) -> dict:
    patch_harness(root / "label" / "harness.py")
    patch_pricing(root / "label" / "pricing.py")
    patch_label_module(root)
    patch_mounting(root)
    return {"version": VERSION, "root": str(root)}


if __name__ == "__main__":
    import sys

    target = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    print(json.dumps(apply(target), indent=2))
