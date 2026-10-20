"""Give a converted dataset the unit metadata its profile declares.

``AgileXProvider.patch_dataset_info`` now writes ``units`` into the LeRobot
feature description. Datasets converted before that fix carry only ``names``,
which makes a downstream physical-layout reader treat the state as unknown.
This script re-applies the missing field to a copy of such a dataset; it never
touches the original.

    python add_units.py SOURCE_DATASET DEST_DATASET
"""

import json
import os
import shutil
import sys
from pathlib import Path

UNITS = ["rad"] * 6 + ["m"] + ["rad"] * 6 + ["m"]


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    source, dest = Path(sys.argv[1]), Path(sys.argv[2])
    if not (source / "meta/info.json").is_file():
        print(f"not a LeRobot dataset: {source}")
        return 1
    if dest.exists():
        print(f"destination exists, refusing to overwrite: {dest}")
        return 1

    shutil.copytree(source, dest)

    path = dest / "meta/info.json"
    info = json.loads(path.read_text())
    for field in ("action", "observation.state"):
        feature = info["features"].get(field)
        if feature is None:
            continue
        if len(UNITS) != int(feature.get("shape", [0])[0]):
            print(f"{field}: shape mismatch, skipped")
            continue
        feature["units"] = list(UNITS)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(info, indent=2, ensure_ascii=False))
    os.replace(tmp, path)
    print(f"units written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
