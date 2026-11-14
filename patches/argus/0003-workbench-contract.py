"""Install one template business contract and shared request retry accounting.

Applied only to the pinned checkout after 0001/0002, idempotently. No model calls.
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

MARKER = "# pipeline-argus-workbench-contract:v2"


def _start(text, name, code):
    node = next(
        n
        for n in ast.parse(text).body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )
    first = (
        node.body[1]
        if isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
        else node.body[0]
    )
    lines = text.splitlines(keepends=True)
    lines.insert(first.lineno - 1, code)
    return "".join(lines)


def apply(root: Path):
    episode = root / "label/episode.py"
    harness = root / "label/harness.py"
    review = root / "review/__main__.py"
    if MARKER not in episode.read_text():
        text = episode.read_text()
        old = "    fixed += task_contract(ctx)\n"
        if text.count(old) != 1:
            raise RuntimeError("pinned Argus template prompt anchor changed")
        text = text.replace(
            old,
            """    # pipeline-argus-workbench-contract:v2
    if task_contract(ctx):
        from label.pipeline_template import template_episode_facts
        return task_contract(ctx), template_episode_facts(ep, pl)
""",
        )
        episode.write_text(text)
    if MARKER not in harness.read_text():
        text = harness.read_text()
        text = _start(
            text,
            "parse_response",
            """    # pipeline-argus-workbench-contract:v2
    from label.pipeline_template import template_spec, parse_template_reply
    if template_spec() is not None:
        return parse_template_reply(text)
""",
        )
        text = _start(
            text,
            "read_reply",
            """    from label.pipeline_template import template_spec, parse_template_reply
    if template_spec() is not None:
        return parse_template_reply(text, request)
""",
        )
        text = _start(
            text,
            "auto_resend",
            """    from label.pipeline_template import logical_retry_reason
    return logical_retry_reason(record)
""",
        )
        # All transport sends use one attempt; the durable logical request
        # controller owns the three retries. Unpaid retries cannot multiply it.
        text = text.replace("UNPAID_ATTEMPTS = 4", "UNPAID_ATTEMPTS = 1")
        text = text.replace("AUTO_RESENDS = 2", "AUTO_RESENDS = 3")
        text = text.replace(
            "AUTO_RESEND_DELAYS_S = (30.0, 120.0)",
            "AUTO_RESEND_DELAYS_S = (2.0, 8.0, 20.0)",
        )
        anchor = '    return {"version": 2, "source_proof": source_proof(ctx, ep_dir), "settings": settings}'
        if text.count(anchor) != 1:
            raise RuntimeError("pinned Argus request identity anchor changed")
        text = text.replace(
            anchor,
            """    from label.pipeline_template import request_contract_identity
    contract = request_contract_identity()
    if contract:
        settings['bodypipeline_business_contract'] = contract
"""
            + anchor,
        )
        anchor = "    if pricing_quote is not None:\n        fields['config']['pricing_quote'] = pricing_quote"
        if text.count(anchor) != 1:
            raise RuntimeError("pinned Argus request fields anchor changed")
        text = text.replace(
            anchor,
            """    from label.pipeline_template import template_spec
    if template_spec() is not None:
        fields['bodypipeline_context'] = {'fps': context['fps'], 'n_state_frames': context['n_state_frames'], 'piece': context.get('piece')}
        fields['config']['data_frame_indices'] = list(map(int, pl['ks']))
"""
            + anchor,
        )
        anchor = "    bound = final_cost_bound(content, max_tokens, model=model, require_known_pricing=require_known_pricing,"
        if text.count(anchor) != 1:
            raise RuntimeError("pinned Argus dispatch prompt anchor changed")
        text = text.replace(
            anchor,
            """    fields['request_text_parts'] = [part['text'] for part in content if part.get('type') == 'text']
    fields['prompt_text'] = '\\n\\n'.join(fields['request_text_parts'])
"""
            + anchor,
        )
        # _post can also serve a routing or dictionary call. Each logical call
        # has at most four sends, and paid final calls already pass attempts=1.
        text = text.replace(
            "unknown_left, unpaid_left = attempts, max(attempts, UNPAID_ATTEMPTS)",
            "unknown_left, unpaid_left = min(attempts, 4), min(4, max(attempts, UNPAID_ATTEMPTS))",
        )
        post = next(
            n
            for n in ast.parse(text).body
            if isinstance(n, ast.FunctionDef) and n.name == "_post"
        )
        lines = text.splitlines(keepends=True)
        body = "".join(lines[post.lineno - 1 : post.end_lineno])
        old_loop = "    while True:\n        tries += 1\n"
        if body.count(old_loop) != 1:
            raise RuntimeError("pinned Argus transport loop anchor changed")
        body = body.replace(
            old_loop,
            "    while True:\n        if tries >= 4:\n            raise DispatchError('logical request reached four total HTTP sends', verified_unpaid=clean, stage='not sent' if clean else 'unknown')\n        tries += 1\n",
        )
        text = (
            "".join(lines[: post.lineno - 1]) + body + "".join(lines[post.end_lineno :])
        )
        harness.write_text(text)
    if MARKER not in review.read_text():
        text = review.read_text()
        anchor = "    rep, preparation = verified_preparation(job, data, a, dataset, overrides)\n"
        if text.count(anchor) != 1:
            raise RuntimeError("pinned Argus preparation scope anchor changed")
        text = text.replace(
            anchor,
            anchor
            + """    # pipeline-argus-workbench-contract:v2
    from label.pipeline_runtime import select_scope
    rep, preparation = select_scope(job, eps, rep, preparation, overrides, policy, preparation_checkpoint)
""",
        )
        anchor = "        spend = dictionary_stage.dictionary_spend(job)\n"
        if text.count(anchor) != 1:
            raise RuntimeError("pinned Argus boundary stage anchor changed")
        text = text.replace(
            anchor,
            """        from label.pipeline_runtime import refine_outputs
        refine_outputs(job, a)
"""
            + anchor,
        )
        review.write_text(text)
    source = Path(__file__).resolve().parents[2] / "pipeline/server"
    for origin, target in [
        ("argus_template_contract.py", "pipeline_template.py"),
        ("argus_template_runtime.py", "pipeline_runtime.py"),
    ]:
        shutil.copyfile(source / origin, root / "label" / target)
    for path in (
        episode,
        harness,
        review,
        root / "label/pipeline_template.py",
        root / "label/pipeline_runtime.py",
    ):
        ast.parse(path.read_text())


if __name__ == "__main__":
    import sys

    apply(Path(sys.argv[1]))
