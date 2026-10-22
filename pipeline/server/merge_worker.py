"""Standalone LeTools worker; invoked in its Python environment or Slurm step."""

import json
import sys
from pathlib import Path


def run(spec_path):
    from letools.merge import merge_datasets

    spec = json.loads(Path(spec_path).read_text())
    result = merge_datasets(
        [source["dataset_path"] for source in spec["sources"]],
        spec["destination"],
        auto=False,
        use_cache=False,
        overwrite=False,
        validate=True,
    )
    return {
        "validated": True,
        "version": result.version,
        "episodes": result.episodes,
        "frames": result.frames,
        "tasks": result.tasks,
        "elapsed_seconds": result.elapsed_seconds,
        "cloned_files": result.cloned_files,
        "copied_files": result.copied_files,
        "copied_bytes": result.copied_bytes,
    }


if __name__ == "__main__":
    try:
        print(json.dumps(run(sys.argv[1]), ensure_ascii=False, allow_nan=False))
    except Exception as exc:
        print(f"数据集合并失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from None
