import hashlib
import json
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from test_aloha_hdf5_import import hdf5_settings, write_episode
from test_ingest import manifest

from pipeline.server.imports import ImportService
from pipeline.server.ingest import IngestService
from pipeline.server.jobs import JobRunner
from pipeline.server.providers.aloha_hdf5 import AlohaHDF5Provider
from pipeline.server.recycle_worker import MARKER, run
from pipeline.server.storage import share_data_tree
from pipeline.shared.core import Store, atomic_json


@contextmanager
def private_umask():
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def assert_shared_tree(root):
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        assert mode(path) == (0o777 if path.is_dir() else 0o666), path


@pytest.mark.parametrize("shared_root", [False, True])
def test_upload_is_shared_despite_private_umask_and_commit_recovery(
    settings_factory, tmp_path, shared_root
):
    with private_umask():
        data_root = tmp_path / "data" if shared_root else None
        if data_root:
            data_root.mkdir()
        settings = settings_factory("server", data_root=data_root)
        ingest = IngestService(settings)
        m, data = manifest(settings)
        row = ingest.create(m, "default", "project/test")
        incoming = ingest.root(row)
        assert_shared_tree(incoming)
        ingest.chunk(row["id"], 0, 0, hashlib.sha256(data).hexdigest(), data)
        assert_shared_tree(incoming)

        # Recover a crash after rename, before COMMITTED is persisted.
        final = ingest.final_dir(row)
        final.parent.mkdir(parents=True)
        row["state"] = "COMMITTING"
        ingest.store.put("upload", row["id"], row)
        incoming.rename(final)
        IngestService(settings).reconcile()
        result = ingest.store.get("upload", row["id"])
        assert result["state"] == "COMMITTED"
        assert_shared_tree(final)
        base = data_root or settings.root
        for parent in final.parents:
            if parent == base:
                break
            assert mode(parent) == 0o777, parent
        assert mode(base) == (0o777 if shared_root else 0o711)
        assert mode(settings.root / "state/upload" / f"{row['id']}.json") == 0o600
        assert mode(settings.root / "state") == 0o700


def test_conversion_shares_provider_metadata_before_dataset_ready(
    settings_factory, tmp_path, monkeypatch
):
    with private_umask():
        settings = hdf5_settings(settings_factory, tmp_path)
        settings.processing_mode = "real"
        settings.letools_bin = sys.executable
        source = tmp_path / "original"
        source.mkdir()
        episode = write_episode(source / "episode_0.hdf5")
        ingest = IngestService(settings)
        imported = ImportService(settings, ingest).import_path(str(source), "ARX_X5")
        runner = JobRunner(settings, ingest)
        job = runner.enqueue_collection(
            imported["collection_id"], "unscrew", run_doctor=False
        )
        patched = []
        original_patch = AlohaHDF5Provider.patch_dataset_info

        def patch(self, dataset, profile, **kwargs):
            original_patch(self, dataset, profile, **kwargs)
            patched.append(mode(dataset / "meta/info.json"))

        def command(args, work, name, allocation=None):
            output, error = work / f"{name}.json", work / f"{name}.stderr.log"
            output.write_text("{}")
            error.write_text("")
            if name == "convert":
                atomic_json(
                    work / "dataset/meta/info.json",
                    {
                        "total_episodes": 1,
                        "total_frames": 4,
                        "features": {"action": {}, "observation.state": {}},
                    },
                )
                (work / "dataset/data").mkdir()
                (work / "dataset/data/example.parquet").write_bytes(b"example")
            if name == "validate":
                assert_shared_tree(work / "dataset")
                assert not job.get("dataset_ready")
            return 0, output, error

        monkeypatch.setattr(AlohaHDF5Provider, "patch_dataset_info", patch)
        monkeypatch.setattr(runner, "command", command)
        runner.run(job)
        result = ingest.store.get("job", job["id"])
        assert result["state"] == "CONVERTED", result
        assert result["dataset_ready"]
        assert patched == [0o600]  # Exercise the real provider's atomic rewrite.
        assert_shared_tree(Path(result["dataset_path"]))
        assert mode(Path(result["work_path"]) / "lineage.json") == 0o666
        info = json.loads(Path(result["dataset_path"], "meta/info.json").read_text())
        assert info["features"]["action"]["names"]
        # An imported source is read-only input to the pipeline, not owned data.
        assert mode(source) == 0o700 and mode(episode) == 0o600
        assert mode(settings.root / "state/job" / f"{job['id']}.json") == 0o600


def test_sharing_does_not_follow_alignment_links_or_change_private_json(tmp_path):
    with private_umask():
        private = tmp_path / "private.json"
        atomic_json(private, {"token": "private"})
        data = tmp_path / "work"
        data.mkdir()
        (data / "aligned").symlink_to(tmp_path, target_is_directory=True)
        (data / "frame.json").symlink_to(private)
        executable = data / "tool"
        executable.write_text("#!/bin/sh\n")
        executable.chmod(0o700)
        share_data_tree(data)
        assert mode(data) == mode(executable) == 0o777
        assert mode(private) == 0o600
        assert mode(tmp_path) != 0o777


def test_recycle_preserves_shared_access_for_new_metadata(tmp_path, monkeypatch):
    with private_umask():
        root = tmp_path / "dataset"
        atomic_json(root / "meta/info.json", {"total_episodes": 2})
        share_data_tree(root)
        store = Store(tmp_path / "state")
        row = {
            "id": "shared",
            "dataset_path": str(root),
            "revision": 0,
            "entries": {"1": {"episode": 1}},
            "operation": {
                "id": "cleanup",
                "state": "pending",
                "episodes": [1],
                "entries_before": {"1": {"episode": 1}},
            },
        }
        store.put("recycle", row["id"], row)

        def rebuild(source, stage, episodes, progress):
            atomic_json(stage / "meta/info.json", {"total_episodes": 1})
            atomic_json(stage / "meta/new.json", {"new": True})
            return {"mapping": {"0": 0}, "total_episodes": 1, "deleted_frames": 4}

        def exchange(left, right):
            # Test permission publication on macOS without Linux renameat2.
            assert_shared_tree(right)
            saved = left.with_name("old-dataset")
            left.rename(saved)
            right.rename(left)
            saved.rename(right)

        monkeypatch.setattr("pipeline.server.recycle_worker.rebuild", rebuild)
        monkeypatch.setattr("pipeline.server.recycle_worker._exchange", exchange)
        run(store, row["id"], "cleanup")
        assert store.get("recycle", row["id"])["operation"]["state"] == "completed"
        assert_shared_tree(root)
        assert mode(root / MARKER) == 0o666
        assert mode(store.path / "recycle/shared.json") == 0o600
