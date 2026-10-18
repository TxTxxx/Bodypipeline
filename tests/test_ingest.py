import hashlib
from pathlib import Path

import pytest

from pipeline.server.ingest import IngestService
from pipeline.shared.core import DomainError, digest


def manifest(settings, files=None):
    data = b"hello-world"
    m = {
        "manifest_version": 1,
        "complete": True,
        "session_id": "session-test-001",
        "robot_id": settings.robot_id,
        "project_id": settings.project_id,
        "control_mode": "teleoperation",
        "raw_format": "agilex_timestamped_directory_v1",
        "capture_mode": "mock",
        "instruction": "fold cloth",
        "profile": settings.profile(),
        "profile_hash": digest(settings.profile()),
        "files": files
        or [
            {
                "path": "episode0/example.json",
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        ],
    }
    return m, data


def test_chunks_resume_integrity_and_commit(settings_factory):
    s = settings_factory("server")
    ingest = IngestService(s)
    m, data = manifest(s)
    row = ingest.create(m, "default", "project/test")
    uid = row["id"]
    assert ingest.create(m, "default", "project/test")["id"] == uid
    chunk = data[:5]
    ingest.chunk(uid, 0, 0, hashlib.sha256(chunk).hexdigest(), chunk)
    assert ingest.offsets(uid)["offsets"] == [5]
    assert (
        ingest.chunk(uid, 0, 0, hashlib.sha256(chunk).hexdigest(), chunk)["offset"] == 5
    )
    with pytest.raises(DomainError):
        ingest.chunk(uid, 0, 0, hashlib.sha256(b"xxxxx").hexdigest(), b"xxxxx")
    with pytest.raises(DomainError):
        ingest.complete(uid)
    ingest = IngestService(s)
    rest = data[5:]
    ingest.chunk(uid, 0, 5, hashlib.sha256(rest).hexdigest(), rest)
    receipt = ingest.complete(uid)
    assert receipt["state"] == "COMMITTED"
    assert Path(receipt["raw_path"], "episode0/example.json").read_bytes() == data
    assert ingest.complete(uid)["manifest_hash"] == digest(m)
    assert ingest.offsets(uid)["offsets"] == [len(data)]


def test_bad_hash_and_traversal_and_identity(settings_factory):
    s = settings_factory("server")
    ingest = IngestService(s)
    for path in ["../x", "/tmp/x", "episode0/../../x", "episode0/a;sh"]:
        m, data = manifest(s)
        m["files"][0]["path"] = path
        with pytest.raises(DomainError):
            ingest.create(m, "default", "")
    m, data = manifest(s)
    m["robot_id"] = "another-robot"
    with pytest.raises(DomainError):
        ingest.create(m, "default", "")
    m, data = manifest(s)
    uid = ingest.create(m, "default", "")["id"]
    with pytest.raises(DomainError):
        ingest.chunk(uid, 0, 0, "0" * 64, data)
    assert ingest.offsets(uid)["offsets"] == [0]


def test_final_hash_detects_corruption_and_reset(settings_factory):
    s = settings_factory("server")
    ingest = IngestService(s)
    m, data = manifest(s)
    uid = ingest.create(m, "default", "")["id"]
    wrong = b"x" * len(data)
    ingest.chunk(uid, 0, 0, hashlib.sha256(wrong).hexdigest(), wrong)
    with pytest.raises(DomainError):
        ingest.complete(uid)
    ingest.reset_file(uid, 0)
    ingest.chunk(uid, 0, 0, hashlib.sha256(data).hexdigest(), data)
    assert ingest.complete(uid)["state"] == "COMMITTED"


def test_reconcile_rename_before_state_file_commit(settings_factory):
    s = settings_factory("server")
    ingest = IngestService(s)
    m, data = manifest(s)
    row = ingest.create(m, "default", "")
    ingest.chunk(row["id"], 0, 0, hashlib.sha256(data).hexdigest(), data)
    src = ingest.root(row)
    dest = ingest.final_dir(row)
    dest.parent.mkdir(parents=True, exist_ok=True)
    row["state"] = "COMMITTING"
    ingest.store.put("upload", row["id"], row)
    src.rename(dest)
    second = IngestService(s)
    second.reconcile()
    assert second.store.get("upload", row["id"])["state"] == "COMMITTED"
