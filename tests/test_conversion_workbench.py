import copy
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_ingest import manifest
from test_processing import captured, upload_sample

from pipeline.server.app import create_app
from pipeline.server.checks import QualityRunner
from pipeline.server.datasets import dataset_for_job
from pipeline.server.ingest import IngestService
from pipeline.server.jobs import JobRunner
from pipeline.shared.core import DomainError


def seed_upload(ingest, settings, complete=True):
    m, data = manifest(settings)
    uid = ingest.create(m, "default", "")["id"]
    if complete:
        ingest.chunk(uid, 0, 0, hashlib.sha256(data).hexdigest(), data)
        ingest.complete(uid)
    return uid


def test_conversion_api_formats_options_and_authorization(settings_factory):
    s = settings_factory("server")
    app = create_app(s, background=False)
    uid = seed_upload(app.state.ingest, s)
    admin = {"Authorization": "Bearer " + s.token}
    device = {"Authorization": "Bearer " + s.device_token}
    with TestClient(app) as http:
        config = http.get("/api/config", headers=admin).json()
        assert {f["id"] for f in config["conversion_formats"]} == {"v2.1", "v3.0"}
        assert config["viewer_url"] == s.viewer_origin.rstrip("/") + "/"
        path = f"/api/uploads/{uid}/process"
        assert http.post(path).status_code == 401
        assert http.post(path, headers=device).status_code == 401
        for body in (
            {"target_format": "v9.0"},
            {"run_doctor": "false"},
            {"output_path": "/tmp/escape"},
        ):
            assert http.post(path, headers=admin, json=body).status_code == 422
        default = http.post(path, headers=admin)
        assert default.status_code == 202
        first = default.json()
        assert first["target_format"] == "v3.0" and first["run_doctor"]
        assert not first["dataset_available"]
        assert http.post(path, headers=admin, json={}).json()["id"] == first["id"]
        v2 = http.post(path, headers=admin, json={"target_format": "v2.1"}).json()
        manual = http.post(
            path, headers=admin, json={"target_format": "v2.1", "run_doctor": False}
        ).json()
        assert len({first["id"], v2["id"], manual["id"]}) == 3
        assert manual["run_doctor"] is False
        assert (
            http.post(f"/api/jobs/{first['id']}/checks", headers=admin).status_code
            == 409
        )
        assert (
            http.post(f"/api/jobs/{first['id']}/viewer", headers=admin).status_code
            == 409
        )
        assert (
            http.get(f"/api/jobs/{first['id']}/checks", headers=device).status_code
            == 401
        )


def test_cannot_convert_partial_upload_or_bypass_real_profile(settings_factory):
    s = settings_factory("server")
    ingest = IngestService(s)
    uid = seed_upload(ingest, s, complete=False)
    runner = JobRunner(s, ingest)
    with pytest.raises(DomainError, match="已提交"):
        runner.enqueue(uid, "v2.1", False)
    raw, m = captured(settings_factory)
    m["capture_mode"] = "ros2"
    real_uid = upload_sample(ingest, raw, m)
    job = runner.enqueue(real_uid, "v2.1", False)
    runner.tick()
    result = ingest.store.get("job", job["id"])
    assert result["state"] == "BLOCKED"
    with pytest.raises(DomainError):
        dataset_for_job(s, result)


@pytest.mark.parametrize("state", ["CONVERTED", "QUARANTINED", "COMPLETED"])
def test_check_queue_recovery_and_report_error_do_not_change_job(
    settings_factory, monkeypatch, state
):
    s = settings_factory("server", letools_bin="/usr/bin/true")
    app = create_app(s, background=False)
    store = app.state.ingest.store
    job = {"id": "inspect-test", "state": state, "dataset_ready": True}
    dataset = s.root / "work/inspect-test/dataset"
    (dataset / "meta").mkdir(parents=True)
    (dataset / "meta/info.json").write_text('{"total_episodes": 1, "total_frames": 2}')
    store.put("job", job["id"], job)
    original = copy.deepcopy(job)

    def command(args, work, name, allocation=None):
        out, err = work / (name + ".json"), work / (name + ".stderr.log")
        out.write_text("not a doctor report")
        err.write_text("test diagnostic")
        return 0, out, err

    app.state.checks.command = command
    admin = {"Authorization": "Bearer " + s.token}
    with TestClient(app) as http:
        response = http.post(f"/api/jobs/{job['id']}/checks", headers=admin)
        assert response.status_code == 202
        check = response.json()
        assert (
            http.post(f"/api/jobs/{job['id']}/checks", headers=admin).json()["id"]
            == check["id"]
        )
        app.state.checks.tick()
        rows = http.get(f"/api/jobs/{job['id']}/checks", headers=admin).json()
        assert rows[0]["state"] == "FAILED" and "有效报告" in rows[0]["error"]
        assert (
            http.get(f"/api/checks/{check['id']}/logs/doctor", headers=admin).json()[
                "text"
            ]
            == "test diagnostic"
        )
        assert (
            http.get(
                f"/api/checks/{check['id']}/logs/unknown", headers=admin
            ).status_code
            == 404
        )
        next_check = http.post(f"/api/jobs/{job['id']}/checks", headers=admin).json()
        assert next_check["id"] != check["id"]
        app.state.checks.save(next_check, state="RUNNING")
        app.state.checks.recover()
        assert store.get("check", next_check["id"])["state"] == "INTERRUPTED"
        assert store.get("job", job["id"]) == original
        assert dataset_for_job(s, job) == dataset


def test_old_jobs_keep_v3_default_and_no_dataset_until_ready(settings_factory):
    s = settings_factory("server")
    app = create_app(s, background=False)
    job = {"id": "legacy", "state": "DEMO_READY", "processing_mode": "demo"}
    app.state.ingest.store.put("job", job["id"], job)
    with TestClient(app) as http:
        row = http.get(
            "/api/jobs/legacy", headers={"Authorization": "Bearer " + s.token}
        ).json()
        assert row["target_format"] == "v3.0" and not row["dataset_available"]


def test_dataset_resolution_reads_recorded_path_as_read_only_fallback(
    settings_factory, tmp_path
):
    s = settings_factory("server")
    recorded = tmp_path / "old-layout" / "dataset"
    (recorded / "meta").mkdir(parents=True)
    (recorded / "meta/info.json").write_text("{}")
    job = {
        "id": "legacy-ready",
        "state": "COMPLETED",
        "dataset_path": str(recorded),
    }
    # The fallback only accepts paths below a server-owned data root.
    with pytest.raises(DomainError, match="不存在或存储不可用"):
        dataset_for_job(s, job)
    s.root = tmp_path
    assert dataset_for_job(s, job) == recorded


@pytest.mark.parametrize("target_format", ["v2.1", "v3.0"])
def test_real_convert_then_independent_doctor(
    settings_factory, letools_binary, target_format
):
    raw, m = captured(settings_factory)
    s = settings_factory("server", processing_mode="real", letools_bin=letools_binary)
    ingest = IngestService(s)
    uid = upload_sample(ingest, raw, m)
    runner = JobRunner(s, ingest)
    job = runner.enqueue(uid, target_format, run_doctor=False)
    runner.tick()
    result = ingest.store.get("job", job["id"])
    assert result["state"] == "CONVERTED", result
    assert "doctor_report" not in result
    data = dataset_for_job(s, result)
    info = json.loads((data / "meta/info.json").read_text())
    assert info["codebase_version"] == target_format
    before = {
        str(p.relative_to(data)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in data.rglob("*")
        if p.is_file()
    }
    called = []

    def command(args, work, name, allocation=None):
        called.append(args[1])
        return runner.command(args, work, name)

    checks = QualityRunner(s, ingest.store, command)
    check = checks.enqueue(job["id"])
    checks.tick()
    report = ingest.store.get("check", check["id"])
    assert report["state"] == "COMPLETED", report
    assert report["severity"] == "WARN"
    assert report["quality_gate"]["coverage"]["frames"] == info["total_frames"]
    assert called == ["validate", "doctor"]
    assert ingest.store.get("job", job["id"]) == result
    assert before == {
        str(p.relative_to(data)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in data.rglob("*")
        if p.is_file()
    }
    assert not (s.root / "datasets").exists() or not any(
        (s.root / "datasets").iterdir()
    )
    assert Path(report["work_path"], "doctor.json").is_file()


def test_robot_type_catalog_and_preflight_legacy_records(settings_factory):
    from pipeline.shared.core import digest

    s = settings_factory("server")
    app = create_app(s, background=False)
    ingest = app.state.ingest
    for robot_type, mode in [("agilex_aloha", "ros2"), ("another_robot", "mock")]:
        m, data = manifest(s)
        m["session_id"] = robot_type.replace("_", "-")
        m["capture_mode"] = mode
        m["profile"]["robot_type"] = robot_type
        m["profile_hash"] = digest(m["profile"])
        uid = ingest.create(m, "default", "")["id"]
        ingest.chunk(uid, 0, 0, hashlib.sha256(data).hexdigest(), data)
        ingest.complete(uid)
        if robot_type == "agilex_aloha":
            row = ingest.store.get("upload", uid)
            row.pop("robot_type")
            ingest.store.put("upload", uid, row)
    with TestClient(app) as http:
        rows = http.get(
            "/api/uploads", headers={"Authorization": "Bearer " + s.token}
        ).json()
        indexed = {r["robot_type"]: r for r in rows}
        assert set(indexed) == {"agilex_aloha", "another_robot"}
        assert {b["field"] for b in indexed["agilex_aloha"]["conversion_blockers"]} == {
            "semantics_approved",
            "clock_verified",
            "joint_names",
            "joint_units",
        }
        assert {
            b["field"] for b in indexed["another_robot"]["conversion_blockers"]
        } == {"robot_type", "profile_hash"}


def test_deep_validation_retries_only_the_publish_race(
    settings_factory, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from pipeline.shared.core import Store

    s = settings_factory("server")
    runner = JobRunner(s, SimpleNamespace(store=Store(s.root / "state")))
    work = tmp_path / "work"
    work.mkdir()
    calls = []

    def command(args, work, name, allocation=None):
        calls.append(name)
        report, err = work / f"{name}.json", work / f"{name}.stderr.log"
        err.write_text("")
        if len(calls) < 3:
            report.write_text('{"errors": ["[Errno 2] No such file or directory"]}')
            return 1, report, err
        report.write_text('{"valid": true}')
        return 0, report, err

    monkeypatch.setattr(runner, "command", command)
    monkeypatch.setattr("pipeline.server.jobs.time.sleep", lambda _: None)
    runner._deep_validate("/opt/letools", work, None)
    assert calls == ["validate"] * 3


def test_deep_validation_does_not_retry_a_real_failure(
    settings_factory, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from pipeline.shared.core import Store

    s = settings_factory("server")
    runner = JobRunner(s, SimpleNamespace(store=Store(s.root / "state")))
    work = tmp_path / "work"
    work.mkdir()
    calls = []

    def command(args, work, name, allocation=None):
        calls.append(name)
        report, err = work / f"{name}.json", work / f"{name}.stderr.log"
        report.write_text('{"errors": ["episode 0 has no data"]}')
        err.write_text("validation failed")
        return 1, report, err

    monkeypatch.setattr(runner, "command", command)
    with pytest.raises(DomainError, match="结构验证失败"):
        runner._deep_validate("/opt/letools", work, None)
    assert calls == ["validate"]


def test_verified_profile_and_optional_limit_metadata():
    from pipeline.server.quality import robot_profile_blockers, validate_robot_policy

    profile = json.loads(Path("configs/agilex-aloha-v2.json").read_text())
    validate_robot_policy(profile)
    assert robot_profile_blockers(profile) == []
    assert profile["joint_units"] == (["rad"] * 6 + ["m"]) * 2
    assert profile["joint_names"][6] == "left_gripper"
    assert profile["joint_names"][13] == "right_gripper"
    assert profile["max_action_age_ms"] == 50
    without_limits = {
        key: value
        for key, value in profile.items()
        if key not in {"state_limits", "action_limits"}
    }
    validate_robot_policy(without_limits)
    assert robot_profile_blockers(without_limits) == []
    assert profile["verification"]["reviewed_components"]
    # The shared template must not silently approve a different robot.
    template = json.loads(Path("configs/agilex-aloha.json").read_text())
    assert not template["semantics_approved"] and not template["clock_verified"]
