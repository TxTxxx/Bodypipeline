"""Read and transactionally rewrite LeRobot task instructions.

This module runs in the isolated LeTools environment. It never changes the
dataset identity or path: only task metadata and affected data shards are
replaced in place.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq


def _fsync_dir(path: Path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _tasks(root: Path):
    parquet = root / "meta/tasks.parquet"
    jsonl = root / "meta/tasks.jsonl"
    if parquet.is_file():
        rows = pq.read_table(parquet).to_pylist()
    elif jsonl.is_file():
        rows = [
            json.loads(line)
            for line in jsonl.read_text(encoding="utf-8").splitlines()
            if line
        ]
    else:
        raise ValueError("数据集缺少 meta/tasks.parquet / tasks.jsonl")
    result = [
        {"task_index": int(row["task_index"]), "task": str(row["task"])}
        for row in rows
    ]
    if len({row["task_index"] for row in result}) != len(result):
        raise ValueError("tasks 中的 task_index 不唯一")
    if len({row["task"] for row in result}) != len(result):
        raise ValueError("tasks 中的指令文本不唯一")
    return result


def _scan_data(root: Path):
    frames = Counter()
    indices = defaultdict(Counter)
    for path in sorted((root / "data").rglob("*.parquet")):
        table = pq.read_table(path, columns=["episode_index", "task_index"])
        for episode, task_index in zip(
            table["episode_index"].to_pylist(), table["task_index"].to_pylist()
        ):
            episode, task_index = int(episode), int(task_index)
            frames[episode] += 1
            indices[episode][task_index] += 1
    return frames, indices


def read_instructions(root: Path) -> dict:
    info = json.loads((root / "meta/info.json").read_text(encoding="utf-8"))
    tasks = _tasks(root)
    text = {row["task_index"]: row["task"] for row in tasks}
    frames, indices = _scan_data(root)
    episodes = []
    for episode in sorted(frames):
        counts = indices[episode]
        task_index = counts.most_common(1)[0][0]
        episodes.append(
            {
                "episode_index": episode,
                "frames": frames[episode],
                "task_index": task_index,
                "task_indices": sorted(counts),
                "instruction": text.get(task_index),
            }
        )
    return {
        "fps": float(info["fps"]),
        "tasks": tasks,
        "episodes": episodes,
    }


def _constant_stats(value: int, count: int):
    return {
        "min": [float(value)],
        "max": [float(value)],
        "mean": [float(value)],
        "std": [0.0],
        "count": [int(count)],
    }


class _TaskIndexTotals:
    def __init__(self):
        self.count = 0
        self.minimum = None
        self.maximum = None
        self.total = 0
        self.total_squared = 0

    def add(self, values):
        if not values:
            return
        self.count += len(values)
        self.minimum = min(values) if self.minimum is None else min(self.minimum, min(values))
        self.maximum = max(values) if self.maximum is None else max(self.maximum, max(values))
        self.total += sum(values)
        self.total_squared += sum(value * value for value in values)

    def stats(self):
        if not self.count:
            raise ValueError("数据集没有 task_index")
        mean = self.total / self.count
        variance = max(0.0, self.total_squared / self.count - mean * mean)
        return {
            "min": [float(self.minimum)],
            "max": [float(self.maximum)],
            "mean": [float(mean)],
            "std": [float(np.sqrt(variance))],
            "count": [self.count],
        }


def _stage_tasks(root: Path, stage: Path, tasks: list[dict]):
    rows = sorted(tasks, key=lambda item: item["task_index"])
    parquet = root / "meta/tasks.parquet"
    if parquet.is_file():
        target = stage / "meta/tasks.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist(rows), target)
        return parquet, target
    target = stage / "meta/tasks.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return root / "meta/tasks.jsonl", target


def _stage_v2_metadata(
    root: Path,
    stage: Path,
    assignments: dict[int, str],
    assignment_indices: dict[int, int],
):
    result = []
    episodes = root / "meta/episodes.jsonl"
    if episodes.is_file():
        rows = [
            json.loads(line)
            for line in episodes.read_text(encoding="utf-8").splitlines()
            if line
        ]
        for row in rows:
            episode = int(row["episode_index"])
            if episode in assignments:
                row["tasks"] = [assignments[episode]]
        staged = stage / "meta/episodes.jsonl"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        result.append((episodes, staged))

    stats = root / "meta/episodes_stats.jsonl"
    if stats.is_file():
        rows = [
            json.loads(line)
            for line in stats.read_text(encoding="utf-8").splitlines()
            if line
        ]
        for row in rows:
            episode = int(row["episode_index"])
            if episode in assignments:
                count = int(row["stats"]["task_index"]["count"][0])
                row["stats"]["task_index"] = _constant_stats(
                    assignment_indices[episode], count
                )
        staged = stage / "meta/episodes_stats.jsonl"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        result.append((stats, staged))
    return result


def _stage_v3_metadata(
    root: Path,
    stage: Path,
    assignments: dict[int, str],
    assignment_indices: dict[int, int],
    task_index_stats: dict,
):
    result = []
    for source in sorted((root / "meta/episodes").rglob("*.parquet")):
        table = pq.read_table(source)
        rows = table.to_pylist()
        changed = False
        for row in rows:
            episode = int(row["episode_index"])
            if episode not in assignments:
                continue
            index = assignment_indices[episode]
            row["tasks"] = [assignments[episode]]
            row["stats/task_index/min"] = [float(index)]
            row["stats/task_index/max"] = [float(index)]
            row["stats/task_index/mean"] = [float(index)]
            row["stats/task_index/std"] = [0.0]
            row["stats/task_index/count"] = [int(row["length"])]
            changed = True
        if changed:
            staged = stage / source.relative_to(root)
            staged.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), staged)
            result.append((source, staged))

    stats_source = root / "meta/stats.json"
    if stats_source.is_file():
        stats = json.loads(stats_source.read_text(encoding="utf-8"))
        stats["task_index"] = task_index_stats
        staged = stage / "meta/stats.json"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(
            json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        result.append((stats_source, staged))

    return result


def _stage_info(root: Path, stage: Path, task_count: int):
    source = root / "meta/info.json"
    info = json.loads(source.read_text(encoding="utf-8"))
    if "total_tasks" in info:
        info["total_tasks"] = task_count
    # The Visualizer keys its Parquet cache from this field. Every committed
    # instruction edit must therefore publish a fresh revision even when the
    # task vocabulary count did not change.
    info["bodypipeline_revision"] = str(time.time_ns())
    staged = stage / "meta/info.json"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(
        json.dumps(info, indent=4, ensure_ascii=False), encoding="utf-8"
    )
    return source, staged


def _backup(source: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _replace(staged: Path, target: Path):
    staged.replace(target)
    _fsync_dir(target.parent)


def _rollback(root: Path, journal: dict):
    for item in reversed(journal.get("files", [])):
        backup = root / item["backup"]
        if backup.exists():
            backup.replace(root / item["path"])
            _fsync_dir((root / item["path"]).parent)


def _write_journal(path: Path, row: dict):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(row, indent=2))
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    _fsync_dir(path.parent)


def apply_instructions(spec: dict) -> dict:
    root = Path(spec["dataset_path"])
    operation = str(spec["operation_id"])
    if not operation or any(
        character
        not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for character in operation
    ):
        raise ValueError("operation_id 无效")
    assignments = {
        int(item["episode_index"]): str(item["instruction"]).strip()
        for item in spec["assignments"]
    }
    if not assignments or any(
        not text or len(text) > 2000 for text in assignments.values()
    ):
        raise ValueError("每条语言指令必须为 1–2000 个字符")

    tasks = _tasks(root)
    by_text = {row["task"]: row["task_index"] for row in tasks}
    next_index = max((row["task_index"] for row in tasks), default=-1) + 1
    for instruction in assignments.values():
        if instruction not in by_text:
            by_text[instruction] = next_index
            tasks.append({"task_index": next_index, "task": instruction})
            next_index += 1
    assignment_indices = {
        episode: by_text[instruction]
        for episode, instruction in assignments.items()
    }

    stage = root / "meta" / f".bodypipeline-instruction-stage-{operation}"
    backup = root / "meta" / f".bodypipeline-instruction-backup-{operation}"
    if stage.exists() or backup.exists():
        raise ValueError("同名指令事务已存在，请先恢复或清理")
    stage.mkdir(parents=True)
    files = []
    rewritten_shards = 0
    task_index_totals = _TaskIndexTotals()
    observed_episodes = set()

    for shard in sorted((root / "data").rglob("*.parquet")):
        table = pq.read_table(shard)
        episodes = table["episode_index"].to_pylist()
        indices = table["task_index"].to_pylist()
        changed = False
        for row, raw_episode in enumerate(episodes):
            episode = int(raw_episode)
            if episode in assignments:
                observed_episodes.add(episode)
            if (
                episode in assignment_indices
                and int(indices[row]) != assignment_indices[episode]
            ):
                indices[row] = assignment_indices[episode]
                changed = True
        task_index_totals.add(indices)
        if changed:
            column = pa.array(
                indices, type=table.schema.field("task_index").type
            )
            staged = stage / shard.relative_to(root)
            staged.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(
                table.set_column(
                    table.schema.get_field_index("task_index"),
                    "task_index",
                    column,
                ),
                staged,
            )
            files.append((shard, staged))
            rewritten_shards += 1

    missing = sorted(set(assignments) - observed_episodes)
    if missing:
        shutil.rmtree(stage)
        raise ValueError("数据集中不存在 Episode：" + "、".join(map(str, missing)))
    if not files:
        shutil.rmtree(stage)
        return {
            "updated_episodes": sorted(assignments),
            "rewritten_shards": 0,
            "total_tasks": len(tasks),
            "noop": True,
        }

    tasks_source, tasks_stage = _stage_tasks(root, stage, tasks)
    files.append((tasks_source, tasks_stage))
    if (root / "meta/episodes.jsonl").is_file():
        files.extend(
            _stage_v2_metadata(
                root, stage, assignments, assignment_indices
            )
        )
    else:
        files.extend(
            _stage_v3_metadata(
                root,
                stage,
                assignments,
                assignment_indices,
                task_index_totals.stats(),
            )
        )
    files.append(_stage_info(root, stage, len(tasks)))

    journal_path = (
        root
        / "meta"
        / f".bodypipeline-instruction-journal-{operation}.json"
    )
    journal = {"operation_id": operation, "state": "prepared", "files": []}
    try:
        for source, staged in files:
            relative = source.relative_to(root).as_posix()
            backup_path = backup / relative
            _backup(source, backup_path)
            journal["files"].append(
                {
                    "path": relative,
                    "staged": staged.relative_to(root).as_posix(),
                    "backup": backup_path.relative_to(root).as_posix(),
                }
            )
        _write_journal(journal_path, journal)
        journal["state"] = "applying"
        _write_journal(journal_path, journal)
        for item in journal["files"]:
            _replace(root / item["staged"], root / item["path"])
        journal["state"] = "applied"
        _write_journal(journal_path, journal)

        from letools import validate_dataset

        validation = validate_dataset(root, deep=True)
        if not validation.valid:
            raise ValueError("LeTools 校验失败：" + str(validation))
        report = {
            "updated_episodes": sorted(assignments),
            "rewritten_shards": rewritten_shards,
            "total_tasks": len(tasks),
            "noop": False,
        }
        journal["state"] = "committed"
        journal["validation"] = {"valid": True}
        journal["report"] = report
        _write_journal(journal_path, journal)
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)
        return report
    except Exception as exc:
        _rollback(root, journal)
        journal["state"] = "rolled_back"
        journal["error"] = str(exc)
        _write_journal(journal_path, journal)
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)
        journal_path.unlink(missing_ok=True)
        raise RuntimeError(str(exc)) from exc


def recover(root: Path) -> dict:
    recovered = []
    for journal_path in sorted(
        (root / "meta").glob(".bodypipeline-instruction-journal-*.json")
    ):
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        if journal.get("state") == "committed":
            continue
        _rollback(root, journal)
        journal["state"] = "rolled_back"
        _write_journal(journal_path, journal)
        recovered.append(journal["operation_id"])
    for directory in (root / "meta").glob(
        ".bodypipeline-instruction-stage-*"
    ):
        shutil.rmtree(directory, ignore_errors=True)
    for directory in (root / "meta").glob(
        ".bodypipeline-instruction-backup-*"
    ):
        shutil.rmtree(directory, ignore_errors=True)
    return {"recovered": recovered}


def main():
    if len(sys.argv) < 3:
        raise SystemExit("usage: instruction_writer_worker.py read|apply|recover PATH|SPEC")
    action = sys.argv[1]
    if action == "read":
        result = read_instructions(Path(sys.argv[2]))
    elif action == "recover":
        result = recover(Path(sys.argv[2]))
    elif action == "apply":
        result = apply_instructions(json.loads(Path(sys.argv[2]).read_text()))
    else:
        raise SystemExit(f"unknown action: {action}")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
