"""Bundle the dependency-free TypeScript workspace, or verify the shipped bundle.

Run `python scripts/build_workbench.py [--check] [--bun /path/to/bun]` after
editing viewer_ui/workbench*.ts. The resulting asset ships in the API package.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--bun", default=shutil.which("bun"))
    args = parser.parse_args()
    if not args.bun:
        parser.error("Bun is required to build the workspace")
    output = root / "pipeline/web/workbench.js"
    with tempfile.TemporaryDirectory(prefix="bodypipeline-workbench-") as directory:
        target = Path(directory) / "workbench.js" if args.check else output
        subprocess.run(
            [
                args.bun,
                "build",
                "pipeline/server/viewer_ui/workbench.ts",
                "--target",
                "browser",
                "--outfile",
                str(target),
            ],
            cwd=root,
            check=True,
        )
        if args.check and target.read_bytes() != output.read_bytes():
            raise SystemExit(
                "Shipped workbench.js is stale; run scripts/build_workbench.py"
            )


if __name__ == "__main__":
    main()
