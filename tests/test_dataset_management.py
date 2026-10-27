import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_ingest import manifest

from pipeline.server.app import create_app
from pipeline.server.dataset_management import DatasetManager
from pipeline.server.datasets import dataset_for_job
from pipeline.server.recycle import dataset_lock
from pipeline.server.storage import storage_path
from pipeline.shared.core import DomainError
from pipeline.shared.hdf5 import ALOHA_HDF5_FORMAT


def seed_result(app, jid="converted", **fields):
    job = {
        "id": jid,
        "key": jid,
        "dataset_name": "抓取训练集",
        "robot_type": "agilex_aloha",
        "created_at": 1,
        "state": "CONVERTED",
        "dataset_ready": True,
        "target_format": "v3.0",
        **fields,
    }
    path = storage_path(app.state.runner.settings, job, "work", jid) / "dataset"
    (path / "meta").mkdir(parents=True)
    (path / "meta/info.json").write_text(
        json.dumps(
            {
                "codebase_version": "v3.0",
                "robot_type": "agilex_aloha",
                "total_episodes": 1,
                "total_frames": 2,
            }
        )
    )
    job["dataset_path"] = str(path)
    app.state.ingest.store.put("job", jid, job)
    return job, path


def seed_raw(app, session="session-one"):
    ingest = app.state.ingest
    m, data = manifest(ingest.settings)
    m["session_id"] = session
    row = ingest.create(m, "default", "")
    ingest.chunk(row["id"], 0, 0, hashlib.sha256(data).hexdigest(), data)
    return ingest.complete(row["id"])


@pytest.mark.parametrize("shared", [False, True])
def test_api_delete_result_independent_of_raw_and_other_outputs(
    settings_factory, tmp_path, shared
):
    root = tmp_path / "shared"
    root.mkdir()
    settings = settings_factory("server", data_root=root if shared else None)
    app = create_app(settings, background=False)
    raw = seed_raw(app)
    layout = {k: raw[k] for k in ("data_layout", "robot_type") if k in raw}
    job, path = seed_result(app, upload_ids=[raw["id"]], **layout)
    merged, merged_path = seed_result(
        app,
        "independent-merge",
        operation="merge",
        source_job_ids=[job["id"]],
        **layout,
    )
    app.state.viewer.registry.register(job["id"], path)
    admin = {"Authorization": "Bearer " + settings.token}
    body = {"kind": "result", "id": job["id"], "confirmation": job["dataset_name"]}
    with TestClient(app) as http:
        assert http.get("/api/datasets").status_code == 401
        assert http.post("/api/dataset-deletions", json=body).status_code == 401
        assert (
            http.post(
                "/api/dataset-deletions",
                json=body,
                headers={"Authorization": "Bearer " + settings.device_token},
            ).status_code
            == 401
        )
        assert (
            http.post(
                "/api/dataset-deletions",
                json={**body, "path": str(root)},
                headers=admin,
            ).status_code
            == 422
        )
        assert (
            http.post(
                "/api/dataset-deletions",
                json={**body, "confirmation": "wrong"},
                headers=admin,
            ).status_code
            == 400
        )
        assert len(http.get("/api/datasets", headers=admin).json()["datasets"]) == 3
        response = http.post("/api/dataset-deletions", json=body, headers=admin)
        assert response.status_code == 202, response.text
        assert path.exists()  # I/O runs in the worker, not the request.
        assert app.state.viewer.registry.active() == []
        assert (
            http.post(f"/api/jobs/{job['id']}/viewer", headers=admin).status_code == 409
        )
        assert (
            http.post(f"/api/jobs/{job['id']}/checks", headers=admin).status_code == 409
        )
        assert app.state.datasets.tick()
        assert not path.exists() and merged_path.exists()
        assert Path(raw["raw_path"]).exists()
        assert all(
            j["id"] != job["id"] for j in http.get("/api/jobs", headers=admin).json()
        )
        assert app.state.ingest.store.get("job", job["id"])["state"] == "DELETED"
        assert (
            http.get("/api/datasets", headers=admin).json()["deletions"][0]["state"]
            == "COMPLETED"
        )


def test_delete_raw_collection_removes_episodes_preserves_result_and_receipt(
    settings_factory,
):
    app = create_app(settings_factory("server"), background=False)
    rows = [seed_raw(app, session) for session in ("one", "two")]
    job, path = seed_result(app, upload_ids=[r["id"] for r in rows])
    cid = rows[0]["collection_id"]
    op = app.state.datasets.enqueue("raw", cid, rows[0]["dataset_name"])
    assert len(op["records"]) == 2
    app.state.datasets.tick()
    assert path.exists()
    assert not any(Path(r["raw_path"]).exists() for r in rows)
    assert app.state.datasets.collections() == []
    assert app.state.ingest.store.get("job", job["id"])["state"] == "CONVERTED"
    with pytest.raises(DomainError):
        app.state.ingest.complete(rows[0]["id"])
    m, _ = manifest(app.state.runner.settings)
    m["session_id"] = "one"
    with pytest.raises(DomainError, match="已删除"):
        app.state.ingest.create(m, "default", "")


@pytest.mark.parametrize(
    "operation", ["convert", "merge", "version", "check", "analysis"]
)
def test_active_consumers_block_deletion(settings_factory, operation):
    app = create_app(settings_factory("server"), background=False)
    raw = seed_raw(app)
    job, path = seed_result(app)
    kind, identifier, name = "result", job["id"], job["dataset_name"]
    row = {"id": "active", "key": "active", "state": "QUEUED", "created_at": 1}
    bucket = "job"
    if operation == "convert":
        row["upload_ids"] = [raw["id"]]
        kind, identifier, name = "raw", raw["collection_id"], raw["dataset_name"]
    elif operation == "merge":
        row["source_job_ids"] = [job["id"]]
    elif operation == "version":
        row["source_job_id"] = job["id"]
    else:
        bucket = operation
        row["job_id"] = job["id"]
    app.state.ingest.store.put(bucket, row["id"], row)
    with pytest.raises(DomainError, match="使用|质检|分析"):
        app.state.datasets.enqueue(kind, identifier, name)
    assert path.exists() and Path(raw["raw_path"]).exists()


def test_imported_raw_only_deletes_manifest_files(settings_factory, tmp_path):
    source = tmp_path / "imports"
    source.mkdir()
    settings = settings_factory("server", import_roots=[source])
    app = create_app(settings, background=False)
    episode = source / "capture"
    (episode / "episode0").mkdir(parents=True)
    m, data = manifest(settings)
    (episode / "episode0/example.json").write_bytes(data)
    (episode / "manifest.json").write_text(json.dumps(m))
    unrelated = episode / "do-not-delete.txt"
    unrelated.write_text("preserve")
    row = app.state.ingest.register_import(m, episode / "manifest.json", episode)
    app.state.datasets.enqueue("raw", row["collection_id"], row["dataset_name"])
    app.state.datasets.tick()
    assert unrelated.read_text() == "preserve"
    assert not (episode / "episode0/example.json").exists()
    assert not (episode / "manifest.json").exists()


def test_imported_hdf5_files_in_same_directory(settings_factory, tmp_path):
    source = tmp_path / "imports"
    source.mkdir()
    settings = settings_factory("server", import_roots=[source])
    app = create_app(settings, background=False)
    rows = []
    for i in range(2):
        m, data = manifest(settings)
        m.update(session_id=f"hdf-{i}", raw_format=ALOHA_HDF5_FORMAT)
        file = source / f"episode_{i}.hdf5"
        file.write_bytes(data)
        m["files"][0]["path"] = file.name
        rows.append(app.state.ingest.register_import(m, file, file, inline=True))
    keep = source / "notes.txt"
    keep.write_text("keep")
    app.state.datasets.enqueue("raw", rows[0]["collection_id"], rows[0]["dataset_name"])
    app.state.datasets.tick()
    assert keep.exists() and not list(source.glob("*.hdf5"))


def test_alias_symlink_and_roots_cannot_be_deleted(settings_factory, tmp_path):
    app = create_app(settings_factory("server"), background=False)
    job, path = seed_result(app)
    alias = {**job, "id": "alias", "key": "alias"}
    app.state.ingest.store.put("job", "alias", alias)
    with pytest.raises(DomainError, match="重叠"):
        app.state.datasets.enqueue("result", job["id"], job["dataset_name"])
    app.state.ingest.store.delete("job", "alias")
    renamed = path.with_name("real")
    path.rename(renamed)
    path.symlink_to(renamed, target_is_directory=True)
    with pytest.raises(DomainError):
        app.state.datasets.enqueue("result", job["id"], job["dataset_name"])
    for protected in (
        app.state.runner.settings.root,
        app.state.ingest.store.path,
        app.state.runner.settings.root / "work",
        tmp_path,
    ):
        with pytest.raises(DomainError):
            app.state.datasets._safe(protected)
    assert renamed.exists()


def test_recycle_lock_blocks_deletion(settings_factory):
    app = create_app(settings_factory("server"), background=False)
    job, path = seed_result(app)
    key = hashlib.sha256(str(path).encode()).hexdigest()
    with (
        dataset_lock(app.state.ingest.store, key),
        pytest.raises(DomainError, match="正在处理"),
    ):
        app.state.datasets.enqueue("result", job["id"], job["dataset_name"])
    assert path.exists()


def test_failure_after_rename_retries_after_restart(settings_factory, monkeypatch):
    settings = settings_factory("server")
    app = create_app(settings, background=False)
    job, path = seed_result(app)
    operation = app.state.datasets.enqueue("result", job["id"], job["dataset_name"])
    import shutil

    original = shutil.rmtree

    def fail(_):
        raise PermissionError("simulated permissions")

    monkeypatch.setattr("pipeline.server.dataset_management.shutil.rmtree", fail)
    app.state.datasets.tick()
    assert not path.exists()
    assert Path(operation["targets"][0]["trash"]).exists()
    assert app.state.ingest.store.get("job", job["id"])["state"] == "DELETE_FAILED"
    assert app.state.datasets.listing()["datasets"][0]["state"] == "DELETE_FAILED"
    monkeypatch.setattr("pipeline.server.dataset_management.shutil.rmtree", original)
    restarted = create_app(settings, background=False)
    restarted.state.datasets.retry(operation["id"])
    restarted.state.datasets.tick()
    assert (
        restarted.state.ingest.store.get("dataset_deletion", operation["id"])["state"]
        == "COMPLETED"
    )
    assert not Path(operation["targets"][0]["trash"]).exists()


def test_recover_intent_and_refuse_replaced_file(settings_factory):
    app = create_app(settings_factory("server"), background=False)
    job, path = seed_result(app)
    op = app.state.datasets.enqueue("result", job["id"], job["dataset_name"])
    # Simulate a crash between writing the plan and marking its job.
    app.state.ingest.store.put("job", job["id"], job)
    manager = DatasetManager(
        app.state.runner.settings, app.state.runner, app.state.viewer
    )
    manager.recover()
    with pytest.raises(DomainError):
        dataset_for_job(
            app.state.runner.settings, app.state.ingest.store.get("job", job["id"])
        )
    path.rename(path.with_name("old"))
    path.mkdir()
    keep = path / "new-data.txt"
    keep.write_text("new dataset")
    manager.tick()
    assert keep.exists()
    assert app.state.ingest.store.get("dataset_deletion", op["id"])["state"] == "FAILED"


def test_deleted_result_does_not_deduplicate_new_conversion(settings_factory):
    app = create_app(settings_factory("server"), background=False)
    raw = seed_raw(app)
    first = app.state.runner.enqueue(raw["id"])
    first.update(state="CONVERTED", dataset_ready=True)
    path = (
        storage_path(app.state.runner.settings, first, "work", first["id"]) / "dataset"
    )
    (path / "meta").mkdir(parents=True)
    (path / "meta/info.json").write_text("{}")
    first["dataset_path"] = str(path)
    app.state.ingest.store.put("job", first["id"], first)
    app.state.datasets.enqueue("result", first["id"], first["dataset_name"])
    app.state.datasets.tick()
    second = app.state.runner.enqueue(raw["id"])
    assert second["id"] != first["id"] and second["state"] == "QUEUED"


def test_imported_lerobot_removes_source_but_not_symlink_destination(
    settings_factory, tmp_path
):
    root = tmp_path / "imports"
    root.mkdir()
    settings = settings_factory("server", data_root=root, import_roots=[root])
    app = create_app(settings, background=False)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    dataset = root / "historical-dataset"
    (dataset / "meta").mkdir(parents=True)
    (dataset / "meta/info.json").write_text(
        json.dumps(
            {
                "codebase_version": "v3.0",
                "robot_type": "agilex_aloha",
                "total_episodes": 1,
                "total_frames": 2,
            }
        )
    )
    (dataset / "link").symlink_to(outside, target_is_directory=True)
    response = app.state.imports.import_path(str(dataset))
    job = app.state.ingest.store.get("job", response["job_id"])
    app.state.datasets.enqueue("result", job["id"], job["dataset_name"])
    app.state.datasets.tick()
    assert not dataset.exists()
    assert (outside / "keep.txt").read_text() == "keep"


def test_pending_recycle_blocks_even_if_dataset_lock_is_free(settings_factory):
    app = create_app(settings_factory("server"), background=False)
    job, path = seed_result(app)
    key = hashlib.sha256(str(path).encode()).hexdigest()
    app.state.ingest.store.put(
        "recycle", key, {"id": key, "created_at": 1, "operation": {"state": "pending"}}
    )
    with pytest.raises(DomainError, match="Episode"):
        app.state.datasets.enqueue("result", job["id"], job["dataset_name"])
    assert path.exists()


def test_incomplete_upload_cannot_be_deleted(settings_factory):
    app = create_app(settings_factory("server"), background=False)
    m, _ = manifest(app.state.runner.settings)
    raw = app.state.ingest.create(m, "default", "")
    with pytest.raises(DomainError, match="上传"):
        app.state.datasets.enqueue("raw", raw["collection_id"], raw["dataset_name"])
    assert app.state.ingest.root(raw).exists()


def test_changed_parent_cannot_be_mistaken_for_completed_deletion(settings_factory):
    app = create_app(settings_factory("server"), background=False)
    job, path = seed_result(app)
    operation = app.state.datasets.enqueue("result", job["id"], job["dataset_name"])
    # Simulate an unmounted/replaced parent, with neither the original nor the
    # temporary deletion path visible. Absence alone must not report success.
    path.parent.rename(path.parent.with_name("original-parent"))
    path.parent.mkdir()
    app.state.datasets.tick()
    result = app.state.ingest.store.get("dataset_deletion", operation["id"])
    assert result["state"] == "FAILED" and "挂载发生变化" in result["error"]


@pytest.mark.parametrize("relation", ["same", "parent", "child", "sibling"])
def test_listing_and_deletion_agree_on_shared_path_ownership(settings_factory, relation):
    app = create_app(settings_factory("server"), background=False)
    job, path = seed_result(app)
    other = {
        "same": path,
        "parent": path.parent,
        "child": path / "nested",
        "sibling": path.with_name("independent"),
    }[relation]
    # References are protected even when the other record has not produced a
    # usable result yet (e.g. an interrupted import or conversion).
    app.state.ingest.store.put(
        "job", "other", {
            "id": "other", "key": "other", "created_at": 1,
            "state": "FAILED", "dataset_path": str(other),
        },
    )
    item = next(d for d in app.state.datasets.listing()["datasets"] if d["id"] == job["id"])
    assert item["deletable"] == (relation == "sibling")
    if relation != "sibling":
        with pytest.raises(DomainError, match="重叠"):
            app.state.datasets.enqueue("result", job["id"], job["dataset_name"])
    assert path.exists()


def test_management_listing_does_not_hold_upload_or_job_admission_locks(
    settings_factory, monkeypatch,
):
    import threading

    settings = settings_factory("server")
    app = create_app(settings, background=False)
    acquired = []

    def listing():
        def concurrent_request():
            for lock in (app.state.runner.lock, app.state.ingest.lock):
                got = lock.acquire(timeout=0.2)
                acquired.append(got)
                if got:
                    lock.release()
        worker = threading.Thread(target=concurrent_request)
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        return {"datasets": [], "deletions": []}

    monkeypatch.setattr(app.state.datasets, "listing", listing)
    with TestClient(app) as client:
        response = client.get(
            "/api/datasets", headers={"Authorization": "Bearer " + settings.token},
        )
        assert response.status_code == 200
    assert acquired == [True, True]
