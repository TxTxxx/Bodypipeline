import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pipeline.server.app import create_app
from pipeline.server.datasets import dataset_for_job
from pipeline.server.merges import source_snapshot
from pipeline.server.recycle import dataset_lock
from pipeline.server.storage import share_data_tree
from pipeline.shared.core import DomainError

SCENARIO = Path(__file__).parent / "fixtures/merge_scenario.py"


@pytest.fixture
def merge_app(settings_factory, monkeypatch):
    settings = settings_factory("server", letools_bin=sys.executable)
    monkeypatch.setattr(
        "pipeline.server.merges.executable_path", lambda _: sys.executable
    )
    return create_app(settings, background=False)


def seed(app, jid="first", **updates):
    settings = app.state.runner.settings
    path = settings.root / "work" / jid / "dataset"
    (path / "meta").mkdir(parents=True)
    info = {
        "codebase_version": "v3.0",
        "robot_type": "test_arm",
        "fps": 10,
        "total_episodes": 2,
        "total_frames": 11,
        "splits": {"train": "0:2"},
        "features": {
            "action": {
                "dtype": "float32",
                "shape": [2],
                "names": ["a", "b"],
                "units": ["rad", "rad"],
            }
        },
        **updates,
    }
    (path / "meta/info.json").write_text(json.dumps(info))
    job = {
        "id": jid,
        "key": jid,
        "state": "CONVERTED",
        "dataset_name": jid,
        "robot_type": info["robot_type"],
        "created_at": 1,
        "dataset_ready": True,
        "dataset_path": str(path),
    }
    app.state.ingest.store.put("job", jid, job)
    return job, path


def test_merge_api_auth_validation_snapshot_and_inflight_dedup(merge_app):
    app = merge_app
    seed(app)
    seed(app, "second", fps=10.0)
    admin = {"Authorization": "Bearer " + app.state.runner.settings.token}
    body = {"name": "  新  数据集  ", "source_job_ids": ["second", "first"]}
    with TestClient(app) as client:
        assert client.get("/api/merge-candidates").status_code == 401
        assert client.post("/api/merges", json=body).status_code == 401
        device = {"Authorization": "Bearer " + app.state.runner.settings.device_token}
        assert client.post("/api/merges", json=body, headers=device).status_code == 401
        assert all(
            s["selectable"]
            for s in client.get("/api/merge-candidates", headers=admin).json()
        )
        for invalid in (
            {**body, "source_job_ids": ["first"]},
            {**body, "destination": "/tmp/escape"},
        ):
            assert (
                client.post("/api/merges", json=invalid, headers=admin).status_code
                == 422
            )
        for invalid in (
            {**body, "source_job_ids": ["first", "first"]},
            {**body, "name": "  "},
        ):
            assert (
                client.post("/api/merges", json=invalid, headers=admin).status_code
                == 400
            )
        assert (
            client.post(
                "/api/merges",
                json={**body, "source_job_ids": ["first", "missing"]},
                headers=admin,
            ).status_code
            == 404
        )
        response = client.post("/api/merges", json=body, headers=admin)
        assert response.status_code == 202
        job = response.json()
        assert job["source_job_ids"] == ["second", "first"]
        assert job["dataset_name"] == "新 数据集"
        assert job["operation"] == "merge" and job["source_count"] == 4
        assert not job["dataset_available"]
        assert (
            client.post("/api/merges", json=body, headers=admin).json()["id"]
            == job["id"]
        )
        assert (
            client.get("/api/jobs/" + job["id"], headers=admin).json()["state"]
            == "QUEUED"
        )


@pytest.mark.parametrize(
    "change",
    [
        {"codebase_version": "v2.1"},
        {"fps": 20},
        {"robot_type": "other_arm"},
        {"features": {"action": {"dtype": "float32", "shape": [3]}}},
        {
            "features": {
                "action": {
                    "dtype": "float32",
                    "shape": [2],
                    "names": ["b", "a"],
                    "units": ["rad", "rad"],
                }
            }
        },
        {
            "features": {
                "action": {
                    "dtype": "float32",
                    "shape": [2],
                    "names": ["a", "b"],
                    "units": ["deg", "deg"],
                }
            }
        },
        {"splits": {"test": "0:2"}},
        {"synthetic": True},
    ],
)
def test_merge_rejects_incompatible_inputs(merge_app, change):
    seed(merge_app)
    seed(merge_app, "second", **change)
    with pytest.raises(DomainError, match="不兼容"):
        merge_app.state.runner.merges.enqueue(["first", "second"], "merged")
    assert len(merge_app.state.ingest.store.all("job")) == 2


def test_merge_rejects_aliases_links_recycle_and_locked_sources(merge_app):
    app = merge_app
    first, path = seed(app)
    second, _ = seed(app, "second")
    store, merges = app.state.ingest.store, app.state.runner.merges
    store.put("job", "alias", {**first, "id": "alias"})
    with pytest.raises(DomainError, match="同一路径"):
        merges.enqueue(["first", "alias"], "merged")
    key = merges._key(path)
    with dataset_lock(store, key), pytest.raises(DomainError, match="正在处理"):
        merges.enqueue(["first", "second"], "merged")
    store.put("recycle", key, {"entries": {"0": {"episode": 0}}})
    assert not next(r for r in merges.candidates() if r["id"] == "first")["selectable"]
    with pytest.raises(DomainError, match="回收站"):
        merges.enqueue(["first", "second"], "merged")
    store.delete("recycle", key)
    (path / "linked").symlink_to(path / "meta/info.json")
    with pytest.raises(DomainError, match="符号链接"):
        merges.enqueue(["first", "second"], "merged")
    (path / "linked").unlink()
    second["state"] = "QUEUED"
    second["dataset_ready"] = False
    store.put("job", "second", second)
    with pytest.raises(DomainError, match="尚无"):
        merges.enqueue(["first", "second"], "merged")


def test_queued_merge_rejects_changes_and_recovery_is_explicit(merge_app):
    app = merge_app
    _, path = seed(app)
    seed(app, "second")
    runner = app.state.runner
    job = runner.merges.enqueue(["first", "second"], "merged")
    (path / "meta/changed.json").write_text("{}")
    assert runner.tick()
    result = runner.store.get("job", job["id"])
    assert result["state"] == "FAILED" and "发生变化" in result["error"]
    with pytest.raises(DomainError):
        dataset_for_job(runner.settings, result)
    retry = runner.merges.enqueue(["first", "second"], "merged")
    assert retry["id"] != job["id"]
    runner.save(retry, state="RUNNING")
    runner.recover()
    assert runner.store.get("job", retry["id"])["state"] == "INTERRUPTED"
    assert not runner.tick()


@pytest.mark.parametrize("interrupted", [False, True])
def test_merge_failure_never_publishes_and_releases_allocation(
    merge_app, monkeypatch, interrupted
):
    runner = merge_app.state.runner
    _, path = seed(merge_app)
    seed(merge_app, "second")
    before = source_snapshot(path)
    opened, closed = [], []
    monkeypatch.setattr(
        runner, "_open_allocation", lambda work: opened.append(work) or "allocation-42"
    )
    monkeypatch.setattr(runner, "_close_allocation", lambda: closed.append(True))

    def command(args, work, name, allocation=None):
        assert allocation == "allocation-42" and name == "merge"
        with pytest.raises(DomainError, match="正在处理"):
            with dataset_lock(runner.store, runner.merges._key(path)):
                pass
        if interrupted:
            raise InterruptedError("shutdown")
        report, err = work / "merge.json", work / "merge.stderr.log"
        report.write_text("")
        err.write_text("invalid source")
        return 1, report, err

    monkeypatch.setattr(runner, "command", command)
    job = runner.merges.enqueue(["first", "second"], "merged")
    runner.tick()
    result = runner.store.get("job", job["id"])
    assert result["state"] == ("INTERRUPTED" if interrupted else "FAILED")
    assert opened and len(closed) == 1
    assert source_snapshot(path) == before
    assert not (Path(result["work_path"]) / "dataset").exists()


def test_merge_checks_space_before_allocating(merge_app, monkeypatch):
    seed(merge_app)
    seed(merge_app, "second")
    runner = merge_app.state.runner
    monkeypatch.setattr(
        "pipeline.server.merges.shutil.disk_usage", lambda _: SimpleNamespace(free=0)
    )
    monkeypatch.setattr(
        runner, "_open_allocation", lambda _: pytest.fail("must not allocate")
    )
    job = runner.merges.enqueue(["first", "second"], "merged")
    runner.tick()
    result = runner.store.get("job", job["id"])
    assert result["state"] == "FAILED" and "空间不足" in result["error"]
    assert not (Path(result["work_path"]) / "dataset").exists()


@pytest.mark.parametrize("failure", ["source_changed", "counts", "permissions"])
def test_merge_publication_requires_unchanged_sources_valid_counts_and_permissions(
    merge_app, monkeypatch, failure
):
    runner = merge_app.state.runner
    _, path = seed(merge_app)
    seed(merge_app, "second")

    def command(args, work, name, allocation=None):
        pending = work / "pending-dataset"
        (pending / "meta").mkdir(parents=True)
        (pending / "meta/info.json").write_text(
            json.dumps(
                {
                    "total_episodes": 3 if failure == "counts" else 4,
                    "total_frames": 22,
                    "codebase_version": "v3.0",
                }
            )
        )
        report, err = work / "merge.json", work / "merge.stderr.log"
        report.write_text('{"validated": true}')
        err.write_text("")
        if failure == "source_changed":
            (path / "changed.bin").write_bytes(b"changed externally")
        return 0, report, err

    def share(work):
        if failure == "permissions" and (work / "pending-dataset").exists():
            raise PermissionError("cannot grant shared permissions")
        share_data_tree(work)

    monkeypatch.setattr(runner, "command", command)
    monkeypatch.setattr("pipeline.server.merges.share_data_tree", share)
    job = runner.merges.enqueue(["first", "second"], "merged")
    runner.tick()
    result = runner.store.get("job", job["id"])
    assert result["state"] == "FAILED"
    assert not result.get("dataset_ready")
    assert not (Path(result["work_path"]) / "dataset").exists()
    with pytest.raises(DomainError):
        dataset_for_job(runner.settings, result)


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
def test_real_merge_keeps_sources_tasks_videos_and_shared_output(
    settings_factory, tmp_path, letools_binary, version
):
    python = str(Path(letools_binary).with_name("python"))
    fixtures = tmp_path / "fixtures"
    subprocess.run(
        [python, str(SCENARIO), "create", str(fixtures), version], check=True
    )
    data_root = tmp_path / "shared"
    data_root.mkdir()
    settings = settings_factory(
        "server",
        letools_bin=letools_binary,
        data_root=data_root,
        import_roots=[fixtures],
    )
    app = create_app(settings, background=False)
    runner = app.state.runner
    originals = {}
    for ordinal in range(2):
        path = fixtures / f"input-{ordinal}"
        originals[str(path)] = {
            str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob("*")
            if p.is_file()
        }
    admin = {"Authorization": "Bearer " + settings.token}
    with TestClient(app) as client:
        ids = []
        for ordinal in range(2):
            response = client.post(
                "/api/imports",
                headers=admin,
                json={"path": str(fixtures / f"input-{ordinal}")},
            )
            assert response.status_code == 201, response.text
            ids.append(response.json()["job_id"])
        # Imported results remain mergeable in demo conversion mode; a merge
        # always runs the real LeTools engine rather than producing fake data.
        response = client.post(
            "/api/merges",
            headers=admin,
            json={"name": "new training set", "source_job_ids": ids[::-1]},
        )
        assert response.status_code == 202, response.text
        job = response.json()
        old_umask = os.umask(0o077)
        try:
            assert runner.tick()
        finally:
            os.umask(old_umask)
        result = client.get("/api/jobs/" + job["id"], headers=admin).json()
        assert result["state"] == "CONVERTED", result
        output = Path(result["dataset_path"])
        assert output == data_root / "test_arm/work" / job["id"] / "dataset"
        assert result["dataset_available"] and result["episode_count"] == 4
        inspected = json.loads(
            subprocess.check_output(
                [python, str(SCENARIO), "inspect", str(output)], text=True
            )
        )
        assert inspected["indices"] == [0, 1, 2, 3]
        assert inspected["episodes"] == 4 and inspected["frames"] == 22
        assert inspected["tasks"] == [["place"], ["pick"], ["pick"], ["place"]]
        assert inspected["rows"][0][0]["action"][0] == pytest.approx(1.05)
        assert inspected["rows"][2][0]["action"][0] == pytest.approx(0.05)
        lineage = json.loads((output / "meta/bodypipeline-merge.json").read_text())
        assert [s["episode_start"] for s in lineage["sources"]] == [0, 2]
        assert [s["episode_end"] for s in lineage["sources"]] == [2, 4]
        assert [s["job_id"] for s in lineage["sources"]] == ids[::-1]
        for path, hashes in originals.items():
            root = Path(path)
            assert {
                str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in root.rglob("*")
                if p.is_file()
            } == hashes
        input_video_hashes = {
            hashlib.sha256(p.read_bytes()).hexdigest()
            for root in originals
            for p in Path(root).rglob("*.mp4")
        }
        assert {
            hashlib.sha256(p.read_bytes()).hexdigest() for p in output.rglob("*.mp4")
        } == input_video_hashes
        for path in [output, *output.rglob("*")]:
            mode = stat.S_IMODE(path.stat().st_mode)
            access = 0o777 if path.is_dir() else 0o666
            assert mode & access == access, (path, oct(mode))
        for path in (
            output.parent,
            output.parent.parent,
            data_root / "test_arm",
            data_root,
        ):
            assert stat.S_IMODE(path.stat().st_mode) & 0o777 == 0o777
        state = settings.root / "state/job" / (job["id"] + ".json")
        assert stat.S_IMODE(state.stat().st_mode) == 0o600
        assert (
            client.post(f"/api/jobs/{job['id']}/checks", headers=admin).status_code
            == 202
        )
        candidates = client.get("/api/merge-candidates", headers=admin).json()
        assert next(c for c in candidates if c["id"] == job["id"])["selectable"]
        assert any(
            j["id"] == job["id"] and j["dataset_available"]
            for j in client.get("/api/jobs", headers=admin).json()
        )
        preview = app.state.viewer.registry.register(job["id"], output)
        assert preview["dataset_path"] == str(output)
