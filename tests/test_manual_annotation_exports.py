import json
import os
import zipfile

from test_manual_annotation_service import bind, head_and_body
from test_manual_annotation_service import service as service


def test_bundle_has_portable_checksums_and_no_source_paths(tmp_path):
    from pipeline.server.manual_annotation_exports import write_annotation_bundle

    root = tmp_path / "dataset"
    (root / "meta").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "videos").mkdir()
    (root / "meta/info.json").write_text('{"total_episodes": 1}')
    (root / "data/frames.parquet").write_bytes(b"frames")
    (root / "videos/view.mp4").write_bytes(b"video")
    snapshot = {
        "dataset_uid": "dataset-uid",
        "revision": 2,
        "scheme": {"name": "test"},
        "episodes": [
            {
                "episode_uid": "one",
                "episode_index": 0,
                "frame_timestamps": [0, 0.1],
                "records": [],
                "status": "completed",
            }
        ],
    }
    target = tmp_path / "annotations.zip"
    write_annotation_bundle(target, snapshot, root)
    with zipfile.ZipFile(target) as archive:
        assert "episodes.jsonl" in archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["media_bytes_verified"] is False
        files = {f["path"]: f for f in manifest["source_files"]}
        assert (
            files["data/frames.parquet"]["sha256"]
            == "594cfd2607d4f09e1ce6241203e418cacfed545063792bc5eab7afaadbf0c4e0"
        )
        assert "sha256" not in files["videos/view.mp4"]
        assert str(root).encode() not in b"".join(
            archive.read(n) for n in archive.namelist()
        )


def test_recycle_remaps_old_and_custom_sidecar_without_mutating_hardlinks(tmp_path):
    from pipeline.server.recycle_worker import remap_annotation_sidecars

    source, stage = tmp_path / "source", tmp_path / "stage"
    for root in (source, stage):
        (root / "meta").mkdir(parents=True)
    old = source / "meta/bodypipeline-manual-annotations.json"
    old.write_text(
        json.dumps(
            {
                "version": 1,
                "episodes": {"0": ["first"], "1": ["deleted"], "2": ["third"]},
            }
        )
    )
    custom = source / "meta/bodypipeline-custom-annotations.json"
    custom.write_text(
        json.dumps(
            {
                "scheme": {"name": "schema"},
                "episodes": [
                    {"episode_uid": "first", "episode_index": 0},
                    {"episode_uid": "deleted", "episode_index": 1},
                    {"episode_uid": "third", "episode_index": 2},
                ],
            }
        )
    )
    for path in (old, custom):
        os.link(path, stage / "meta" / path.name)
    remap_annotation_sidecars(stage, {"0": 0, "2": 1})
    assert json.loads(old.read_text())["episodes"]["1"] == ["deleted"]
    assert json.loads((stage / "meta" / old.name).read_text())["episodes"] == {
        "0": ["first"],
        "1": ["third"],
    }
    records = json.loads((stage / "meta" / custom.name).read_text())["episodes"]
    assert [(r["episode_uid"], r["episode_index"]) for r in records] == [
        ("first", 0),
        ("third", 1),
    ]
    assert len(json.loads(custom.read_text())["episodes"]) == 3


def test_pending_annotation_export_blocks_dataset_delete(settings_factory):
    from types import SimpleNamespace

    import pytest
    from test_dataset_management import seed_result

    from pipeline.server.dataset_management import DatasetManager
    from pipeline.shared.core import DomainError, Store

    settings = settings_factory("server")
    store = Store(settings.root / "state")
    runner = SimpleNamespace(
        settings=settings, store=store, ingest=SimpleNamespace(store=store)
    )
    app = SimpleNamespace(
        state=SimpleNamespace(runner=runner, ingest=runner.ingest, viewer=None)
    )
    job, path = seed_result(app)
    manager = DatasetManager(
        app.state.runner.settings, app.state.runner, app.state.viewer
    )
    app.state.runner.store.put(
        "manual_annotation_export",
        "export",
        {
            "id": "export",
            "dataset_path": str(path),
            "job_id": job["id"],
            "state": "QUEUED",
        },
    )
    with pytest.raises(DomainError, match="标注包"):
        manager._validate([("job", job)], [path])


def test_revision_sidecar_is_frozen_and_does_not_write_through_source(tmp_path):
    from pipeline.server.subtask_export_worker import write_custom_annotation_sidecar

    source, destination = tmp_path / "source", tmp_path / "destination"
    for path in (source, destination):
        (path / "meta").mkdir(parents=True)
    original = source / "meta/bodypipeline-custom-annotations.json"
    original.write_text('{"old": true}')
    os.link(original, destination / "meta" / original.name)
    snapshot = {
        "dataset_uid": "source",
        "revision": 3,
        "_source_path": str(source),
        "scheme": {"name": "example"},
        "episodes": [
            {
                "episode_uid": "stable",
                "episode_index": 0,
                "values": {"outcome": "success"},
            }
        ],
    }
    write_custom_annotation_sidecar(destination, snapshot)
    assert json.loads(original.read_text()) == {"old": True}
    written = json.loads((destination / "meta" / original.name).read_text())
    assert written["revision"] == 3
    assert written["episodes"][0]["values"] == {"outcome": "success"}
    assert "_source_path" not in written
    assert written["dataset_uid"] == "source"


def test_export_queue_uses_frozen_revision_and_only_completed_by_default(service):
    from pipeline.server.manual_annotation_exports import ManualAnnotationExports

    workspace, source = service
    bind(workspace)
    uid, body = head_and_body(workspace)
    workspace.save(
        "sample",
        uid,
        {**body, "values": {"outcome": {"success": True}}, "status": "completed"},
    )
    latest = workspace.summary("sample")
    exports = ManualAnnotationExports(workspace.settings, workspace.store)
    queued = exports.enqueue(
        "sample",
        {
            "revision": latest["revision"],
            "source_signature": latest["source_signature"],
        },
    )
    uid, body = head_and_body(workspace)
    workspace.save("sample", uid, {**body, "values": {"outcome": {"success": False}}})
    assert exports.tick() is True
    assert exports.status("sample", queued["id"])["state"] == "COMPLETED"
    with zipfile.ZipFile(exports.download("sample", queued["id"])) as archive:
        rows = [
            json.loads(line) for line in archive.read("episodes.jsonl").splitlines()
        ]
    assert len(rows) == 1
    assert rows[0]["values"]["outcome"]["success"] is True
    assert rows[0]["status"] == "completed"
    assert workspace.episode("sample", uid)["status"] == "draft"


def test_export_rejects_changed_source_and_wrong_dataset_access(service):
    import pytest

    from pipeline.server.manual_annotation_exports import ManualAnnotationExports
    from pipeline.shared.core import DomainError

    workspace, source = service
    workspace.summary("sample")
    exports = ManualAnnotationExports(workspace.settings, workspace.store)
    queued = exports.enqueue("sample", {"include_drafts": True})
    with pytest.raises(DomainError):
        exports.status("another", queued["id"])
    (source / "meta/info.json").write_text('{"changed": true}')
    assert exports.tick() is True
    assert exports.status("sample", queued["id"])["state"] == "FAILED"
    with pytest.raises(DomainError):
        exports.download("sample", queued["id"])


def test_purge_keeps_old_payload_until_annotation_recovery_succeeds(
    tmp_path, monkeypatch
):
    import pytest

    from pipeline.server import manual_annotations, recycle_worker
    from pipeline.shared.core import Store

    store = Store(tmp_path / "state")
    root = tmp_path / "dataset"
    root.mkdir()
    row = {
        "id": "key",
        "dataset_path": str(root),
        "revision": 1,
        "operation": {
            "id": "operation",
            "entries_before": {"1": {"episode": 1}},
            "state": "running",
        },
    }
    stage = recycle_worker._stage(row)
    stage.mkdir()
    (stage / "old-data").write_bytes(b"old")
    (root / recycle_worker.MARKER).write_text(
        json.dumps(
            {
                "operation": "operation",
                "mapping": {"0": 0, "2": 1},
                "total_episodes": 2,
                "deleted_frames": 3,
            }
        )
    )
    attempts = []

    def remap(*args):
        attempts.append(args)
        if len(attempts) == 1:
            raise OSError("storage unavailable")

    monkeypatch.setattr(manual_annotations, "remap_workspace", remap)
    with pytest.raises(OSError):
        recycle_worker.recover_operation(store, row)
    assert (stage / "old-data").read_bytes() == b"old"
    assert row["operation"]["state"] == "running"
    recycle_worker.recover_operation(store, row)
    assert not stage.exists()
    assert row["operation"]["state"] == "completed"
    assert attempts[1][2:] == ({"0": 0, "2": 1}, "operation")


def test_same_revision_dataset_export_is_deduplicated(service, tmp_path):
    from threading import RLock
    from types import SimpleNamespace

    from pipeline.server.merges import MergeRunner
    from pipeline.server.subtask_export import SubtaskExportRunner
    from pipeline.server.subtask_review import RevisionExportRequest
    from pipeline.server.viewer_data import PreviewRegistry

    workspace, source = service
    bind(workspace)
    head = workspace.summary("sample")
    tool = tmp_path / "letools"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    tool.with_name("python").write_text("placeholder")
    workspace.settings.letools_bin = str(tool)
    PreviewRegistry(workspace.store, workspace.settings.viewer_ttl).register(
        "sample", source
    )
    runner = SimpleNamespace(
        store=workspace.store, settings=workspace.settings, lock=RLock()
    )
    runner.merges = MergeRunner(workspace.settings, runner)
    exports = SubtaskExportRunner(workspace.settings, runner)
    body = RevisionExportRequest(
        revision=head["revision"], source_signature=head["source_signature"]
    )
    first = exports.enqueue_review("sample", body)
    second = exports.enqueue_review("sample", body)
    assert second["id"] == first["id"]


def test_exports_preserve_user_field_keys_and_language_args(tmp_path):
    from pipeline.server.manual_annotation_exports import write_annotation_bundle
    from pipeline.server.subtask_export_worker import write_custom_annotation_sidecar

    root = tmp_path / "source"
    (root / "meta").mkdir(parents=True)
    values = {"_group": {"source_path": "user value", "_note": "keep"}}
    args = {"storage_dir": "user tool argument"}
    snapshot = {
        "_source_path": str(root),
        "episodes": [{"values": values, "atoms": [{"tool_calls": [args]}]}],
    }
    target = tmp_path / "labels.zip"
    write_annotation_bundle(target, snapshot, root)
    with zipfile.ZipFile(target) as archive:
        record = json.loads(archive.read("episodes.jsonl"))
        assert record["values"] == values
        assert record["atoms"][0]["tool_calls"] == [args]
    write_custom_annotation_sidecar(root, snapshot)
    record = json.loads(
        (root / "meta/bodypipeline-custom-annotations.json").read_text()
    )["episodes"][0]
    assert record["values"] == values
    assert record["atoms"][0]["tool_calls"] == [args]


def test_purge_refuses_to_bless_stale_workspace(service):
    import pytest

    from pipeline.server.recycle_worker import check_annotation_source
    from pipeline.shared.core import DomainError

    workspace, source = service
    workspace.summary("sample")
    check_annotation_source(workspace.store, source)
    info = source / "meta/info.json"
    info.write_text(info.read_text() + "\n")
    with pytest.raises(DomainError, match="来源"):
        check_annotation_source(workspace.store, source)


def test_unmigrated_legacy_draft_survives_reindex(service):
    from pipeline.server.manual_annotations import remap_workspace, workspace_key
    from pipeline.server.merges import source_snapshot

    workspace, source = service
    key = workspace_key(source)
    workspace.store.put(
        "subtask_draft",
        key,
        {
            "id": key,
            "revision": 2,
            "source_signature": "old",
            "episodes": {"1": [{"text": "keep"}]},
            "atoms": {"1": [{"content": "keep atom"}]},
        },
    )
    remap_workspace(workspace.store, source, {"1": 0}, "operation")
    saved = workspace.store.get("subtask_draft", key)
    assert saved["episodes"] == {"0": [{"text": "keep"}]}
    assert saved["atoms"] == {"0": [{"content": "keep atom"}]}
    assert saved["source_signature"] == source_snapshot(source)["signature"]
    remap_workspace(workspace.store, source, {"1": 0}, "operation")
    assert workspace.store.get("subtask_draft", key)["revision"] == 3
