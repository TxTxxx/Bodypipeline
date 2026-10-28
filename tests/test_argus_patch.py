"""The Argus integration patch must apply to the pinned layout and be idempotent.

The checkout itself lives outside the repository (``scripts/install_argus.sh``
clones it), so this exercises the patch on synthetic files that mirror the
anchors it edits.  A test here catches a stale anchor before a deployment resets
a checkout and fails to re-apply.
"""

import importlib.util
from pathlib import Path

import pytest

PATCH = (
    Path(__file__).resolve().parents[1]
    / "patches"
    / "argus"
    / "0001-pipeline-gateway.py"
)


@pytest.fixture(scope="module")
def patch():
    spec = importlib.util.spec_from_file_location("argus_gateway_patch", PATCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The anchors the patch edits, in the shapes the pinned checkout uses.
HARNESS = '''import json
import math
import os

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-6-astra"
PRICE_IN = 1.0e-5
OPENAI_PRICES = {"gpt-6-astra": ((1.0e-5, 1.0e-6, 5.0e-5), (2.0e-5, 2.0e-6, 7.5e-5))}
LONG_PROMPT_TOKENS = 272000


def is_openrouter_key(key: str) -> bool:
    return key.startswith("sk-or-")


def get_keys():
    def split(var):
        return [part for part in var.split(",") if part]

    return split(os.environ.get("KEY", ""))


def _post(url, body, key, timeout, attempts=6):
    return {}


def call_model(content: list, model: str, reasoning: str, api_key: str, max_tokens: int, timeout: int,
               *, attempts: int = 6, pricing_quote=None) -> dict:
    if not is_openrouter_key(api_key):
        return _call_openai(content, model, reasoning, api_key, max_tokens, timeout, attempts=attempts)
    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
    }
    return _post(OPENROUTER_URL, body, api_key, timeout, attempts=attempts)


def _call_openai(content: list, model: str, reasoning: str, api_key: str, max_tokens: int, timeout: int,
                 *, attempts: int = 6) -> dict:
    body = {"model": model.split("/", 1)[1],
            "messages": [{"role": "user", "content": content}]}
    return _post(OPENAI_URL, body, api_key, timeout, attempts=attempts)


def label_episode(ep_dir, out_path, api_key, *, dry_run=False, require_known_pricing=False):
    model = "openai/gpt-6-astra"
    if not dry_run and not is_openrouter_key(api_key) and not model.startswith('openai/'):
        raise ValueError("needs a key")
    pricing_quote = None
    if not dry_run and require_known_pricing and (not model.startswith('openai/')
            or model.split('/', 1)[-1] not in OPENAI_PRICES):
        if not is_openrouter_key(api_key):
            raise ValueError("no rates")
    tiers = OPENAI_PRICES.get(model.split('/', 1)[-1]) if model.startswith('openai/') else None
    used = {
        "provider": "openrouter" if is_openrouter_key(api_key) else "openai",
    }
    return tiers, pricing_quote, used


def _verified_final_cost(usage, model):
    if (not _complete_token_usage(usage) or not model.startswith("openai/")
            or model.split("/", 1)[-1] not in OPENAI_PRICES):
        return None, "unverified"
    return 1.0, "estimate"


def _call_and_record(content, model):
    result = {"request": {}}
    if content is not None:
        if model.startswith('openai/') and model.split('/', 1)[-1] in OPENAI_PRICES:
            result['request']['final_cost_floor_usd'] = 1.0
    return result


def main(args):
    keys = get_keys()
    if not args.dry_run and not is_openrouter_key(keys[0]) and args.model and not args.model.startswith("openai/"):
        raise SystemExit("bad")
'''

ROUTE = '''import json

ROUTE_MODEL = "openai/gpt-6-sol"


def rates():
    rates = OPENAI_PRICES[ROUTE_MODEL.split('/', 1)[-1]][1]
    return rates
'''

PRICING = '''import json


def quote(model: str, timeout: int) -> dict:
    return {}
'''

FORMATS = '''def describe(cam: dict, view: str, name: str, rig: str) -> dict:
    return {}


def read():
    ctx = {}
    set_unshown(ctx, lerobot_unshown(ctx))
    set_unshown(ctx, lerobot_unshown(ctx))
'''


def make_tree(root: Path) -> Path:
    for module, body in (
        ("label/harness.py", HARNESS),
        ("label/route.py", ROUTE),
        ("label/pricing.py", PRICING),
        ("prepare/formats.py", FORMATS),
        ("label/__init__.py", ""),
    ):
        path = root / module
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return root


def test_patch_applies_and_is_idempotent(patch, tmp_path):
    root = make_tree(tmp_path)
    assert patch.apply(root)["version"] == patch.VERSION
    once = (root / "label/harness.py").read_text()
    # A second run must not stack duplicates: install_argus.sh re-applies only
    # when a marker is missing, and a manual re-run must stay safe.
    patch.apply(root)
    assert (root / "label/harness.py").read_text() == once

    for marker in (
        patch.MARKER,
        patch.GENERIC_MODEL_MARKER,
        patch.CONFIGURED_PRICES_MARKER,
        patch.GENERIC_DRY_RUN_MARKER,
    ):
        assert once.count(marker) == 1
    # The gateway-key marker marks two places: the key reader and the dispatch
    # branch that must run before Argus's OpenRouter-key test.
    assert once.count(patch.GATEWAY_MARKER) == 2
    assert (root / "label/route.py").read_text().count(patch.GENERIC_ROUTE_MARKER) == 1
    assert (
        (root / "label/pricing.py").read_text().count(patch.GENERIC_PRICING_MARKER)
        == 1
    )
    assert (root / "prepare/formats.py").read_text().count(patch.MOUNTING_MARKER) == 1


def test_upstream_price_table_is_kept_and_extended(patch, tmp_path):
    """Argus keeps its own rates; the operator's only add to them."""

    root = make_tree(tmp_path)
    patch.apply(root)
    text = (root / "label/harness.py").read_text()
    assert 'OPENAI_PRICES = {"gpt-6-astra"' in text
    assert "OPENAI_PRICES.update(_pipeline_configured_prices())" in text
    # The declared table is read from the environment, never hard-coded, so an
    # unpriced model still fails the cap instead of being guessed at.
    assert 'os.environ.get("PIPELINE_ARGUS_PRICES")' in text


def test_a_priced_gateway_model_records_its_final_reserve(patch, tmp_path):
    """The dry run must price a gateway model, not only an ``openai/`` one."""

    root = make_tree(tmp_path)
    patch.apply(root)
    text = (root / "label/harness.py").read_text()
    assert (
        "        if ((_GENERIC or model.startswith('openai/'))\n"
        "                and model.split('/', 1)[-1] in OPENAI_PRICES):"
    ) in text
