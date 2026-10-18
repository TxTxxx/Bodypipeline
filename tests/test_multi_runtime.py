import copy
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pipeline.server.app import create_app
from pipeline.server.ingest import IngestService
from pipeline.server.jobs import JobRunner
from pipeline.server.providers import register_provider
from pipeline.shared.core import DomainError, digest


class TestRobotProvider:
    id = "test_robot_b"
    prepare_calls = 0

    def validate_profile(self, profile):
        if profile.get("robot_type") != "robot_b":
            raise DomainError("test provider robot type mismatch")
        names, units = profile.get("joint_names"), profile.get("joint_units")
        if len(names) != len(units) or len(names) != 6:
            raise DomainError("test provider expects six joints")

    def profile_blockers(self, profile):
        return []

    def prepare(self, raw, dest, profile):
        type(self).prepare_calls += 1
        dest.mkdir(parents=True)
        (dest / "episode0").mkdir()
        return {"fps": 10, "frames": 1, "streams": {}}

    def quality(self, aligned, profile, synthetic=False):
        return {"status": "PASS", "joint_limits_enforced": False}

    def conversion_args(self, *, executable, source, destination, profile, job):
        return [
            executable,
            "convert",
            str(source),
            str(destination),
            "--source-format",
            "robot_b",
            "--to",
            job.get("target_format", "v3.0"),
        ]

    def patch_dataset_info(self, dataset, profile, *, synthetic=False):
        return None

    def lineage_metadata(self, profile):
        return {"provider": self.id, "source_format": "robot_b"}


register_provider(TestRobotProvider())


def robot_profile(robot_type="robot_b"):
    return {
        "id": f"{robot_type}-profile",
        "version": 1,
        "control_mode": "teleoperation",
        "robot_type": robot_type,
        "semantics_approved": True,
        "clock_verified": True,
        "joint_names": [f"joint_{i}" for i in range(6)],
        "joint_units": ["rad"] * 6,
        "output_fps": 10,
        "minimum_frames": 1,
    }


def manifest_for(settings, profile, *, robot_id, project_id, raw_format, value=b"x"):
    m = {
        "manifest_version": 1,
        "complete": True,
        "session_id": f"session-{robot_id}",
        "robot_id": robot_id,
        "project_id": project_id,
        "control_mode": "teleoperation",
        "raw_format": raw_format,
        "capture_mode": "mock",
        "instruction": "test task",
        "dataset_name": "test task",
        "profile": profile,
        "profile_hash": digest(profile),
        "files": [
            {
                "path": "episode0/example.json",
                "size_bytes": len(value),
                "sha256": hashlib.sha256(value).hexdigest(),
            }
        ],
    }
    return m, value


def upload(http, headers, manifest, data):
    response = http.post(
        "/device/uploads",
        headers=headers,
        json={"manifest": manifest, "target": "default"},
    )
    assert response.status_code == 201, response.text
    upload_id = response.json()["id"]
    assert (
        http.put(
            f"/device/uploads/{upload_id}/files/0?offset=0",
            headers={**headers, "X-Chunk-SHA256": hashlib.sha256(data).hexdigest()},
            content=data,
        ).status_code
        == 200
    )
    assert (
        http.post(f"/device/uploads/{upload_id}/complete", headers=headers).status_code
        == 200
    )
    return upload_id


def multi_runtime_settings(settings_factory, monkeypatch):
    monkeypatch.setenv("ROBOT_A_SECRET", "robot-a-" + "a" * 32)
    monkeypatch.setenv("ROBOT_B_SECRET", "robot-b-" + "b" * 32)
    base = settings_factory("server")
    profile_b = robot_profile()
    return settings_factory(
        "server",
        profiles={
            "primary": {
                "profile_path": base.profile_path,
                "provider": "agilex",
                "raw_formats": ["agilex_timestamped_directory_v1"],
            },
            "robot_b": {
                "profile": profile_b,
                "provider": "test_robot_b",
                "raw_formats": ["robot_b_v1"],
            },
        },
        default_profile="primary",
        devices=[
            {
                "robot_id": "robot-a",
                "project_id": "project-a",
                "token_env": "ROBOT_A_SECRET",
                "targets": ["default"],
                "profile": "primary",
            },
            {
                "robot_id": "robot-b",
                "project_id": "project-b",
                "token_env": "ROBOT_B_SECRET",
                "targets": ["default"],
                "profile": "robot_b",
            },
        ],
    )


def test_legacy_profile_path_creates_default_runtime(settings_factory):
    settings = settings_factory("server")
    runtimes = settings.runtime_registry()
    assert list(runtimes) == ["default"]
    assert runtimes["default"].provider == "agilex"
    assert runtimes["default"].profile_hash == digest(settings.profile())


def test_devices_route_to_distinct_runtimes_and_cannot_cross_formats(
    settings_factory, monkeypatch
):
    settings = multi_runtime_settings(settings_factory, monkeypatch)
    robot_b_profile = robot_profile()
    app = create_app(settings, background=False)
    headers_a = {"Authorization": "Bearer robot-a-" + "a" * 32}
    headers_b = {"Authorization": "Bearer robot-b-" + "b" * 32}
    human = {"Authorization": "Bearer " + settings.token}
    with TestClient(app) as http:
        config = http.get("/api/config", headers=human).json()
        assert {r["robot_type"] for r in config["runtimes"]} == {
            "agilex_aloha",
            "robot_b",
        }
        assert config["default_runtime_id"] == "primary"
        assert {r["id"] for r in config["runtimes"]} == {"primary", "robot_b"}
        assert any(r["provider"] == "test_robot_b" for r in config["runtimes"])

        m_b, data_b = manifest_for(
            settings,
            robot_b_profile,
            robot_id="robot-b",
            project_id="project-b",
            raw_format="robot_b_v1",
        )
        uid_b = upload(http, headers_b, m_b, data_b)
        assert app.state.ingest.store.get("upload", uid_b)["runtime_id"] == "robot_b"

        # Device A cannot turn a Client-declared fake profile into code execution
        # or route its upload through Device B's Runtime.
        response = http.post(
            "/device/uploads",
            headers=headers_a,
            json={"manifest": m_b, "target": "default"},
        )
        assert response.status_code == 403

        m_b_wrong_format, data = manifest_for(
            settings,
            robot_b_profile,
            robot_id="robot-b",
            project_id="project-b",
            raw_format="agilex_timestamped_directory_v1",
        )
        response = http.post(
            "/device/uploads",
            headers=headers_b,
            json={"manifest": m_b_wrong_format, "target": "default"},
        )
        assert response.status_code == 400

        rows = http.get("/api/uploads", headers=human).json()
        row = next(item for item in rows if item["id"] == uid_b)
        assert row["robot_type"] == "robot_b"
        assert row["runtime_id"] == "robot_b"
        assert row["provider"] == "test_robot_b"
        assert row["conversion_blockers"] == []


def test_job_uses_upload_runtime_provider(settings_factory, monkeypatch):
    settings = multi_runtime_settings(settings_factory, monkeypatch)
    app = create_app(settings, background=False)
    profile_b = robot_profile()
    m_b, data_b = manifest_for(
        settings,
        profile_b,
        robot_id="robot-b",
        project_id="project-b",
        raw_format="robot_b_v1",
    )
    # Use the service directly so this test covers routing independently of HTTP.
    ingest = app.state.ingest
    runtime = ingest.catalog.get("robot_b")
    row = ingest.create(
        m_b,
        "default",
        "",
        identity={"robot_id": "robot-b", "project_id": "project-b"},
        runtime=runtime,
    )
    ingest.chunk(row["id"], 0, 0, hashlib.sha256(data_b).hexdigest(), data_b)
    ingest.complete(row["id"])
    runner = JobRunner(settings, ingest)
    before = TestRobotProvider.prepare_calls
    job = runner.enqueue(row["id"], run_doctor=False)
    runner.tick()
    result = ingest.store.get("job", job["id"])
    assert result["state"] == "DEMO_READY", result
    assert result["runtime_id"] == "robot_b"
    assert result["provider"] == "test_robot_b"
    assert TestRobotProvider.prepare_calls == before + 1
    lineage = json.loads((Path(result["work_path"]) / "lineage.json").read_text())
    assert lineage["runtime_id"] == "robot_b"
    assert lineage["source_format"] == "robot_b"


def test_batch_rejects_mixed_runtimes(settings_factory, monkeypatch):
    settings = multi_runtime_settings(settings_factory, monkeypatch)
    base = settings_factory("server")
    primary_profile = json.loads(Path(base.profile_path).read_text())
    runtime_b = settings.runtime_registry()["robot_b"]
    fake_profile = copy.deepcopy(runtime_b.profile)

    ingest = IngestService(settings)
    first_manifest, first_data = manifest_for(
        settings,
        primary_profile,
        robot_id="robot-a",
        project_id="project-a",
        raw_format="agilex_timestamped_directory_v1",
    )
    second_manifest, second_data = manifest_for(
        settings,
        fake_profile,
        robot_id="robot-b",
        project_id="project-b",
        raw_format="robot_b_v1",
    )
    rows = []
    for manifest, data, runtime, robot, project in [
        (
            first_manifest,
            first_data,
            ingest.catalog.get("primary"),
            "robot-a",
            "project-a",
        ),
        (
            second_manifest,
            second_data,
            ingest.catalog.get("robot_b"),
            "robot-b",
            "project-b",
        ),
    ]:
        row = ingest.create(
            manifest,
            "default",
            "",
            identity={"robot_id": robot, "project_id": project},
            runtime=runtime,
        )
        ingest.chunk(row["id"], 0, 0, hashlib.sha256(data).hexdigest(), data)
        ingest.complete(row["id"])
        rows.append(row)

    runner = JobRunner(settings, ingest)
    with pytest.raises(DomainError, match="混合"):
        runner._enqueue([row["id"] for row in rows], "v3.0", False, "mixed")


def test_legacy_upload_without_runtime_id_resolves_by_profile_hash(settings_factory):
    profile_v1 = robot_profile()
    profile_v2 = copy.deepcopy(profile_v1)
    profile_v2.update(id="robot-b-profile-v2", version=2)
    settings = settings_factory(
        "server",
        profiles={
            "robot_b_v1": {
                "profile": profile_v1,
                "provider": "test_robot_b",
                "raw_formats": ["robot_b_v1"],
            },
            "robot_b_v2": {
                "profile": profile_v2,
                "provider": "test_robot_b",
                "raw_formats": ["robot_b_v1"],
            },
        },
        default_profile="robot_b_v1",
    )
    ingest = IngestService(settings)
    manifest, data = manifest_for(
        settings,
        profile_v2,
        robot_id="robot-b",
        project_id="project-b",
        raw_format="robot_b_v1",
    )
    row = ingest.create(
        manifest,
        "default",
        "",
        identity={"robot_id": "robot-b", "project_id": "project-b"},
    )
    assert row.get("runtime_id") is None
    ingest.chunk(row["id"], 0, 0, hashlib.sha256(data).hexdigest(), data)
    ingest.complete(row["id"])

    runner = JobRunner(settings, ingest)
    job = runner.enqueue(row["id"], run_doctor=False)
    runner.tick()
    result = ingest.store.get("job", job["id"])
    assert result["state"] == "DEMO_READY", result
    assert result["runtime_id"] == "robot_b_v2"
    assert result["provider"] == "test_robot_b"


def test_legacy_upload_rejects_ambiguous_runtime(settings_factory):
    profile = robot_profile()
    settings = settings_factory(
        "server",
        profiles={
            "robot_b_a": {
                "profile": profile,
                "provider": "test_robot_b",
                "raw_formats": ["robot_b_v1"],
            },
            "robot_b_b": {
                "profile": profile,
                "provider": "test_robot_b",
                "raw_formats": ["robot_b_v1"],
            },
        },
        default_profile="robot_b_a",
    )
    ingest = IngestService(settings)
    manifest, data = manifest_for(
        settings,
        profile,
        robot_id="robot-b",
        project_id="project-b",
        raw_format="robot_b_v1",
    )
    row = ingest.create(
        manifest,
        "default",
        "",
        identity={"robot_id": "robot-b", "project_id": "project-b"},
    )
    ingest.chunk(row["id"], 0, 0, hashlib.sha256(data).hexdigest(), data)
    ingest.complete(row["id"])

    with pytest.raises(DomainError, match="多个 Runtime"):
        JobRunner(settings, ingest).enqueue(row["id"], run_doctor=False)


def test_unknown_provider_is_rejected(settings_factory):
    base = settings_factory("server")
    with pytest.raises(DomainError, match="Provider"):
        IngestService(
            settings_factory(
                "server",
                profiles={
                    "broken": {
                        "profile_path": base.profile_path,
                        "provider": "does_not_exist",
                        "raw_formats": ["agilex_timestamped_directory_v1"],
                    }
                },
                default_profile="broken",
            )
        )


def test_generic_quality_accepts_provider_dimensions():
    from pipeline.server.quality import validate_robot_policy

    profile = robot_profile()
    validate_robot_policy(profile)
    with pytest.raises(DomainError):
        validate_robot_policy(profile, expected_dimensions=14)
