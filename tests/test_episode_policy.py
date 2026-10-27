"""Batch quality admission, dense output numbering and fatal-error boundaries."""

import copy
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import h5py
import pytest
from fastapi.testclient import TestClient
from test_conversion_workbench import seed_upload
from test_multi_runtime import TestRobotProvider, manifest_for, robot_profile
from test_processing import captured, upload_sample

from pipeline.server.app import create_app
from pipeline.server.episode_validation import EpisodeValidationError
from pipeline.server.ingest import IngestService
from pipeline.server.jobs import JobRunner
from pipeline.server.providers.tianji import TianjiProvider
from pipeline.shared.core import DomainError


def batch(settings_factory, *, bad=(0, 2), count=4, **settings):
    raw, manifest = captured(settings_factory)
    config = settings_factory("server", **settings)
    ingest = IngestService(config)
    package = raw.parent / "episode.hdf5"
    healthy = package.read_bytes()
    ids = []
    try:
        for index in range(count):
            package.write_bytes(healthy)
            if index in bad:
                with h5py.File(package, "r+") as h:
                    h["streams/arm/jointState/puppetLeft/position"][0, 0] = float("nan")
            m = copy.deepcopy(manifest)
            m["session_id"] = f"policy-source-{index}"
            for item in m["files"]:
                payload = (raw.parent / item["path"]).read_bytes()
                item.update(
                    size_bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest()
                )
            ids.append(upload_sample(ingest, raw, m))
    finally:
        package.write_bytes(healthy)
    runner = JobRunner(config, ingest)
    return config, ingest, runner, ids


def enqueue(runner, ingest, ids, policy="skip", target="v3.0"):
    return runner.enqueue_collection(
        ingest.store.get("upload", ids[0])["collection_id"],
        "Fold cloth.",
        target,
        False,
        invalid_episode_policy=policy,
    )


@pytest.mark.parametrize("policy,state", [("fail", "FAILED"), ("skip", "DEMO_READY")])
def test_quality_policy_keeps_snapshot_and_maps_retained_sources(
    settings_factory, policy, state
):
    _, ingest, runner, ids = batch(settings_factory)
    job = enqueue(runner, ingest, ids, policy)
    runner.tick()
    result = ingest.store.get("job", job["id"])
    assert result["state"] == state, result
    assert result["upload_ids"] == ids and result["source_count"] == 4
    selection = result["selection_report"]
    assert selection["input_count"] == 4 and selection["accepted_count"] == 2
    assert selection["skipped_count"] == (2 if policy == "skip" else 0)
    assert all(ingest.store.get("upload", uid)["state"] == "COMMITTED" for uid in ids)
    audit = json.loads(
        (Path(result["work_path"]) / "episode-selection.json").read_text()
    )
    assert len(audit["episodes"]) == 4
    if policy == "skip":
        work = Path(result["work_path"])
        assert sorted(p.name for p in (work / "aligned").iterdir()) == [
            "episode0.hdf5",
            "episode1.hdf5",
        ]
        sources = json.loads((work / "lineage.json").read_text())["sources"]
        assert [
            (s["episode_index"], s["source_episode_index"], s["upload_id"])
            for s in sources
        ] == [(0, 1, ids[1]), (1, 3, ids[3])]
        assert result["alignment_report"]["episode_count"] == 2
        assert [d["output_episode_index"] for d in result["episode_diagnostics"]] == [
            None,
            0,
            None,
            1,
        ]
    else:
        assert not result.get("dataset_ready")


def test_all_invalid_fails_without_empty_dataset(settings_factory):
    _, ingest, runner, ids = batch(settings_factory, bad=(0, 1), count=2)
    job = enqueue(runner, ingest, ids)
    runner.tick()
    result = ingest.store.get("job", job["id"])
    assert result["state"] == "FAILED" and "没有合格采集" in result["error"]
    assert result["selection_report"]["skipped_count"] == 2
    assert not result.get("dataset_ready")
    assert not (Path(result["work_path"]) / "dataset").exists()


def test_skip_all_valid_preserves_every_source(settings_factory):
    _, ingest, runner, ids = batch(settings_factory, bad=(), count=2)
    job = enqueue(runner, ingest, ids)
    runner.tick()
    result = ingest.store.get("job", job["id"])
    assert result["state"] == "DEMO_READY", result
    assert result["selection_report"]["accepted_count"] == 2
    assert result["selection_report"]["skipped_count"] == 0
    sources = json.loads((Path(result["work_path"]) / "lineage.json").read_text())[
        "sources"
    ]
    assert [s["upload_id"] for s in sources] == ids


@pytest.mark.parametrize(
    "error",
    [
        DomainError("tool unavailable"),
        PermissionError("I/O"),
        RuntimeError("bug"),
        subprocess.TimeoutExpired("provider", 1),
    ],
)
def test_skip_never_hides_system_errors(settings_factory, monkeypatch, error):
    _, ingest, runner, ids = batch(settings_factory, bad=(), count=2)
    provider = runner.catalog.provider(
        runner.runtime_for_job(enqueue(runner, ingest, ids))
    )
    original = provider.prepare

    def prepare(raw, dest, profile, **kwargs):
        if Path(raw) == Path(ingest.store.get("upload", ids[0])["raw_path"]):
            raise error
        return original(raw, dest, profile, **kwargs)

    monkeypatch.setattr(provider, "prepare", prepare)
    job = enqueue(runner, ingest, ids)
    runner.tick()
    result = ingest.store.get("job", job["id"])
    assert result["state"] == "FAILED" and not result.get("dataset_ready")
    assert result["selection_report"]["failed_count"] == 1
    assert result["selection_report"]["skipped_count"] == 0
    assert result["episode_diagnostics"][0]["skippable"] is False


def test_skip_preserves_integrity_gate(settings_factory):
    _, ingest, runner, ids = batch(settings_factory, bad=(), count=2)
    row = ingest.store.get("upload", ids[0])
    m = ingest.manifest(row)
    (Path(row["raw_path"]) / m["files"][0]["path"]).write_bytes(b"changed")
    job = enqueue(runner, ingest, ids)
    runner.tick()
    result = ingest.store.get("job", job["id"])
    assert result["state"] == "FAILED" and "摘要变化" in result["error"]
    assert result["selection_report"]["failed_count"] == 1


def test_policy_api_validation_and_deduplication(settings_factory):
    settings = settings_factory("server")
    app = create_app(settings, background=False)
    uid = seed_upload(app.state.ingest, settings)
    headers = {"Authorization": "Bearer " + settings.token}
    with TestClient(app) as http:
        path = f"/api/uploads/{uid}/process"
        strict = http.post(path, headers=headers).json()
        assert strict["invalid_episode_policy"] == "fail"
        assert (
            http.post(
                path, headers=headers, json={"invalid_episode_policy": "fail"}
            ).json()["id"]
            == strict["id"]
        )
        skipped = http.post(
            path, headers=headers, json={"invalid_episode_policy": "skip"}
        ).json()
        assert skipped["id"] != strict["id"]
        assert (
            http.post(
                path, headers=headers, json={"invalid_episode_policy": "skip"}
            ).json()["id"]
            == skipped["id"]
        )
        for policy in ("ignore", True, None, [], 1):
            assert (
                http.post(
                    path, headers=headers, json={"invalid_episode_policy": policy}
                ).status_code
                == 422
            )
        cid = app.state.ingest.store.get("upload", uid)["collection_id"]
        response = http.post(
            f"/api/collections/{cid}/process",
            headers=headers,
            json={"instruction": "fold cloth", "invalid_episode_policy": "skip"},
        )
        assert response.status_code == 202
        assert response.json()["invalid_episode_policy"] == "skip"
        with pytest.raises(DomainError):
            app.state.runner.enqueue(uid, invalid_episode_policy="ignore")


@pytest.mark.parametrize("target", ["v2.1", "v3.0"])
def test_partial_dataset_real_conversion_and_deep_validation(
    settings_factory, letools_binary, target
):
    _, ingest, runner, ids = batch(
        settings_factory,
        bad=(0,),
        count=3,
        processing_mode="real",
        letools_bin=letools_binary,
    )
    job = enqueue(runner, ingest, ids, target=target)
    runner.tick()
    result = ingest.store.get("job", job["id"])
    assert result["state"] == "CONVERTED", result
    assert result["episode_count"] == 2 and result["source_count"] == 3
    dataset = Path(result["dataset_path"])
    info = json.loads((dataset / "meta/info.json").read_text())
    assert info["total_episodes"] == 2
    assert info["total_frames"] == result["alignment_report"]["frames"]
    assert json.loads((dataset.parent / "validate.json").read_text())["valid"]


def test_skip_is_generic_for_directory_provider_with_six_joints(
    settings_factory, monkeypatch
):
    settings = settings_factory(
        "server",
        profiles={
            "other": {
                "profile": robot_profile(),
                "provider": "test_robot_b",
                "raw_formats": ["robot_b_v1"],
            }
        },
        default_profile="other",
    )
    ingest = IngestService(settings)
    runner = JobRunner(settings, ingest)
    runtime = runner.catalog.get("other")
    ids = []
    for index in range(12):
        m, payload = manifest_for(
            settings,
            runtime.profile,
            robot_id=settings.robot_id,
            project_id=settings.project_id,
            raw_format="robot_b_v1",
            value=b"bad" if index == 0 else str(index).encode(),
        )
        m["session_id"] = f"directory-policy-{index}"
        row = ingest.create(m, "default", "", runtime=runtime)
        ingest.chunk(row["id"], 0, 0, hashlib.sha256(payload).hexdigest(), payload)
        ingest.complete(row["id"])
        ids.append(row["id"])
    original = TestRobotProvider.prepare

    def prepare(self, raw, dest, profile):
        if (raw / "episode0/example.json").read_bytes() == b"bad":
            raise EpisodeValidationError("invalid signal")
        report = original(self, raw, dest, profile)
        (dest / "episode0/source.txt").write_bytes(
            (raw / "episode0/example.json").read_bytes()
        )
        report["streams"] = {"state": {"dimensions": [6]}}
        return report

    monkeypatch.setattr(TestRobotProvider, "prepare", prepare)
    job = enqueue(runner, ingest, ids)
    runner.tick()
    result = ingest.store.get("job", job["id"])
    assert result["state"] == "DEMO_READY", result
    aligned = Path(result["work_path"]) / "aligned"
    assert [
        int((p / "source.txt").read_text()) for p in sorted(aligned.iterdir())
    ] == list(range(1, 12))
    sources = json.loads((aligned.parent / "lineage.json").read_text())["sources"]
    assert [s["episode_index"] for s in sources] == list(range(11))
    assert [s["upload_id"] for s in sources] == ids[1:]


@pytest.mark.parametrize(
    "kind,skippable",
    [
        ("missing", True),
        ("gap", True),
        ("traceback", False),
        ("malformed", False),
        ("timeout", False),
        ("cli_error", False),
        ("permission", False),
        ("mixed", False),
    ],
)
def test_tianji_requires_structured_data_rejection(
    tmp_path, monkeypatch, kind, skippable
):
    provider = TianjiProvider()
    provider._provider_bin = "/test/tianji-provider"
    profile = json.loads(Path("configs/tianji-thor-v1.json").read_text())

    def run(command, **kwargs):
        if command[1] == "inspect":
            return SimpleNamespace(returncode=2, stderr="source rejected", stdout="")
        if kind == "timeout":
            raise subprocess.TimeoutExpired(command, 1)
        if kind == "cli_error":
            return SimpleNamespace(returncode=2, stderr="unknown option", stdout="")
        issue = {
            "severity": "error",
            "code": "required_command_missing",
            "exception_type": "RequiredSignalMissing",
        }
        if kind == "gap":
            issue.update(code="alignment_error", exception_type="ValueError")
        if kind == "traceback":
            issue.update(code="numeric_read_error", exception_type="TypeError")
        if kind == "permission":
            issue.update(code="numeric_read_error", exception_type="PermissionError")
        report = {
            "format": "tianji-diagnostics-v1",
            "recordings": 1,
            "valid": False,
            "results": [{"valid": False, "issues": [issue]}],
        }
        if kind == "mixed":
            report["results"][0]["issues"].append(
                {
                    "severity": "error",
                    "code": "video_read_error",
                    "exception_type": "RuntimeError",
                }
            )
        path = Path(command[command.index("--report") + 1])
        path.write_text("invalid JSON" if kind == "malformed" else json.dumps(report))
        return SimpleNamespace(returncode=1, stderr="", stdout="")

    monkeypatch.setattr("pipeline.server.providers.tianji.subprocess.run", run)
    with pytest.raises((DomainError, subprocess.TimeoutExpired)) as caught:
        provider._inspect(tmp_path, profile)
    assert isinstance(caught.value, EpisodeValidationError) is skippable
    if skippable:
        assert caught.value.code == 409
