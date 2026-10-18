import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_ingest import manifest
from test_processing import captured, upload_sample

from pipeline.server.app import create_app
from pipeline.server.ingest import IngestService
from pipeline.server.jobs import JobRunner
from pipeline.server.storage import ROBOT_LAYOUT, storage_path
from pipeline.shared.core import DomainError, ProcessLock, digest
from scripts.migrate_server_data import migrate


def shared_settings(settings_factory, tmp_path):
    root = tmp_path / "shared"
    root.mkdir()
    return settings_factory("server", data_root=root)


@pytest.mark.parametrize("kind", ["agilex_aloha", "other_robot"])
def test_shared_upload_resume_commit_and_crash_recovery(
    settings_factory, tmp_path, kind
):
    s = shared_settings(settings_factory, tmp_path)
    ingest = IngestService(s)
    m, data = manifest(s)
    m["profile"]["robot_type"] = kind
    m["profile_hash"] = digest(m["profile"])
    row = ingest.create(m, "default", "project/test")
    expected = (
        s.data_root
        / kind
        / "raw/default/project/test/collections"
        / row["collection_id"]
        / "episodes"
        / row["id"]
    )
    assert ingest.final_dir(row) == expected
    first, rest = data[:5], data[5:]
    ingest.chunk(row["id"], 0, 0, hashlib.sha256(first).hexdigest(), first)
    ingest = IngestService(s)
    assert ingest.create(m, "default", "project/test")["id"] == row["id"]
    assert ingest.offsets(row["id"])["offsets"] == [5]
    ingest.chunk(row["id"], 0, 5, hashlib.sha256(rest).hexdigest(), rest)
    source = ingest.root(row)
    row["state"] = "COMMITTING"
    ingest.store.put("upload", row["id"], row)
    expected.parent.mkdir(parents=True)
    source.rename(expected)
    IngestService(s).reconcile()
    result = ingest.store.get("upload", row["id"])
    assert result["state"] == "COMMITTED"
    assert Path(result["raw_path"], "episode0/example.json").read_bytes() == data
    assert (s.root / "state/upload" / (row["id"] + ".json")).is_file()
    assert not (s.data_root / "state").exists()
    assert not list((s.root / "raw").iterdir())


@pytest.mark.parametrize(
    "kind", [None, "../other", "/tmp", "a/b", "..", "a\\b", "x" * 65, 1]
)
def test_robot_type_rejects_path_injection(settings_factory, tmp_path, kind):
    s = shared_settings(settings_factory, tmp_path)
    ingest = IngestService(s)
    m, _ = manifest(s)
    m["profile"]["robot_type"] = kind
    m["profile_hash"] = digest(m["profile"])
    with pytest.raises(DomainError, match="robot_type"):
        ingest.create(m, "default", "")
    assert not list(s.data_root.iterdir())


def test_shared_symlink_and_missing_mount_fail_closed(settings_factory, tmp_path):
    s = shared_settings(settings_factory, tmp_path)
    ingest = IngestService(s)
    m, _ = manifest(s)
    (s.data_root / "agilex_aloha").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(DomainError, match="符号链接"):
        ingest.create(m, "default", "")
    (s.data_root / "agilex_aloha").unlink()
    s.data_root.rmdir()
    with pytest.raises(DomainError, match="数据根目录"):
        ingest.create(m, "default", "")
    with pytest.raises(DomainError, match="数据根目录"):
        IngestService(s)


def test_quota_checks_shared_disk(settings_factory, tmp_path, monkeypatch):
    s = shared_settings(settings_factory, tmp_path)
    ingest = IngestService(s)
    m, _ = manifest(s)
    checked = []

    def disk_usage(path):
        checked.append(Path(path))
        return SimpleNamespace(
            free=1 if Path(path).is_relative_to(s.data_root) else 10**9
        )

    monkeypatch.setattr("pipeline.server.ingest.shutil.disk_usage", disk_usage)
    with pytest.raises(DomainError, match="可用空间不足"):
        ingest.create(m, "default", "")
    assert checked and all(path.is_relative_to(s.data_root) for path in checked)


def test_job_processing_logs_and_worker_recovery_on_shared_disk(
    settings_factory, tmp_path
):
    raw, m = captured(settings_factory)
    s = shared_settings(settings_factory, tmp_path)
    ingest = IngestService(s)
    uid = upload_sample(ingest, raw, m)
    runner = JobRunner(s, ingest)
    job = runner.enqueue(uid)
    runner.tick()
    result = ingest.store.get("job", job["id"])
    assert result["state"] == "DEMO_READY", result
    work = s.data_root / "agilex_aloha/work" / job["id"]
    assert Path(result["work_path"]) == work
    assert (work / "aligned").is_dir()
    (work / "convert.stderr.log").write_text("shared disk log")
    with TestClient(create_app(s, background=False)) as http:
        response = http.get(
            f"/api/jobs/{job['id']}/logs/convert",
            headers={"Authorization": "Bearer " + s.token},
        )
        assert response.json()["text"] == "shared disk log"
    # A Worker restart marks the job interrupted but keeps existing work for diagnosis.
    result.update(state="RUNNING")
    (work / "dataset").mkdir()
    (work / "dataset/example").write_text("complete")
    ingest.store.put("job", job["id"], result)
    runner.recover()
    recovered = ingest.store.get("job", job["id"])
    assert recovered["state"] == "INTERRUPTED"
    assert (work / "dataset/example").read_text() == "complete"
    assert not (s.data_root / "agilex_aloha/datasets" / job["id"]).exists()


def test_migration_retains_originals_and_resumes_partial_upload(
    settings_factory, tmp_path
):
    s = settings_factory("server")
    ingest = IngestService(s)
    m, data = manifest(s)
    complete = ingest.create(m, "default", "test")
    ingest.chunk(complete["id"], 0, 0, hashlib.sha256(data).hexdigest(), data)
    complete = ingest.complete(complete["id"])
    m["session_id"] = "partial-session"
    partial = ingest.create(m, "default", "")
    part = data[:4]
    ingest.chunk(partial["id"], 0, 0, hashlib.sha256(part).hexdigest(), part)
    job = JobRunner(s, ingest).enqueue(complete["id"])
    old_work = s.root / "work" / job["id"]
    (old_work / "dataset/meta").mkdir(parents=True)
    (old_work / "dataset/meta/info.json").write_text("{}")
    (old_work / "aligned").mkdir()
    (old_work / "aligned/example").symlink_to(
        Path(complete["raw_path"]) / "episode0/example.json"
    )
    job.update(
        state="SYNTHETIC_READY",
        work_path=str(old_work),
        dataset_path=str(old_work / "dataset"),
    )
    ingest.store.put("job", job["id"], job)
    s.data_root = tmp_path / "shared"
    s.data_root.mkdir()
    # Merely configuring data_root cannot silently redirect existing uploads.
    assert IngestService(s).offsets(partial["id"])["offsets"] == [4]
    lock = ProcessLock(s.root / "server.lock")
    lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="已有进程"):
            migrate(s)
    finally:
        lock.release()
    report = migrate(s)
    assert report["uploads"] == 2 and report["jobs"] == 1
    assert Path(complete["raw_path"], "episode0/example.json").read_bytes() == data
    assert (old_work / "dataset/meta/info.json").is_file()
    assert (Path(report["backup"]) / "before.json").is_file()
    restarted = IngestService(s)
    assert restarted.offsets(partial["id"])["offsets"] == [4]
    migrated = restarted.store.get("job", job["id"])
    assert Path(migrated["dataset_path"]).is_relative_to(s.data_root)
    assert Path(migrated["dataset_path"], "meta/info.json").is_file()
    aligned = Path(migrated["work_path"], "aligned/example")
    assert aligned.read_bytes() == data and not aligned.is_symlink()
    rest = data[4:]
    restarted.chunk(partial["id"], 0, 4, hashlib.sha256(rest).hexdigest(), rest)
    result = restarted.complete(partial["id"])
    assert result["data_layout"] == ROBOT_LAYOUT
    assert Path(result["raw_path"], "episode0/example.json").read_bytes() == data
    assert migrate(s)["uploads"] == 0


def test_migration_conflict_preserves_existing_state_and_data(
    settings_factory, tmp_path
):
    s = settings_factory("server")
    ingest = IngestService(s)
    m, data = manifest(s)
    row = ingest.create(m, "default", "")
    ingest.chunk(row["id"], 0, 0, hashlib.sha256(data).hexdigest(), data)
    s.data_root = tmp_path / "shared"
    s.data_root.mkdir()
    updated = {**row, "robot_type": "agilex_aloha", "data_layout": ROBOT_LAYOUT}
    target = storage_path(s, updated, "incoming", row["id"])
    target.mkdir(parents=True)
    (target / "unrelated").write_text("do not overwrite")
    with pytest.raises(FileExistsError):
        migrate(s)
    assert ingest.store.get("upload", row["id"]) == row
    assert (target / "unrelated").read_text() == "do not overwrite"
    assert ingest.offsets(row["id"])["offsets"] == [len(data)]


def test_migration_rejects_unrelated_alignment_symlinks(tmp_path):
    from scripts.migrate_server_data import copy_verified

    source = tmp_path / "work"
    (source / "aligned").mkdir(parents=True)
    raw = tmp_path / "raw"
    raw.mkdir()
    outside = tmp_path / "private"
    outside.write_text("not robot data")
    (source / "aligned/unrelated").symlink_to(outside)
    with pytest.raises(ValueError, match="Refusing symlink"):
        copy_verified(source, tmp_path / "target", raw)
    assert not (tmp_path / "target").exists()
