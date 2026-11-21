"""Renumbering must never attach a neighbor's saved document or undo history."""

import json
import os
import shutil
from copy import deepcopy

import pytest

from pipeline.server.annotation_reindex import (
    JOURNAL,
    prepare_reindex,
    publish_reindex,
    reindex_sidecars,
)
from pipeline.server.annotation_workbench import AnnotationWorkbench
from pipeline.server.viewer_data import PreviewRegistry
from pipeline.shared.core import DomainError, Store


def test_sidecars_remap_active_ids_keep_provenance_and_do_not_write_linked_source(
    tmp_path,
):
    source, stage = tmp_path / "source", tmp_path / "stage"
    (source / "meta").mkdir(parents=True)
    documents = {str(i): {"note": f"original-{i}"} for i in range(3)}
    values = {
        "bodypipeline-task-annotations.json": {"version": 1, "episodes": documents},
        "bodypipeline-manual-annotations.json": {"version": 1, "episodes": documents},
        "bodypipeline-annotation-bundle.json": {
            "episodes": [
                {
                    "episode": i,
                    "source_episode": i + 10,
                    "document": documents[str(i)],
                    "complete": i == 2,
                }
                for i in range(3)
            ],
            "sourceMapping": {str(i + 10): i for i in range(3)},
            "selection": {"episodes": [10, 11, 12]},
        },
        "bodypipeline-episode-mapping.json": {
            "source_to_output": {str(i + 10): i for i in range(3)},
        },
    }
    for name, value in values.items():
        (source / "meta" / name).write_text(json.dumps(value))
    before = {p.name: p.read_bytes() for p in (source / "meta").iterdir()}
    shutil.copytree(source, stage, copy_function=os.link)
    reindex_sidecars(stage, {"0": 0, "2": 1})
    for name in list(values)[:2]:
        assert json.loads((stage / "meta" / name).read_text())["episodes"] == {
            "0": documents["0"],
            "1": documents["2"],
        }
    bundle = json.loads(
        (stage / "meta/bodypipeline-annotation-bundle.json").read_text()
    )
    assert [r["episode"] for r in bundle["episodes"]] == [0, 1]
    assert [r["source_episode"] for r in bundle["episodes"]] == [10, 12]
    assert bundle["episodes"][1]["complete"] is True
    assert bundle["sourceMapping"] == {"10": 0, "12": 1}
    assert bundle["selection"] == {"episodes": [10, 11, 12]}
    assert json.loads((stage / "meta/bodypipeline-episode-mapping.json").read_text())[
        "source_to_output"
    ] == {"10": 0, "12": 1}
    assert before == {p.name: p.read_bytes() for p in (source / "meta").iterdir()}


def test_saved_heads_history_and_candidates_reindex_atomically_on_recovery(
    tmp_path, monkeypatch
):
    store = Store(tmp_path / "state")
    root = tmp_path / "dataset"
    PreviewRegistry(store, 600).register("source", root)
    PreviewRegistry(store, 600).register("alias", root)
    PreviewRegistry(store, 600).register("unrelated", tmp_path / "other")
    originals = []
    for dataset in ("source", "alias", "unrelated"):
        for ep in range(3):
            eid = AnnotationWorkbench.key(dataset, ep)
            row = {
                "id": eid,
                "dataset": dataset,
                "episode": ep,
                "revision": 3,
                "source_signature": "before",
                "document": {"note": f"{dataset}-{ep}"},
                "complete": True,
            }
            store.put("annotation_episode", eid, row)
            hid = f"{eid}-3"
            store.put(
                "annotation_episode_history", hid, {**row, "id": hid, "episode_id": eid}
            )
            if dataset != "unrelated":
                originals.append(row)
    candidate = {
        "id": "candidate",
        "dataset": "source",
        "episode": 2,
        "source_signature": "before",
        "document": {"note": "candidate-2"},
    }
    store.put("annotation_candidate", candidate["id"], candidate)
    stale = {**candidate, "id": "incompatible", "source_signature": "older"}
    store.put("annotation_candidate", stale["id"], stale)
    legacy = {
        "id": "legacy",
        "dataset_path": str(root),
        "revision": 1,
        "source_signature": "before",
        "episodes": {"0": [], "1": ["removed"], "2": ["keep"]},
    }
    store.put("subtask_draft", legacy["id"], legacy)
    plan = prepare_reindex(store, "purge", root, {"0": 0, "2": 1}, "before", "after")
    assert plan["state"] == "prepared"
    assert store.get("annotation_episode", originals[1]["id"]) == originals[1]
    original_put = store.put

    def interrupted(kind, key, value):
        if kind == "annotation_episode_history":
            raise OSError("simulated interrupted publish")
        return original_put(kind, key, value)

    with monkeypatch.context() as patch:
        patch.setattr(store, "put", interrupted)
        with pytest.raises(OSError, match="interrupted"):
            publish_reindex(store, "purge")
    # Recovery uses frozen originals, even after some overlapping keys were replaced.
    prepare_reindex(store, "purge", root, {"0": 0, "2": 1}, "before", "after")
    publish_reindex(store, "purge")
    after = deepcopy(store.all("annotation_episode"))
    publish_reindex(store, "purge")
    assert after == store.all("annotation_episode")
    assert {
        row["id"]: row
        for row in store.get(JOURNAL, "purge")["originals"]["annotation_episode"]
    } == {row["id"]: row for row in originals}
    for dataset in ("source", "alias"):
        head = store.get("annotation_episode", AnnotationWorkbench.key(dataset, 1))
        assert head["document"] == {"note": f"{dataset}-2"}
        assert head["complete"] and head["revision"] == 4
        assert head["source_signature"] == "after"
        histories = [
            r
            for r in store.all("annotation_episode_history")
            if r["episode_id"] == head["id"]
        ]
        assert {r["revision"] for r in histories} == {3, 4}
        assert all(r["document"] == head["document"] for r in histories)
        with pytest.raises(DomainError):
            store.get("annotation_episode", AnnotationWorkbench.key(dataset, 2))
    assert (
        store.get("annotation_episode", AnnotationWorkbench.key("unrelated", 1))[
            "source_signature"
        ]
        == "before"
    )
    assert store.get("annotation_candidate", "candidate")["episode"] == 1
    with pytest.raises(DomainError):
        store.get("annotation_candidate", "incompatible")
    assert store.get("subtask_draft", "legacy")["episodes"] == {"0": [], "1": ["keep"]}


def test_unknown_source_change_does_not_migrate_saved_head(tmp_path):
    store = Store(tmp_path / "state")
    root = tmp_path / "dataset"
    PreviewRegistry(store, 600).register("source", root)
    eid = AnnotationWorkbench.key("source", 0)
    row = {
        "id": eid,
        "dataset": "source",
        "episode": 0,
        "revision": 1,
        "source_signature": "unrelated-source",
        "document": {},
    }
    store.put("annotation_episode", eid, row)
    with pytest.raises(DomainError, match="清理前数据版本不一致"):
        prepare_reindex(store, "purge", root, {"0": 0}, "before", "after")
    assert store.get("annotation_episode", eid) == row
    assert store.all(JOURNAL) == []
