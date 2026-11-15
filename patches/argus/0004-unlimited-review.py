"""Explicit unlimited review, retaining the native cost/receipt ledger.

Applied after 0001–0003 to the pinned Argus checkout. None is the review-level
unlimited cap; only the harness receives its existing zero-means-unlimited flag.
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

MARKER = "# pipeline-argus-unlimited:v1"


def replace_once(text, before, after):
    if text.count(before) != 1:
        raise RuntimeError(f"pinned Argus unlimited anchor changed: {before[:100]}")
    return text.replace(before, after)


def apply(root: Path):
    review = root / "review/__main__.py"
    dictionary = root / "label/dictionary_stage.py"
    if MARKER not in review.read_text():
        text = review.read_text()
        text = replace_once(
            text,
            '    ap.add_argument("--cap", type=float, default=20.0, help="most the labelling may spend, in USD (default 20)")',
            f"""    {MARKER}
    budget = ap.add_mutually_exclusive_group()
    budget.add_argument("--cap", type=float, default=20.0, help="most the labelling may spend, in USD (default 20)")
    budget.add_argument("--no-cap", dest="cap", action="store_const", const=None,
                        default=argparse.SUPPRESS, help="no monetary limit; keep cost and usage receipts")""",
        )
        text = replace_once(
            text,
            "    if not math.isfinite(a.cap) or a.cap <= 0:",
            "    if a.cap is not None and (not math.isfinite(a.cap) or a.cap <= 0):",
        )
        text = replace_once(
            text,
            "        smallest = smallest_unit_reserve(job, run_dir / 'out')",
            "        smallest = smallest_unit_reserve(job, run_dir / 'out') if a.cap is not None else None",
        )
        text = replace_once(
            text,
            '"--max-spend", str(remaining),',
            '"--max-spend", str(remaining if a.cap is not None else 0.0),',
        )
        review.write_text(text)
    if MARKER not in dictionary.read_text():
        text = dictionary.read_text()
        text = replace_once(
            text,
            "def remaining_label_budget(job: Path, cap: float) -> float:",
            "def remaining_label_budget(job: Path, cap: float | None) -> float:",
        )
        text = replace_once(
            text,
            "    if not math.isfinite(cap) or cap < 0:",
            f"    {MARKER}\n    if cap is None:\n        return math.inf\n    if not math.isfinite(cap) or cap < 0:",
        )
        text = replace_once(
            text,
            "*, free: bool, cap: float,",
            "*, free: bool, cap: float | None,",
        )
        text = replace_once(
            text,
            """                prices = harness.OPENAI_PRICES[dd.MODEL.split('/', 1)[-1]][n_in >= harness.LONG_PROMPT_TOKENS]
                bound = n_in * prices[0] + plan['max_tokens'] * prices[2]
                available = max(0.0, min(DICTIONARY_MAX_USD, cap - label_spend(job)))""",
            """                if cap is None:
                    bound = harness.final_cost_bound(plan['content'], plan['max_tokens'], model=dd.MODEL,
                                                     require_known_pricing=False)
                    available = math.inf
                else:
                    prices = harness.OPENAI_PRICES[dd.MODEL.split('/', 1)[-1]][n_in >= harness.LONG_PROMPT_TOKENS]
                    bound = n_in * prices[0] + plan['max_tokens'] * prices[2]
                    available = max(0.0, min(DICTIONARY_MAX_USD, cap - label_spend(job)))""",
        )
        dictionary.write_text(text)
    runtime = root / "label/pipeline_runtime.py"
    shutil.copyfile(
        Path(__file__).resolve().parents[2]
        / "pipeline/server/argus_template_runtime.py",
        runtime,
    )
    for path in (review, dictionary, runtime):
        ast.parse(path.read_text())


if __name__ == "__main__":
    import sys

    apply(Path(sys.argv[1]))
