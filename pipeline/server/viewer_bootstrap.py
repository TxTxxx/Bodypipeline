"""Version-checked integration patch in an isolated cache, never in the upstream checkout.

Pinned Visualizer reads process.env.DATASET_URL in client code, but its Next config
omits this variable from the browser bundle. Explicitly exposing ONLY this public
origin prevents local datasets from accidentally being requested from the Hub.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

UPSTREAM = "dc59887796fd41f37040c0df6b10e6f6a30a1854"
MARKER = "// embodied-pipeline: expose only the public dataset origin"


def patch_cache(cache: Path):
    metadata = json.loads((cache / ".letools-source.json").read_text())
    if metadata.get("upstream_commit") != UPSTREAM:
        raise RuntimeError(
            "Visualizer upstream changed: integration patch must be reviewed again"
        )
    path = cache / "source/next.config.ts"
    text = path.read_text()
    if MARKER not in text:
        anchor = "const nextConfig: NextConfig = {\n"
        if text.count(anchor) != 1 or "env:" in text:
            raise RuntimeError(
                "Unexpected Next config; refusing to apply integration patch"
            )
        text = text.replace(
            anchor,
            anchor
            + "  "
            + MARKER
            + '\n  env: { DATASET_URL: process.env.DATASET_URL || "" },\n',
        )
        path.write_text(text)
    (cache / ".pipeline-integration.json").write_text(
        json.dumps(
            {
                "upstream": UPSTREAM,
                "patch": "public-dataset-origin-v1",
                "next_config_sha256": hashlib.sha256(text.encode()).hexdigest(),
            },
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--bun")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    setup = [args.executable, "visualizer", "setup", "--cache-dir", args.cache]
    if args.bun:
        setup += ["--bun", args.bun]
    subprocess.run(setup, check=True)
    patch_cache(Path(args.cache))
    command = (
        args.command[1:] if args.command and args.command[0] == "--" else args.command
    )
    os.execv(args.executable, [args.executable, *command])


if __name__ == "__main__":
    main()
