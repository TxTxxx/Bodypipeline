"""Keep a derived part's millisecond display rounding out of duration limits."""

from __future__ import annotations

import ast
from pathlib import Path

MARKER = "# pipeline-argus-piece-duration-rounding:v1"


def apply(root: Path):
    path = root / "label/episode.py"
    text = path.read_text()
    if MARKER not in text:
        anchor = "    return max(actual, stated or 0.0)"
        if text.count(anchor) != 1:
            raise RuntimeError("pinned Argus request duration anchor changed")
        text = text.replace(
            anchor,
            f"""    {MARKER}
    # write_pieces stores duration_s rounded to milliseconds. Keep using the
    # measured anchor span when that is exactly the retained display claim;
    # real extra frames and distinct longer claims still enforce the limit.
    if (ep['context'].get('piece') and stated is not None
            and abs(stated - round(actual, 3)) <= 1e-9):
        stated = actual
"""
            + anchor,
            1,
        )
        ast.parse(text)
        path.write_text(text)


if __name__ == "__main__":
    import sys

    apply(Path(sys.argv[1]))
