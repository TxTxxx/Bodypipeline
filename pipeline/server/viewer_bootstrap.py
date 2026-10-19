"""Run the pinned Visualizer once, independent of any single dataset.

``letools visualizer serve`` binds the application to one dataset and its own
data server. BodyPipeline instead keeps a single Visualizer alive and serves
every dataset through the gateway, so this bootstrap only prepares the pinned,
patched source and starts the Next.js application with the gateway as its data
origin.

The pinned Visualizer reads ``process.env.DATASET_URL`` in client code, but its
Next config omits this variable from the browser bundle. Exposing ONLY this
public origin prevents local datasets from being requested from the Hub.
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
BUILD_STAMP = ".pipeline-build.json"


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


def _fingerprint(cache: Path, data_origin: str) -> str:
    """Identify the build inputs; the origin is baked into the browser bundle."""

    source = json.loads((cache / ".letools-source.json").read_text())
    return hashlib.sha256(
        json.dumps(
            {"source": source.get("fingerprint"), "origin": data_origin},
            sort_keys=True,
        ).encode()
    ).hexdigest()


def build_if_needed(cache: Path, bun: str, env: dict, data_origin: str):
    stamp = cache / BUILD_STAMP
    wanted = _fingerprint(cache, data_origin)
    source = cache / "source"
    try:
        current = json.loads(stamp.read_text()).get("fingerprint")
    except (OSError, ValueError):
        current = None
    if current == wanted and (source / ".next").is_dir():
        return
    subprocess.run([bun, "run", "build"], cwd=source, env=env, check=True)
    stamp.write_text(json.dumps({"fingerprint": wanted}, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--data-origin", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--bun")
    args = parser.parse_args()
    cache = Path(args.cache)

    setup = [args.executable, "visualizer", "setup", "--cache-dir", str(cache)]
    if args.bun:
        setup += ["--bun", args.bun]
    subprocess.run(setup, check=True)
    patch_cache(cache)

    bun = args.bun or "bun"
    origin = args.data_origin.rstrip("/")
    env = os.environ.copy()
    env["DATASET_URL"] = origin
    env["NEXT_PUBLIC_LEROBOT_DOCTOR_URL"] = f"{origin}/doctor"
    env["NEXT_TELEMETRY_DISABLED"] = "1"
    env.pop("NEXT_PUBLIC_ANNOTATE_BACKEND_URL", None)

    build_if_needed(cache, bun, env, origin)
    source = cache / "source"
    print(
        json.dumps({"url": f"http://{args.host}:{args.port}/", "origin": origin}),
        flush=True,
    )
    os.chdir(source)
    os.execvpe(
        bun,
        [bun, "run", "start", "--hostname", args.host, "--port", str(args.port)],
        env,
    )


if __name__ == "__main__":
    main()
