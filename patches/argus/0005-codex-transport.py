"""Local Codex model transport, applied after 0001–0004 to pinned Argus."""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

MARKER = "# pipeline-argus-codex:v1"


def start(text, name, code):
    node = next(
        n
        for n in ast.parse(text).body
        if isinstance(n, ast.FunctionDef) and n.name == name
    )
    first = node.body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        first = node.body[1]
    lines = text.splitlines(keepends=True)
    lines.insert(first.lineno - 1, code)
    return "".join(lines)


def once(text, before, after):
    if text.count(before) != 1:
        raise RuntimeError(f"pinned Argus Codex anchor changed: {before[:90]}")
    return text.replace(before, after)


def apply(root):
    source = Path(__file__).resolve().parents[2] / "pipeline/server"
    harness = root / "label/harness.py"
    text = harness.read_text()
    if MARKER not in text:
        text = once(
            text,
            'DETAIL = "high"',
            f'{MARKER}\nfrom label import pipeline_codex as codex\nDETAIL = "high"',
        )
        for name, code in {
            "call_model": "    if codex.enabled():\n        return codex.call_model(content, model, reasoning, timeout)\n",
            "get_keys": "    if codex.enabled():\n        return ['local-codex-cli']  # transport identity, never a network key\n",
            "_verified_final_cost": "    if codex.enabled():\n        return None, 'unknown_codex_cli'\n",
            "final_cost_bound": "    if codex.enabled():\n        if require_known_pricing:\n            raise ValueError('Codex CLI cannot enforce a monetary cap')\n        return 0.0  # no monetary reservation; actual CLI cost remains UNKNOWN\n",
            "auto_resend": "    if codex.enabled():\n        error = record.get('codex_error')\n        if error and not error.get('retryable'):\n            return None\n",
        }.items():
            text = start(text, name, code)
        text = text.replace(
            "not dry_run and not _GENERIC and",
            "not dry_run and not codex.enabled() and not _GENERIC and",
        )
        text = text.replace(
            "not args.dry_run and not _GENERIC and",
            "not args.dry_run and not codex.enabled() and not _GENERIC and",
        )
        text = once(
            text,
            '    return {"version": 2, "source_proof": source_proof(ctx, ep_dir), "settings": settings}',
            '    if codex.enabled():\n        settings["bodypipeline_model_transport"] = codex.identity()\n'
            '    return {"version": 2, "source_proof": source_proof(ctx, ep_dir), "settings": settings}',
        )
        text = once(
            text,
            '        failed = dict(claim, no_reply=f"{type(error).__name__}: {error}",\n                      final_dispatch_outcome="unverified")',
            '        failed = dict(claim, no_reply=f"{type(error).__name__}: {error}",\n                      final_dispatch_outcome="unverified")\n'
            '        if isinstance(error, codex.CLIError):\n            failed["codex_error"] = {"kind": error.kind, "retryable": error.retryable}',
        )
        text = once(
            text,
            "    labels, parse_ok = read_reply(text, fields)\n    failure = reply_error(resp)\n",
            "    labels, parse_ok = read_reply(text, fields)\n    codex.acknowledge(resp, parse_ok)\n    failure = reply_error(resp)\n",
        )
        text = once(
            text,
            '"provider": "gateway" if _GENERIC else ("openrouter" if is_openrouter_key(api_key) else "openai"),',
            '"provider": "codex_cli" if codex.enabled() else ("gateway" if _GENERIC else ("openrouter" if is_openrouter_key(api_key) else "openai")),',
        )
        harness.write_text(text)
    text = harness.read_text()
    if "codex delivery is known independently of billing" not in text:
        text = once(
            text,
            "        if any(record.get('final_dispatch_outcome') in ('claimed', 'unverified') for _, record in records):\n",
            "        # codex delivery is known independently of billing\n"
            "        if codex.enabled() and all((record.get('response') or {}).get('provider') == 'codex_cli'\n"
            "                and annotation_complete(record) for _, record in records):\n"
            "            return True  # source/request attestation already matched; cost stays unknown\n"
            "        if any(record.get('final_dispatch_outcome') in ('claimed', 'unverified') for _, record in records):\n",
        )
        harness.write_text(text)
    route = root / "label/route.py"
    text = route.read_text()
    if MARKER not in text:
        # Template-only CLI mode uses the existing wide grid builder. No extra
        # text routing call, no old uploader timeline supplied as an answer.
        text = start(
            text,
            "route_width",
            f"""    {MARKER}
    from label import pipeline_codex as codex
    if codex.enabled():
        ep = me.load(ep_dir)
        widths = me.ROUTE_WIDTHS.get(me.rig(ep))
        return (widths[-1] if widths else None), {{'routed': False, 'method': 'codex template wide evidence (no model routing call)', 'cost_usd': 0.0}}
""",
        )
        route.write_text(text)
    else:
        text = text.replace(
            "'method': 'codex template wide evidence', 'cost_usd': None",
            "'method': 'codex template wide evidence (no model routing call)', 'cost_usd': 0.0",
        )
        route.write_text(text)
    shutil.copyfile(source / "codex_transport.py", root / "label/pipeline_codex.py")
    policy = root / "label/policy.py"
    text = policy.read_text()
    if "codex part request budget" not in text:
        text = once(
            text,
            "    if sample_fps is None and saved.get('model') == model and saved.get('part_max_s') is not None:\n",
            "    # codex part request budget\n"
            "    from label import pipeline_codex as codex\n"
            "    if codex.enabled():\n        maximum = codex.part_limit(ctx, period, maximum)\n"
            "    if sample_fps is None and saved.get('model') == model and saved.get('part_max_s') is not None:\n",
        )
        policy.write_text(text)
    shutil.copyfile(
        source / "argus_template_runtime.py", root / "label/pipeline_runtime.py"
    )
    for file in (
        harness,
        route,
        root / "label/pipeline_codex.py",
        root / "label/pipeline_runtime.py",
    ):
        ast.parse(file.read_text())


if __name__ == "__main__":
    import sys

    apply(Path(sys.argv[1]))
