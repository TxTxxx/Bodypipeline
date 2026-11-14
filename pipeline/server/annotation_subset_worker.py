"""Independent whole-Episode extraction, including shared v3 shards/videos.

Reuses the existing, verified format-aware rebuild, exclusively into a new tree.
Source files are read-only. Labels are patched afterwards from a frozen plan.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
from pathlib import Path


def _detach_links(root):
    for path in root.rglob("*"):
        if path.is_file() and path.stat().st_nlink > 1:
            temp = path.with_name("." + path.name + ".independent")
            shutil.copy2(path, temp)
            os.replace(temp, path)


def _prune_tasks(root):
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq
    from letools import open_dataset
    from letools._stats import aggregate_episode_stats, flatten_stats
    from recycle_worker import _jsonl, _scalar_stats, _write_table
    from subtask_export_worker import _write_json

    source = open_dataset(root)
    ids = set()
    tables = {}
    for file in (root / "data").rglob("*.parquet"):
        table = pq.read_table(file)
        tables[file] = table
        if "task_index" in table.column_names:
            ids.update(table["task_index"].to_pylist())
    mapping = {old: i for i, old in enumerate(sorted(ids))}
    task_file = root / "meta/tasks.parquet"
    if task_file.exists():
        task_table = pq.read_table(task_file)
        tasks = [
            {**r, "task_index": mapping[r["task_index"]]}
            for r in task_table.to_pylist()
            if r["task_index"] in mapping
        ]
        _write_table(task_file, pa.Table.from_pylist(tasks, schema=task_table.schema))
    else:
        task_file = root / "meta/tasks.jsonl"
        tasks = [
            {**r, "task_index": mapping[r["task_index"]]}
            for line in task_file.read_text().splitlines()
            if line
            for r in [json.loads(line)]
            if r["task_index"] in mapping
        ]
        _jsonl(task_file, tasks)
    stats = []
    for ep in source.episodes:
        ep_stats = copy.deepcopy(ep.stats)
        table = source.read_episode(ep)
        if "task_index" in table.column_names:
            ep_stats["task_index"] = _scalar_stats(
                np.array([mapping[v] for v in table["task_index"].to_pylist()])
            )
        stats.append({"episode_index": ep.index, "stats": ep_stats})
    for file, table in tables.items():
        if "task_index" in table.column_names:
            field = table.schema.field("task_index")
            table = table.set_column(
                table.schema.get_field_index("task_index"),
                field,
                pa.array(
                    [mapping[v] for v in table["task_index"].to_pylist()],
                    type=field.type,
                ),
            )
            _write_table(file, table)
    info = copy.deepcopy(source.metadata.info)
    info["total_tasks"] = len(tasks)
    _write_json(root / "meta/info.json", info)
    _write_json(
        root / "meta/stats.json", aggregate_episode_stats([s["stats"] for s in stats])
    )
    if info["codebase_version"] == "v2.1":
        _jsonl(root / "meta/episodes_stats.jsonl", stats)
    else:
        by_id = {r["episode_index"]: r["stats"] for r in stats}
        for file in (root / "meta/episodes").rglob("*.parquet"):
            rows = [
                {
                    **{k: v for k, v in row.items() if not k.startswith("stats/")},
                    **flatten_stats(by_id[row["episode_index"]]),
                }
                for row in pq.read_table(file).to_pylist()
            ]
            _write_table(file, pa.Table.from_pylist(rows))


def extract(
    source: Path,
    destination: Path,
    selected: list[int],
    source_format: str,
    target_format: str,
):
    # The worker runs with its own cwd and isolated site-packages. Only the
    # stdlib shared core is imported from the installed pipeline tree.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from letools import ConversionConfig, convert, open_dataset, validate_dataset
    from recycle_worker import rebuild
    from subtask_export_worker import _copy_tree

    data = open_dataset(source)
    available = {e.index for e in data.episodes}
    chosen = sorted(set(selected))
    if not chosen or not set(chosen) <= available:
        raise ValueError("子集选择为空或已失效，拒绝回退全量")
    mapping = {str(old): new for new, old in enumerate(chosen)}
    stage = (
        destination.with_name(destination.name + "-subset-source")
        if source_format != target_format
        else destination
    )
    deleted = available - set(chosen)
    if deleted:
        result = rebuild(source, stage, deleted, lambda _text: None)
        if result["mapping"] != mapping:
            raise ValueError("子集来源映射不一致")
    else:
        _copy_tree(source, stage)
    _detach_links(stage)
    # Copied episode-specific annotations are superseded by the selected frozen
    # bundle below, never ship unselected archived annotation sidecars.
    for pattern in (
        "bodypipeline-task-annotations*.json",
        "bodypipeline-manual-annotations*.json",
        "bodypipeline-annotation-bundle*.json",
    ):
        for path in (stage / "meta").glob(pattern):
            path.unlink()
    _prune_tasks(stage)
    if source_format != target_format:
        convert(
            stage,
            destination,
            target_format,
            config=ConversionConfig(workers=1, video_workers=1),
        )
        shutil.rmtree(stage)
    report = validate_dataset(destination, deep=True)
    if not report.valid:
        raise ValueError("子集独立验证失败：" + "; ".join(report.errors[:8]))
    return mapping
