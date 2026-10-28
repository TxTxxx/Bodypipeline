"""BodyPipeline integration patch for the pinned Argus harness.

Applied to the pinned Argus checkout by ``scripts/install_argus.sh``. Kept as a
separate module rather than editing upstream files so the patch is reviewable
and version-checked.

What it changes:

1. ``label.harness.OPENROUTER_URL`` / ``OPENAI_URL`` can be pointed at any
   Chat Completions compatible gateway through ``PIPELINE_ARGUS_BASE_URL``.
2. ``PIPELINE_ARGUS_API_KEY`` authenticates a run directly, as an alternative to
   ``OPENROUTER_API_KEYS`` / ``OPENAI_API_KEY``. With
   ``PIPELINE_ARGUS_BASE_URL`` set it is the single key for that gateway.
3. Request bodies drop provider-specific fields that a generic gateway rejects
   (``reasoning``, ``usage.include``, ``provider.max_price``) when
   ``PIPELINE_ARGUS_GENERIC_GATEWAY=1``.
4. ``label.pricing.quote`` is bypassed when the gateway has no pricing
   endpoint; the caller then relies on pipeline-side budget accounting.
5. A generic gateway publishes its own model names, so labels no longer require
   an ``openai/`` prefix or an OpenRouter-shaped key.  A spend cap falls back to
   the OpenAI list price when the gateway model's base name is known, and the
   recorded provider is ``gateway``.
6. ``label.route`` can route with the gateway's own model through
   ``PIPELINE_ARGUS_ROUTE_MODEL`` instead of Argus's OpenRouter-only
   ``openai/gpt-6-sol``.

The patch is deliberately conservative: with none of the environment variables
set, Argus behaves exactly as upstream.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

VERSION = 5
MARKER = "# pipeline-argus-integration:v1"
GATEWAY_MARKER = "# pipeline-argus-gateway-key:v3"
GENERIC_MODEL_MARKER = "# pipeline-argus-generic-models:v4"
GENERIC_ROUTE_MARKER = "# pipeline-argus-generic-route:v5"
GENERIC_PRICING_MARKER = "# pipeline-argus-generic-pricing:v5"


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


GATEWAY_BLOCK = '''    if _GENERIC:
        body = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        return _post(OPENROUTER_URL, body, _gateway_key() or api_key, timeout, attempts=attempts)
'''


def patch_gateway_key(path: Path) -> None:
    """Let PIPELINE_ARGUS_API_KEY authenticate a run without upstream keys.

    Upstream Argus reads only OPENROUTER_API_KEYS / OPENAI_API_KEY, so the key
    the Server already passes for paid labelling was ignored.  A gateway key is
    not necessarily shaped like an OpenRouter key either, so a generic gateway
    must be selected before the key-type branch, not after it.
    """

    text = path.read_text()
    if GATEWAY_MARKER in text:
        return

    anchor = "    def split(var):\n"
    if text.count(anchor) != 1:
        raise RuntimeError("unexpected Argus layout: get_keys() split() missing")
    text = text.replace(
        anchor,
        "    " + GATEWAY_MARKER + "\n"
        + "    if _gateway_key():\n"
        + "        return [_gateway_key()]\n"
        + anchor,
        1,
    )

    # The same block is added by patch_harness() when it first applies the
    # integration, inside the OpenRouter branch.  A gateway key may not look
    # like an OpenRouter key, so move the generic branch before that check.
    generic = (
        '    if _GENERIC:\n'
        '        body = {\n'
        '            "model": model,\n'
        '            "messages": [{"role": "user", "content": content}],\n'
        '            "max_tokens": max_tokens,\n'
        '            "response_format": {"type": "json_object"},\n'
        '        }\n'
        '        return _post(OPENROUTER_URL, body, _gateway_key() or api_key, timeout, attempts=attempts)\n'
    )
    if text.count(generic) != 1:
        raise RuntimeError("unexpected Argus layout: generic call block missing")
    text = text.replace(generic, "", 1)

    signature = (
        "def call_model(content: list, model: str, reasoning: str, api_key: str, max_tokens: int, timeout: int,\n"
        "               *, attempts: int = 6, pricing_quote=None) -> dict:\n"
    )
    if text.count(signature) != 1:
        raise RuntimeError("unexpected Argus layout: call_model() signature missing")
    text = text.replace(
        signature,
        signature + "    " + GATEWAY_MARKER + "\n" + GATEWAY_BLOCK,
        1,
    )
    path.write_text(text)


def patch_generic_models(path: Path) -> None:
    """Let a generic gateway label with the model names it publishes.

    Upstream Argus decides where a call goes from the key shape: only an
    OpenRouter-shaped key may label a model that is not named ``openai/...``.
    A gateway publishes its own catalogue (``gpt-6.1-sol``, not
    ``openai/gpt-6.1-sol``), so every such check has to respect
    ``PIPELINE_ARGUS_GENERIC_GATEWAY`` first.  Pricing and provider telemetry
    follow the same rule: base-name rates give a spend cap something to bound,
    and the recorded provider is the gateway rather than OpenAI.
    """

    text = path.read_text()
    if GENERIC_MODEL_MARKER in text:
        return

    replacements = [
        (
            "    if not dry_run and not is_openrouter_key(api_key) and not model.startswith('openai/'):",
            "    " + GENERIC_MODEL_MARKER + "\n"
            "    if (not dry_run and not _GENERIC and not is_openrouter_key(api_key)\n"
            "            and not model.startswith('openai/')):",
        ),
        (
            "    if not dry_run and require_known_pricing and (not model.startswith('openai/')\n"
            "            or model.split('/', 1)[-1] not in OPENAI_PRICES):",
            "    if (not dry_run and require_known_pricing and not _GENERIC\n"
            "            and (not model.startswith('openai/')\n"
            "                 or model.split('/', 1)[-1] not in OPENAI_PRICES)):",
        ),
        (
            "    tiers = OPENAI_PRICES.get(model.split('/', 1)[-1]) if model.startswith('openai/') else None",
            "    tiers = (OPENAI_PRICES.get(model.split('/', 1)[-1])\n"
            "             if (model.startswith('openai/') or _GENERIC) else None)",
        ),
        (
            '        "provider": "openrouter" if is_openrouter_key(api_key) else "openai",',
            '        "provider": "gateway" if _GENERIC else ("openrouter" if is_openrouter_key(api_key) else "openai"),',
        ),
        (
            "    if (not _complete_token_usage(usage) or not model.startswith(\"openai/\")\n"
            "            or model.split(\"/\", 1)[-1] not in OPENAI_PRICES):",
            "    if (not _complete_token_usage(usage)\n"
            "            or not (_GENERIC or model.startswith(\"openai/\"))\n"
            "            or model.split(\"/\", 1)[-1] not in OPENAI_PRICES):",
        ),
        (
            '    if not args.dry_run and not is_openrouter_key(keys[0]) and args.model and not args.model.startswith("openai/"):',
            "    if (not args.dry_run and not _GENERIC and not is_openrouter_key(keys[0])\n"
            '            and args.model and not args.model.startswith("openai/")):',
        ),
    ]
    for original, replacement in replacements:
        if text.count(original) != 1:
            raise RuntimeError(
                "unexpected Argus layout: generic gateway anchor missing: "
                + original.strip().splitlines()[0][:80]
            )
        text = text.replace(original, replacement, 1)
    path.write_text(text)


def patch_route(path: Path) -> None:
    """Route resolution choices through the gateway's own model.

    Upstream Argus always sends its routing question to ``openai/gpt-6-sol``,
    which exists on OpenRouter but not necessarily on a private gateway.  A
    generic gateway lets the Server name the routing model explicitly; the
    default is unchanged when no gateway is in use.
    """

    text = path.read_text()
    if GENERIC_ROUTE_MARKER in text:
        return

    if "\nimport os\n" not in text:
        text = text.replace("import json\n", "import json\nimport os\n", 1)
    anchor = 'ROUTE_MODEL = "openai/gpt-6-sol"\n'
    if text.count(anchor) != 1:
        raise RuntimeError("unexpected Argus layout: ROUTE_MODEL missing")
    text = text.replace(
        anchor,
        GENERIC_ROUTE_MARKER + "\n"
        + 'ROUTE_MODEL = (\n'
        + '    (os.environ.get("PIPELINE_ARGUS_ROUTE_MODEL") or "").strip()\n'
        + '    or "openai/gpt-6-sol"\n'
        + ')\n',
        1,
    )

    anchor = "    rates = OPENAI_PRICES[ROUTE_MODEL.split('/', 1)[-1]][1]\n"
    if text.count(anchor) != 1:
        raise RuntimeError("unexpected Argus layout: routing rates missing")
    text = text.replace(
        anchor,
        "    rates = OPENAI_PRICES.get(\n"
        "        ROUTE_MODEL.split('/', 1)[-1], OPENAI_PRICES['gpt-6-astra'])[1]\n",
        1,
    )
    path.write_text(text)


def patch_pricing(path: Path) -> None:
    """Skip the OpenRouter catalogue lookup when a generic gateway is configured.

    Earlier patch versions inserted this guard without a marker of its own, so a
    repeated ``apply()`` stacked duplicates.  Normalise every copy to exactly
    one marked guard.
    """

    text = path.read_text()
    guard = (
        '    if os.environ.get("PIPELINE_ARGUS_GENERIC_GATEWAY") == "1":\n'
        '        raise ValueError("generic gateway: no catalogue pricing, use pipeline budget")\n'
    )
    anchor = "def quote(model: str, timeout: int) -> dict:\n"
    if anchor not in text:
        raise RuntimeError("unexpected Argus pricing layout: quote() missing")
    text = text.replace(guard, "")
    text = text.replace("    " + GENERIC_PRICING_MARKER + "\n", "")
    text = text.replace(
        anchor,
        anchor + "    " + GENERIC_PRICING_MARKER + "\n" + guard,
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
    patch_gateway_key(root / "label" / "harness.py")
    patch_generic_models(root / "label" / "harness.py")
    patch_route(root / "label" / "route.py")
    patch_pricing(root / "label" / "pricing.py")
    patch_label_module(root)
    patch_mounting(root)
    return {"version": VERSION, "root": str(root)}


if __name__ == "__main__":
    import sys

    target = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    print(json.dumps(apply(target), indent=2))
