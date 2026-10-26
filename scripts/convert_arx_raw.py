"""Retry offline conversion while preserving the original independent raw data.

Run from the project root: python -m scripts.convert_arx_raw episode.raw --profile PROFILE.json
"""

import argparse
import json
import os
import sqlite3
from pathlib import Path

from pipeline.client.capture_sources.arx_raw import convert_raw
from pipeline.shared.capture_formats import CAPTURE_FILENAME


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--max-gap-ms", type=float)
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.background:
        os.nice(5)
    if args.profile:
        profile = json.loads(args.profile.read_text())
    else:
        with sqlite3.connect(f"{args.raw.resolve().as_uri()}?mode=ro", uri=True) as db:
            profile = json.loads(db.execute("SELECT value FROM metadata WHERE key='profile'").fetchone()[0])
    report = convert_raw(
        args.raw,
        args.output or args.raw.with_name(CAPTURE_FILENAME),
        profile,
        max_gap_ms=args.max_gap_ms,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
