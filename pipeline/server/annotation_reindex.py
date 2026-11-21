"""Keep whole-Episode annotations aligned with a journaled recycle renumbering."""

from __future__ import annotations

import json
import os
import stat
from copy import deepcopy
from pathlib import Path

from pipeline.shared.core import DomainError, atomic_json, digest, now

JOURNAL = "annotation_reindex"


def reindex_sidecars(root: Path, mapping: dict[str, int]):
    """Rewrite active sidecars in an unpublished replacement, never linked input."""

    def remap(values):
        return {str(mapping[k]): v for k, v in values.items() if k in mapping}

    def write(path, value):
        mode = stat.S_IMODE(path.stat().st_mode)
        atomic_json(path, value)
        os.chmod(path, mode)

    for name in (
        "bodypipeline-task-annotations.json",
        "bodypipeline-manual-annotations.json",
    ):
        path = root / "meta" / name
        if path.is_file():
            value = json.loads(path.read_text())
            value["episodes"] = remap(value["episodes"])
            write(path, value)
    path = root / "meta/bodypipeline-annotation-bundle.json"
    if path.is_file():
        value = json.loads(path.read_text())
        value["episodes"] = [
            {**ep, "episode": mapping[str(ep["episode"])]}
            for ep in value["episodes"]
            if str(ep["episode"]) in mapping
        ]
        if "sourceMapping" in value:
            value["sourceMapping"] = {
                key: mapping[str(old)]
                for key, old in value["sourceMapping"].items()
                if str(old) in mapping
            }
        write(path, value)
    path = root / "meta/bodypipeline-episode-mapping.json"
    if path.is_file():
        value = json.loads(path.read_text())
        value["source_to_output"] = {
            key: mapping[str(old)]
            for key, old in value["source_to_output"].items()
            if str(old) in mapping
        }
        write(path, value)


def prepare_reindex(store, operation, root, mapping, before_signature, after_signature):
    """Freeze originals and replacements before publication; retries use this plan.

    Original records, including removed Episodes, remain in the journal. Active
    keys and history keys can overlap after renumbering, so all inputs must be
    captured before any active record is replaced.
    """
    try:
        return store.get(JOURNAL, operation)
    except DomainError as exc:
        if exc.code != 404:
            raise
    datasets = {
        row["job_id"]
        for row in store.all("preview")
        if row["dataset_path"] == str(root)
    }
    kinds = ("annotation_episode", "annotation_episode_history", "annotation_candidate")
    originals = {
        kind: [row for row in store.all(kind) if row.get("dataset") in datasets]
        for kind in kinds
    }
    for kind in ("subtask_draft", "subtask_draft_history"):
        originals[kind] = [
            row for row in store.all(kind) if row.get("dataset_path") == str(root)
        ]
    writes = {kind: {} for kind in originals}
    audit = {"operation": operation, "source_signature": before_signature}
    for kind in kinds:
        for original in originals[kind]:
            if original["source_signature"] != before_signature:
                if kind == "annotation_episode":
                    raise DomainError(
                        "已保存标注与清理前数据版本不一致，请先核对来源", 409
                    )
                # Incompatible old candidates/history stay archived, never get
                # assigned to a different current Episode by their old number.
                continue
            ep = str(original["episode"])
            if ep not in mapping:
                continue
            row = deepcopy(original)
            row.update(
                episode=mapping[ep],
                source_signature=after_signature,
                reindexed_from={**audit, "episode": original["episode"]},
            )
            eid = digest([row["dataset"], row["episode"]])
            if kind == "annotation_episode":
                row.update(
                    id=eid,
                    revision=row["revision"] + 1,
                    operation="episode-renumber",
                    updated_at=now(),
                )
            elif kind == "annotation_episode_history":
                row.update(episode_id=eid, id=f"{eid}-{row['revision']}")
            writes[kind][row["id"]] = row
    for original in originals["annotation_episode"]:
        ep = str(original["episode"])
        if ep not in mapping:
            continue
        eid = digest([original["dataset"], mapping[ep]])
        current = writes["annotation_episode"][eid]
        # Preserve both the old current revision and the renumbering checkpoint.
        for row in (
            {
                **current,
                "revision": original["revision"],
                "operation": original.get("operation", "edit"),
            },
            current,
        ):
            hid = f"{eid}-{row['revision']}"
            writes["annotation_episode_history"].setdefault(
                hid, {**row, "id": hid, "episode_id": eid}
            )
    for kind in ("subtask_draft", "subtask_draft_history"):
        for original in originals[kind]:
            if original["source_signature"] != before_signature:
                if kind == "subtask_draft":
                    raise DomainError("旧版标注草稿与清理前数据版本不一致", 409)
                continue
            row = deepcopy(original)
            for field in ("episodes", "task_documents", "atoms"):
                if field in row:
                    row[field] = {
                        str(mapping[k]): v
                        for k, v in row[field].items()
                        if k in mapping
                    }
            row.update(source_signature=after_signature, reindexed_from=audit)
            if kind == "subtask_draft":
                row["revision"] += 1
            writes[kind][row["id"]] = row
    plan = {
        "id": operation,
        "state": "prepared",
        "dataset_path": str(root),
        "mapping": mapping,
        "before_signature": before_signature,
        "after_signature": after_signature,
        "originals": originals,
        "writes": writes,
        "created_at": now(),
    }
    store.put(JOURNAL, operation, plan)
    return plan


def publish_reindex(store, operation):
    """Idempotently publish a prepared plan while holding the dataset write lock."""
    try:
        plan = store.get(JOURNAL, operation)
    except DomainError as exc:
        if exc.code == 404:
            return  # Transactions from releases before annotation journaling.
        raise
    if plan["state"] == "completed":
        return
    for kind, originals in plan["originals"].items():
        for key in {row["id"] for row in originals} | plan["writes"][kind].keys():
            store.delete(kind, key)
        for key, row in plan["writes"][kind].items():
            store.put(kind, key, row)
    plan.update(state="completed", finished_at=now())
    store.put(JOURNAL, operation, plan)
