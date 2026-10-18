import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pipeline.client.service import CaptureService
from pipeline.shared.core import DomainError, ProcessLock, Store, atomic_json
from scripts.migrate_state import migrate


def test_plain_files_restart_and_queue_order(tmp_path):
    root = tmp_path / "nested" / "state"
    store = Store(root)
    old = {"id": "old", "created_at": 1, "state": "QUEUED"}
    new = {"id": "new", "created_at": 2, "state": "QUEUED"}
    store.put("job", "old", old)
    store.put("job", "new", new)
    old["progress"] = 3
    store.put("job", "old", old)
    assert json.loads((root / "job/old.json").read_text()) == old
    restarted = Store(root)
    assert restarted.all("job") == [new, old]
    assert restarted.get("job", "old") == old
    restarted.delete("job", "old")
    restarted.delete("job", "old")
    with pytest.raises(DomainError) as missing:
        store.get("job", "old")
    assert missing.value.code == 404
    assert not list(tmp_path.rglob("*.sqlite*"))


def test_interrupted_replace_keeps_old_record(tmp_path, monkeypatch):
    store = Store(tmp_path / "state")
    store.put("capture", "one", {"state": "STARTING"})

    def power_loss(*args):
        raise OSError("simulated failure before rename")

    monkeypatch.setattr("pipeline.shared.core.os.replace", power_loss)
    with pytest.raises(OSError):
        store.put("capture", "one", {"state": "RECORDING"})
    # Uncommitted temporary files left by a killed process cannot become records.
    (store.path / "capture/.one.json.abandoned.tmp").write_text('{"state":')
    assert Store(store.path).all("capture") == [{"state": "STARTING"}]


def test_separate_reader_never_observes_partial_json(tmp_path):
    path = tmp_path / "state/viewer/active.json"
    atomic_json(path, {"sequence": 0, "payload": "x" * 8000})
    reader = Store(path.parent.parent)

    def write():
        for sequence in range(1, 60):
            atomic_json(path, {"sequence": sequence, "payload": "x" * 8000})

    def read():
        for _ in range(250):
            value = reader.get("viewer", "active")
            assert 0 <= value["sequence"] < 60
            assert value["payload"] == "x" * 8000

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(write), pool.submit(read), pool.submit(read)]
        for future in futures:
            future.result()


@pytest.mark.parametrize("content", ['{"state":', "[]", '{"x":NaN}', '{"id":"other"}'])
def test_corrupt_history_blocks_new_capture(settings_factory, content):
    settings = settings_factory()
    service = CaptureService(settings)
    path = service.store.path / "capture/unknown.json"
    path.parent.mkdir()
    path.write_text(content)
    with pytest.raises(DomainError) as corrupt:
        service.recover()
    assert corrupt.value.code == 500
    with pytest.raises(DomainError):
        service.create({"idempotency_key": "new-request"})
    assert path.read_text() == content


@pytest.mark.parametrize(
    "kind,key",
    [
        ("../escape", "x"),
        ("capture", "../x"),
        ("capture", "/tmp/x"),
        ("capture", "x/y"),
    ],
)
def test_state_path_rejects_traversal(tmp_path, kind, key):
    store = Store(tmp_path / "state")
    with pytest.raises(DomainError):
        store.put(kind, key, {})


def test_state_paths_reject_symlinks(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    store = Store(tmp_path / "state")
    (store.path / "capture").symlink_to(outside, target_is_directory=True)
    with pytest.raises(DomainError):
        store.put("capture", "one", {})
    assert not list(outside.iterdir())
    (store.path / "capture").unlink()
    (store.path / "capture").mkdir()
    secret = outside / "secret.json"
    secret.write_text('{"secret":true}')
    (store.path / "capture/one.json").symlink_to(secret)
    with pytest.raises(DomainError):
        store.get("capture", "one")
    with pytest.raises(DomainError):
        store.all("capture")


def test_failed_directory_scan_never_looks_like_empty_history(tmp_path, monkeypatch):
    store = Store(tmp_path / "state")
    (store.path / "capture").write_text("not a directory")
    with pytest.raises(DomainError) as invalid:
        store.all("capture")
    assert invalid.value.code == 500

    def denied(*args):
        raise PermissionError("unreadable directory")

    monkeypatch.setattr(Path, "iterdir", denied)
    with pytest.raises(DomainError) as unreadable:
        store.all("capture")
    assert unreadable.value.code == 500


def legacy_file(root, content='{"id":"one","state":"RECORDING"}'):
    root.mkdir(parents=True, exist_ok=True)
    source = root / "client.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE records(kind TEXT, id TEXT, data TEXT)")
        connection.execute("INSERT INTO records VALUES ('capture','one',?)", (content,))
    return source


def test_offline_migration_preserves_history_and_original(tmp_path):
    source = legacy_file(tmp_path)
    before = source.read_bytes()
    with pytest.raises(RuntimeError, match="migrate_state"):
        Store(tmp_path / "state", legacy_name="client.sqlite")
    assert not (tmp_path / "state").exists()
    report = migrate(tmp_path, "client")
    assert report["counts"] == {"capture": 1}
    store = Store(tmp_path / "state", legacy_name="client.sqlite")
    assert store.get("capture", "one")["state"] == "RECORDING"
    assert source.read_bytes() == before
    store.put("capture", "one", {"id": "one", "state": "RECOVERY_REQUIRED"})
    with pytest.raises(FileExistsError):
        migrate(tmp_path, "client")
    assert store.get("capture", "one")["state"] == "RECOVERY_REQUIRED"


def test_migration_requires_stopped_service_and_is_all_or_nothing(tmp_path):
    legacy_file(tmp_path, content="not JSON")
    lock = ProcessLock(tmp_path / "client.lock")
    lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="已有进程"):
            migrate(tmp_path, "client")
    finally:
        lock.release()
    with pytest.raises(ValueError):
        migrate(tmp_path, "client")
    assert not (tmp_path / "state").exists()
    assert not list(tmp_path.glob(".state-import-*"))
