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
from pipeline.server.quality import DOCTOR_REQUIRED_CHECKS
from pipeline.server.recycle import dataset_lock
from pipeline.shared.core import DomainError

SCENARIO = Path(__file__).parent / "fixtures/merge_scenario.py"


def admin(settings):
    return {"Authorization": "Bearer " + settings.token}


def seed(app, jid="source", version="v3.0", **updates):
    settings = app.state.runner.settings
    path = settings.root / "work" / jid / "dataset"
    (path / "meta").mkdir(parents=True)
    info = {
        "codebase_version": version,
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
        "dataset_name": "source dataset",
        "robot_type": info["robot_type"],
        "created_at": 1,
        "dataset_ready": True,
        "dataset_path": str(path),
        "target_format": version,
        "episode_count": 2,
        "frame_count": 11,
    }
    app.state.ingest.store.put("job", jid, job)
    return job, path


def test_version_conversion_api_auth_validation_and_dedup(
    settings_factory,
):
    settings = settings_factory("server", letools_bin=sys.executable)
    app = create_app(settings, background=False)
    seed(app)
    with TestClient(app) as client:
        auth = admin(settings)
        assert client.get("/api/version-conversion-candidates").status_code == 401
        assert (
            client.post(
                "/api/version-conversions",
                json={"source_job_id": "source", "target_format": "v2.1"},
            ).status_code
            == 401
        )
        device = {"Authorization": "Bearer " + settings.device_token}
        assert (
            client.get("/api/version-conversion-candidates", headers=device).status_code
            == 401
        )
        candidates = client.get(
            "/api/version-conversion-candidates", headers=auth
        ).json()
        candidate = next(row for row in candidates if row["source_job_id"] == "source")
        assert candidate["selectable"]
        assert candidate["source_format"] == "v3.0"
        assert candidate["target_format"] == "v2.1"
        assert candidate["episode_count"] == 2
        assert (
            client.post(
                "/api/version-conversions",
                headers=auth,
                json={"source_job_id": "source", "target_format": "v3.0"},
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/api/version-conversions",
                headers=auth,
                json={"source_job_id": "missing", "target_format": "v2.1"},
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/version-conversions",
                headers=auth,
                json={
                    "source_job_id": "source",
                    "target_format": "v2.1",
                    "destination": "/tmp/escape",
                },
            ).status_code
            == 422
        )
        body = {
            "source_job_id": "source",
            "target_format": "v2.1",
            "name": "source v2.1",
            "run_doctor": False,
        }
        response = client.post("/api/version-conversions", headers=auth, json=body)
        assert response.status_code == 202, response.text
        job = response.json()
        assert job["operation"] == "version_conversion"
        assert job["source_format"] == "v3.0"
        assert job["target_format"] == "v2.1"
        assert job["dataset_name"] == "source v2.1"
        assert job["source_count"] == job["episode_count"] == 2
        assert job["capture_mode"] == "version_conversion"
        assert not job["dataset_available"]
        assert (
            client.post("/api/version-conversions", headers=auth, json=body).json()[
                "id"
            ]
            == job["id"]
        )
        default_name = client.post(
            "/api/version-conversions",
            headers=auth,
            json={
                "source_job_id": "source",
                "target_format": "v2.1",
                "run_doctor": False,
            },
        )
        assert default_name.status_code == 202
        assert default_name.json()["dataset_name"] == "source dataset · v2.1"


def test_version_conversion_candidates_block_recycle_and_invalid_metadata(
    settings_factory,
):
    settings = settings_factory("server", letools_bin=sys.executable)
    app = create_app(settings, background=False)
    _, path = seed(app)
    runner = app.state.runner
    key = runner.version_conversions._path_key(path)
    runner.store.put("recycle", key, {"entries": {"0": {"episode": 0}}})
    candidate = next(
        row
        for row in runner.version_conversions.candidates()
        if row["source_job_id"] == "source"
    )
    assert not candidate["selectable"]
    assert "回收站" in candidate["reason"]
    runner.store.delete("recycle", key)
    info_path = path / "meta/info.json"
    info = json.loads(info_path.read_text())
    info["codebase_version"] = "v3.1"
    info_path.write_text(json.dumps(info))
    candidate = runner.version_conversions.candidates()[0]
    assert not candidate["selectable"]
    assert "v2.1 / v3.0" in candidate["reason"]


def test_version_conversion_uses_registered_robot_type_when_metadata_omits_it(
    settings_factory,
):
    settings = settings_factory("server", letools_bin=sys.executable)
    app = create_app(settings, background=False)
    job, path = seed(app)
    info_path = path / "meta/info.json"
    info = json.loads(info_path.read_text())
    info.pop("robot_type")
    info_path.write_text(json.dumps(info))
    job["robot_type"] = "test_arm"
    app.state.ingest.store.put("job", job["id"], job)
    candidate = app.state.runner.version_conversions.candidates()[0]
    assert candidate["selectable"]
    assert candidate["robot_type"] == "test_arm"


def test_version_conversion_failure_releases_lock_allocation_and_never_publishes(
    settings_factory,
    monkeypatch,
):
    settings = settings_factory("server", letools_bin=sys.executable)
    app = create_app(settings, background=False)
    _, source_path = seed(app)
    runner = app.state.runner
    conversions = runner.version_conversions
    before = source_snapshot(source_path)
    opened, closed = [], []
    monkeypatch.setattr(
        runner,
        "_open_allocation",
        lambda work: opened.append(work) or "allocation-42",
    )
    monkeypatch.setattr(runner, "_close_allocation", lambda: closed.append(True))

    def command(args, work, name, allocation=None):
        assert allocation == "allocation-42"
        output, error = work / f"{name}.json", work / f"{name}.stderr.log"
        output.write_text("")
        error.write_text("")
        if name == "environment":
            return 0, output, error
        if name == "source-validate":
            with pytest.raises(DomainError, match="正在处理"):
                with dataset_lock(runner.store, conversions._path_key(source_path)):
                    pass
            return 0, output, error
        error.write_text("conversion failed")
        return 1, output, error

    monkeypatch.setattr(runner, "command", command)
    job = conversions.enqueue("source", "v2.1", run_doctor=False)
    runner.tick()
    result = runner.store.get("job", job["id"])
    assert result["state"] == "FAILED"
    assert "版本转换失败" in result["error"]
    assert opened and closed
    assert source_snapshot(source_path) == before
    assert not result.get("dataset_ready")
    with pytest.raises(DomainError):
        dataset_for_job(settings, result)


def test_version_conversion_detects_source_changes_and_checks_space(
    settings_factory,
    monkeypatch,
):
    settings = settings_factory("server", letools_bin=sys.executable)
    app = create_app(settings, background=False)
    _, source_path = seed(app)
    runner = app.state.runner
    conversions = runner.version_conversions
    monkeypatch.setattr(
        "pipeline.server.version_conversions.shutil.disk_usage",
        lambda _: SimpleNamespace(free=0),
    )
    monkeypatch.setattr(
        runner, "_open_allocation", lambda _: pytest.fail("must not allocate")
    )
    job = conversions.enqueue("source", "v2.1", run_doctor=False)
    runner.tick()
    result = runner.store.get("job", job["id"])
    assert result["state"] == "FAILED" and "空间不足" in result["error"]

    monkeypatch.setattr(
        "pipeline.server.version_conversions.shutil.disk_usage",
        lambda _: SimpleNamespace(free=10**12),
    )
    opened, closed = [], []
    monkeypatch.setattr(
        runner,
        "_open_allocation",
        lambda work: opened.append(work) or "allocation-43",
    )
    monkeypatch.setattr(runner, "_close_allocation", lambda: closed.append(True))

    def command(args, work, name, allocation=None):
        output, error = work / f"{name}.json", work / f"{name}.stderr.log"
        output.write_text("")
        error.write_text("")
        if name == "convert":
            dataset = work / "dataset"
            (dataset / "meta").mkdir(parents=True)
            (dataset / "meta/info.json").write_text(
                json.dumps(
                    {
                        "codebase_version": "v2.1",
                        "total_episodes": 2,
                        "total_frames": 11,
                    }
                )
            )
            (source_path / "changed.bin").write_text("changed")
        if name == "doctor":
            output.write_text("{}")
        return 0, output, error

    monkeypatch.setattr(runner, "command", command)
    second = conversions.enqueue("source", "v2.1", name="second", run_doctor=False)
    runner.tick()
    result = runner.store.get("job", second["id"])
    assert result["state"] == "FAILED"
    assert "来源发生变化" in result["error"]
    assert not result.get("dataset_ready")
    assert opened and closed


def test_version_conversion_runs_doctor_in_the_same_allocation(
    settings_factory,
    monkeypatch,
):
    settings = settings_factory("server", letools_bin=sys.executable)
    app = create_app(settings, background=False)
    seed(app)
    runner = app.state.runner
    conversions = runner.version_conversions
    opened, closed = [], []
    monkeypatch.setattr(
        runner,
        "_open_allocation",
        lambda work: opened.append(work) or "allocation-44",
    )
    monkeypatch.setattr(runner, "_close_allocation", lambda: closed.append(True))
    calls = []

    def command(args, work, name, allocation=None):
        assert allocation == "allocation-44"
        calls.append(name)
        output, error = work / f"{name}.json", work / f"{name}.stderr.log"
        error.write_text("")
        if name == "convert":
            dataset = work / "dataset"
            (dataset / "meta").mkdir(parents=True)
            (dataset / "meta/info.json").write_text(
                json.dumps(
                    {
                        "codebase_version": "v2.1",
                        "total_episodes": 2,
                        "total_frames": 11,
                    }
                )
            )
            output.write_text("")
        elif name == "doctor":
            output.write_text(
                json.dumps(
                    {
                        "total_episodes": 2,
                        "total_frames": 11,
                        "overall_severity": "PASS",
                        "checks": [
                            {"name": name, "severity": "PASS", "messages": []}
                            for name in sorted(DOCTOR_REQUIRED_CHECKS)
                        ],
                    }
                )
            )
        else:
            output.write_text("")
        return 0, output, error

    monkeypatch.setattr(runner, "command", command)
    job = conversions.enqueue("source", "v2.1", run_doctor=True)
    runner.tick()
    result = runner.store.get("job", job["id"])
    assert result["state"] == "COMPLETED", result
    assert result["dataset_ready"]
    assert result["doctor_report"]["overall_severity"] == "PASS"
    assert calls == ["environment", "source-validate", "convert", "validate", "doctor"]
    assert opened and len(closed) == 1


def test_version_conversion_permission_failure_never_publishes(
    settings_factory,
    monkeypatch,
):
    settings = settings_factory("server", letools_bin=sys.executable)
    app = create_app(settings, background=False)
    seed(app)
    runner = app.state.runner
    closed = []
    monkeypatch.setattr(runner, "_open_allocation", lambda _: "allocation-45")
    monkeypatch.setattr(runner, "_close_allocation", lambda: closed.append(True))

    def command(args, work, name, allocation=None):
        output, error = work / f"{name}.json", work / f"{name}.stderr.log"
        output.write_text("")
        error.write_text("")
        if name == "convert":
            dataset = work / "dataset"
            (dataset / "meta").mkdir(parents=True)
            (dataset / "meta/info.json").write_text(
                json.dumps(
                    {
                        "codebase_version": "v2.1",
                        "total_episodes": 2,
                        "total_frames": 11,
                    }
                )
            )
        return 0, output, error

    monkeypatch.setattr(runner, "command", command)
    monkeypatch.setattr(
        "pipeline.server.version_conversions.share_data_tree",
        lambda _: (_ for _ in ()).throw(PermissionError("cannot share")),
    )
    job = runner.version_conversions.enqueue("source", "v2.1", run_doctor=False)
    runner.tick()
    result = runner.store.get("job", job["id"])
    assert result["state"] == "FAILED"
    assert not result.get("dataset_ready")
    assert closed
    with pytest.raises(DomainError):
        dataset_for_job(settings, result)


@pytest.mark.parametrize(
    ("source_version", "target_version"),
    [("v2.1", "v3.0"), ("v3.0", "v2.1")],
)
def test_real_version_conversion_round_trip_preserves_data_and_permissions(
    settings_factory,
    tmp_path,
    letools_binary,
    source_version,
    target_version,
):
    python = str(Path(letools_binary).with_name("python"))
    fixtures = tmp_path / "fixtures"
    subprocess.run(
        [python, str(SCENARIO), "create", str(fixtures), source_version],
        check=True,
    )
    source = fixtures / "input-0"
    source_hashes = {
        str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in source.rglob("*")
        if path.is_file()
    }
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
    with TestClient(app) as client:
        auth = admin(settings)
        imported = client.post("/api/imports", headers=auth, json={"path": str(source)})
        assert imported.status_code == 201, imported.text
        source_job_id = imported.json()["job_id"]
        candidates = client.get(
            "/api/version-conversion-candidates", headers=auth
        ).json()
        candidate = next(
            row for row in candidates if row["source_job_id"] == source_job_id
        )
        assert candidate["selectable"]
        assert candidate["source_format"] == source_version
        assert candidate["target_format"] == target_version
        response = client.post(
            "/api/version-conversions",
            headers=auth,
            json={
                "source_job_id": source_job_id,
                "target_format": target_version,
                "name": f"converted {target_version}",
                "run_doctor": False,
            },
        )
        assert response.status_code == 202, response.text
        job = response.json()
        old_umask = os.umask(0o077)
        try:
            assert runner.tick()
        finally:
            os.umask(old_umask)
        result = client.get("/api/jobs/" + job["id"], headers=auth).json()
        assert result["state"] == "CONVERTED", result
        output = Path(result["dataset_path"])
        assert output == data_root / "test_arm/work" / job["id"] / "dataset"
        assert (
            json.loads((output / "meta/info.json").read_text())["codebase_version"]
            == target_version
        )
        source_inspection = json.loads(
            subprocess.check_output(
                [python, str(SCENARIO), "inspect", str(source)],
                text=True,
            )
        )
        output_inspection = json.loads(
            subprocess.check_output(
                [python, str(SCENARIO), "inspect", str(output)],
                text=True,
            )
        )
        assert output_inspection["episodes"] == source_inspection["episodes"]
        assert output_inspection["frames"] == source_inspection["frames"]
        assert output_inspection["indices"] == source_inspection["indices"]
        assert output_inspection["tasks"] == source_inspection["tasks"]
        assert output_inspection["rows"] == source_inspection["rows"]
        lineage = json.loads(
            (output / "meta/bodypipeline-version-conversion.json").read_text()
        )
        assert lineage["source_job_id"] == source_job_id
        assert lineage["source_format"] == source_version
        assert lineage["target_format"] == target_version
        assert {
            str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in source.rglob("*")
            if path.is_file()
        } == source_hashes
        for path in [output, *output.rglob("*")]:
            access = 0o777 if path.is_dir() else 0o666
            assert stat.S_IMODE(path.stat().st_mode) & access == access, path
        next_candidate = next(
            row
            for row in client.get(
                "/api/version-conversion-candidates", headers=auth
            ).json()
            if row["source_job_id"] == job["id"]
        )
        assert next_candidate["source_format"] == target_version
        assert next_candidate["target_format"] == source_version
