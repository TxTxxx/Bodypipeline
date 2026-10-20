"""Tests for the Argus analysis queue, budget and verdict aggregation."""

import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pipeline.server.analyses import (
    VERDICT_CLEAN,
    VERDICT_INCOMPLETE,
    VERDICT_REVIEW,
    AnalysisRunner,
    BudgetExceeded,
)
from pipeline.server.app import create_app
from pipeline.shared.core import DomainError, Store


def make_dataset(root, episodes=2):
    data = root / "dataset"
    (data / "meta").mkdir(parents=True)
    (data / "meta/info.json").write_text(
        json.dumps({"fps": 30, "total_episodes": episodes, "total_frames": 100})
    )
    return data


def make_argus(root):
    src = root / "argus"
    (src / "label").mkdir(parents=True)
    (src / "label/__init__.py").write_text("")
    venv = src / ".venv/bin"
    venv.mkdir(parents=True)
    python = venv / "python"
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    return src


def write_episode(
    episodes,
    name,
    pairing=None,
    jumps=None,
    grippers=None,
    flags=None,
    not_assessed=None,
):
    directory = episodes / name
    directory.mkdir(parents=True)
    (directory / "context.json").write_text(
        json.dumps(
            {
                "stream_pairing": pairing,
                "recorded_jumps": jumps,
                "gripper_channels": grippers,
                "capture_qc": {
                    "flags": flags or [],
                    "not_assessed": not_assessed or {},
                },
            }
        )
    )


@pytest.fixture
def runner(settings_factory, tmp_path):
    src = make_argus(tmp_path)
    s = settings_factory(
        "server",
        argus_src=str(src),
        argus_task_budget_usd=10.0,
        argus_daily_budget_usd=20.0,
        argus_per_episode_budget_usd=1.0,
    )
    store = Store(s.root / "state")
    data = make_dataset(s.root)
    store.put(
        "job",
        "job-1",
        {
            "id": "job-1",
            "state": "CONVERTED",
            "dataset_ready": True,
            "dataset_path": str(data),
            "robot_type": "agilex_aloha",
            "provider": "agilex",
            "profile": {
                "cameras": [
                    {"id": "left", "mounting": "wrist_left"},
                    {"id": "front", "mounting": "scene"},
                    {"id": "right", "mounting": "wrist_right"},
                ],
                "state_streams": ["puppetLeft", "puppetRight"],
                "joint_names": ["j"] * 14,
                "joint_units": ["rad"] * 14,
                "output_fps": 30,
                "control_mode": "teleoperation",
            },
        },
    )
    return AnalysisRunner(s, store, lambda *a, **k: (0, None, None)), store, src


def test_budget_scales_with_the_batch(runner):
    service, store, _ = runner
    # 2 episodes at 1 USD each, under a 10 USD task cap.
    budget = service.budget("job-1", 2, "full")
    assert budget["task_cap_usd"] == 2.0
    assert budget["available_usd"] == 2.0
    # A large batch is bounded by the task cap, not by the per-episode figure.
    big = service.budget("job-1", 1000, "full")
    assert big["task_cap_usd"] == 10.0
    # Local analysis never consumes budget.
    local = service.budget("job-1", 2, "local")
    assert local["available_usd"] == 0.0


def test_spend_accumulates_across_runs(runner):
    service, store, _ = runner
    store.put(
        "analysis",
        "old",
        {
            "id": "old",
            "job_id": "job-1",
            "state": "COMPLETED",
            "cost_usd": 1.5,
            "created_at": time.time(),
            "finished_at": time.time(),
        },
    )
    budget = service.budget("job-1", 2, "full")
    assert budget["task_used_usd"] == 1.5
    assert budget["available_usd"] == 0.5


def test_full_mode_refuses_without_budget(runner):
    service, store, _ = runner
    store.put(
        "analysis",
        "old",
        {
            "id": "old",
            "job_id": "job-1",
            "state": "COMPLETED",
            "cost_usd": 2.0,
            "created_at": time.time(),
            "finished_at": time.time(),
        },
    )
    with pytest.raises(BudgetExceeded):
        service.enqueue("job-1", mode="full")
    # Local mode is always allowed.
    assert service.enqueue("job-1", mode="local")["mode"] == "local"


def test_enqueue_is_idempotent_while_queued(runner):
    service, _, _ = runner
    first = service.enqueue("job-1", mode="local")
    second = service.enqueue("job-1", mode="local")
    assert first["id"] == second["id"]


def test_unavailable_toolchain_is_reported(settings_factory, tmp_path):
    s = settings_factory("server", argus_src=str(tmp_path / "missing"))
    service = AnalysisRunner(
        s, Store(s.root / "state"), lambda *a, **k: (0, None, None)
    )
    ok, why = service.available()
    assert not ok and "argus_src" in why or "源码" in why


def test_recover_marks_running_as_interrupted(runner):
    service, store, _ = runner
    store.put(
        "analysis",
        "a1",
        {"id": "a1", "job_id": "job-1", "state": "RUNNING", "created_at": time.time()},
    )
    service.recover()
    assert store.get("analysis", "a1")["state"] == "INTERRUPTED"


def test_cancel_is_idempotent(runner):
    service, store, _ = runner
    row = service.enqueue("job-1", mode="local")
    assert service.cancel(row["id"])["state"] == "INTERRUPTED"
    assert service.cancel(row["id"])["state"] == "INTERRUPTED"


def test_summarize_counts_and_verdict_inputs(runner):
    service, _, _tmp = runner
    episodes = _tmp / "episodes"
    write_episode(
        episodes,
        "episode_000000",
        pairing={"left_vs_left": 0.1, "crossed": False},
        jumps={"flagged": False},
        grippers={"flagged": True},
        flags=[{"check": "video_frozen_run"}],
        not_assessed={"largest_action_not_in_video": "needs an end-effector pose"},
    )
    write_episode(
        episodes,
        "episode_000001",
        pairing={"not_assessed": "no mounted cameras"},
        jumps={"not_assessed": "no camera"},
        grippers={"flagged": False},
    )
    summary = service._summarize(episodes)
    assert summary["episodes"] == 2
    assert summary["checks"]["stream_pairing"] == {
        "assessed": 1,
        "not_assessed": 1,
        "crossed": 0,
        "undefined": 0,
        "skipped": 0,
    }
    assert summary["checks"]["gripper_channels"] == {
        "flagged": 1,
        "clean": 1,
        "skipped": 0,
    }
    assert summary["checks"]["capture_qc_flags"] == 1
    assert "largest_action_not_in_video" in summary["not_assessed"]


def test_verdict_distinguishes_clean_from_incomplete(runner):
    service, store, _tmp = runner
    work = _tmp / "work"
    clean = {
        "episodes": 1,
        "checks": {
            "stream_pairing": {
                "assessed": 1,
                "not_assessed": 0,
                "crossed": 0,
                "undefined": 0,
                "skipped": 0,
            },
            "recorded_jumps": {
                "flagged": 0,
                "clean": 1,
                "not_assessed": 0,
                "skipped": 0,
            },
            "gripper_channels": {"flagged": 0, "clean": 1, "skipped": 0},
            "capture_qc_flags": 0,
            "capture_qc_not_assessed": 0,
        },
        "not_assessed": {},
    }
    row = service.enqueue("job-1", mode="local")
    store.put("analysis", row["id"], dict(row))
    service._finish(row, work, clean, {"cross_modal_ready": True})
    assert store.get("analysis", row["id"])["verdict"] == VERDICT_CLEAN

    incomplete = json.loads(json.dumps(clean))
    incomplete["checks"]["stream_pairing"]["not_assessed"] = 1
    incomplete["checks"]["capture_qc_not_assessed"] = 3
    row2 = dict(row, id="a2")
    store.put("analysis", "a2", row2)
    service._finish(row2, work, incomplete, {"cross_modal_ready": False})
    assert store.get("analysis", "a2")["verdict"] == VERDICT_INCOMPLETE

    fired = json.loads(json.dumps(clean))
    fired["checks"]["gripper_channels"]["flagged"] = 1
    row3 = dict(row, id="a3")
    store.put("analysis", "a3", row3)
    service._finish(row3, work, fired, {"cross_modal_ready": True})
    assert store.get("analysis", "a3")["verdict"] == VERDICT_REVIEW


def test_local_gate_reports_missing_mounting(runner):
    service, _, _ = runner
    ready = service._local_gate(
        {"cameras": {"left": "wrist_left", "right": "wrist_right"}}, "a"
    )
    assert ready["cross_modal_ready"] is True
    missing = service._local_gate({"cameras": {"left": "scene"}}, "a")
    assert missing["cross_modal_ready"] is False
    assert "mounting" in missing["reason"]


def test_env_carries_mounting_and_excludes_secrets(runner, monkeypatch):
    service, _, src = runner
    monkeypatch.setenv("PIPELINE_ARGUS_API_KEY", "k" * 32)
    monkeypatch.setenv("PIPELINE_TOKEN", "t" * 32)
    env = service._env({}, {"cameras": {"left": "wrist_left", "right": "wrist_right"}})
    assert json.loads(env["PIPELINE_ARGUS_CAMERA_MOUNTING"]) == {
        "left": "wrist_left",
        "right": "wrist_right",
    }
    assert env["PIPELINE_ARGUS_API_KEY"] == "k" * 32
    assert "PIPELINE_TOKEN" not in env
    assert env["PYTHONPATH"] == str(src)


def test_api_enqueues_lists_and_cancels(runner):
    service, store, _ = runner
    settings = service.settings
    app = create_app(settings, background=False)
    app.state.analyses = service
    headers = {"Authorization": "Bearer " + settings.token}
    with TestClient(app) as http:
        created = http.post(
            "/api/jobs/job-1/analyses", headers=headers, json={"mode": "local"}
        )
        assert created.status_code == 202, created.text
        aid = created.json()["id"]
        listed = http.get("/api/jobs/job-1/analyses", headers=headers).json()
        assert [row["id"] for row in listed] == [aid]
        assert http.get(f"/api/analyses/{aid}", headers=headers).status_code == 200
        budget = http.get(f"/api/analyses/{aid}/budget", headers=headers).json()
        assert budget["task_cap_usd"] == 2.0
        cancelled = http.post(f"/api/analyses/{aid}/cancel", headers=headers)
        assert cancelled.json()["state"] == "INTERRUPTED"
        assert (
            http.post(
                "/api/jobs/job-1/analyses", headers=headers, json={"mode": "bogus"}
            ).status_code
            == 422
        )


def test_doctor_run_also_enqueues_an_analysis(runner):
    service, store, _ = runner
    settings = service.settings
    app = create_app(settings, background=False)
    app.state.analyses = service
    headers = {"Authorization": "Bearer " + settings.token}
    with TestClient(app) as http:
        http.post("/api/jobs/job-1/checks", headers=headers)
        rows = http.get("/api/jobs/job-1/analyses", headers=headers).json()
        assert len(rows) == 1 and rows[0]["mode"] == "local"


def test_analysis_failure_never_touches_the_job(runner):
    service, store, _ = runner
    before = dict(store.get("job", "job-1"))
    row = service.enqueue("job-1", mode="local")
    # Point the runner at a dataset that vanished; the run must fail cleanly.
    store.put("analysis", row["id"], dict(row, dataset_path="/nonexistent/dataset"))
    service.run(store.get("analysis", row["id"]))
    assert store.get("analysis", row["id"])["state"] == "FAILED"
    assert store.get("job", "job-1") == before


def test_storage_area_is_isolated(settings_factory, tmp_path):
    from pipeline.server.storage import AREAS, storage_path

    assert "analyses" in AREAS
    s = settings_factory("server", root=tmp_path / "state", data_root=tmp_path / "data")
    (tmp_path / "data").mkdir()
    row = {"data_layout": "robot_type_v1", "robot_type": "agilex_aloha"}
    path = storage_path(s, row, "analyses", "abc")
    assert path == (tmp_path / "data/agilex_aloha/analyses/abc").resolve()
    with pytest.raises(DomainError):
        storage_path(s, row, "analyses", "../escape")


def test_python_path_is_not_symlink_resolved(settings_factory, tmp_path):
    """A venv interpreter must be invoked as configured.

    Resolving the symlink drops the venv's site-packages, which surfaces as a
    ModuleNotFoundError on the first import inside the toolchain.
    """

    src = tmp_path / "argus"
    (src / "label").mkdir(parents=True)
    venv_bin = src / ".venv/bin"
    venv_bin.mkdir(parents=True)
    real = tmp_path / "real-python"
    real.write_text("#!/bin/sh\nexit 0\n")
    real.chmod(0o755)
    link = venv_bin / "python"
    link.symlink_to(real)
    s = settings_factory("server", argus_src=str(src))
    service = AnalysisRunner(
        s, Store(s.root / "state"), lambda *a, **k: (0, None, None)
    )
    assert service._python() == str(link)


def test_provider_context_falls_back_to_runtime_profile(settings_factory, tmp_path):
    """Imported datasets carry no Profile snapshot on the job."""

    src = make_argus(tmp_path)
    s = settings_factory(
        "server",
        argus_src=str(src),
        profiles={
            "agilex_aloha_agx": {
                "profile_path": "configs/agilex-aloha-v2.json",
                "provider": "agilex",
                "raw_formats": ["agilex_joint_camera_hdf5_v1"],
                "accepted_capture_profile_hashes": [],
            }
        },
        default_profile="agilex_aloha_agx",
    )
    service = AnalysisRunner(
        s, Store(s.root / "state"), lambda *a, **k: (0, None, None)
    )
    context = service._provider_context(
        {"runtime_id": "agilex_aloha_agx", "provider": "agilex"}
    )
    assert context["cameras"] == {
        "left": "wrist_left",
        "front": "scene",
        "right": "wrist_right",
    }
    assert context["joint_units"]


def test_slurm_wrapper_is_transparent_and_configurable(
    settings_factory, tmp_path, monkeypatch
):
    from pipeline.server import jobs as jobs_module
    from pipeline.server.jobs import JobRunner

    monkeypatch.setattr(
        jobs_module, "executable_path", lambda name: "/usr/bin/srun", raising=True
    )

    s = settings_factory(
        "server",
        slurm={
            "enabled": True,
            "partition": "train",
            "cpus_per_task": 32,
            "mem": "128G",
            "time": "04:00:00",
            "nodelist": "H800-node11",
        },
    )
    runner = JobRunner(s, SimpleNamespace(store=Store(s.root / "state")))
    argv = runner._scheduled(["/opt/letools", "convert", "a", "b"])
    assert argv[0] == "/usr/bin/srun"
    assert "--partition" in argv and argv[argv.index("--partition") + 1] == "train"
    assert argv[argv.index("--cpus-per-task") + 1] == "32"
    assert argv[argv.index("--mem") + 1] == "128G"
    assert argv[argv.index("--nodelist") + 1] == "H800-node11"
    assert argv[-4:] == ["/opt/letools", "convert", "a", "b"]


def test_slurm_disabled_passes_the_command_through(settings_factory):
    from pipeline.server.jobs import JobRunner

    s = settings_factory("server")
    runner = JobRunner(s, SimpleNamespace(store=Store(s.root / "state")))
    assert runner._scheduled(["x", "y"]) == ["x", "y"]


def test_conversion_parallelism_respects_the_allocation(settings_factory):
    from pipeline.server.jobs import JobRunner

    s = settings_factory("server", slurm={"enabled": True})
    runner = JobRunner(s, SimpleNamespace(store=Store(s.root / "state")))
    counts = runner._conversion_parallelism()
    assert counts["convert_workers"] >= 1
    assert counts["video_workers"] == counts["convert_workers"]
    assert counts["video_codec_threads"] >= 1

    fixed = settings_factory("server", slurm={"enabled": True, "conversion_workers": 3})
    assert (
        JobRunner(
            fixed, SimpleNamespace(store=Store(fixed.root / "state"))
        )._conversion_parallelism()["convert_workers"]
        == 3
    )


def test_provider_context_fills_missing_facts_from_trusted_runtime(
    settings_factory, tmp_path
):
    """A job converted before mounting was documented still gets the current facts."""

    src = make_argus(tmp_path)
    s = settings_factory(
        "server",
        argus_src=str(src),
        profiles={
            "agilex_aloha_agx": {
                "profile_path": "configs/agilex-aloha-v2.json",
                "provider": "agilex",
                "raw_formats": ["agilex_joint_camera_hdf5_v1"],
                "accepted_capture_profile_hashes": [],
            }
        },
        default_profile="agilex_aloha_agx",
    )
    service = AnalysisRunner(
        s, Store(s.root / "state"), lambda *a, **k: (0, None, None)
    )
    stale = {
        "cameras": [
            {"id": "left", "topic": "/camera_l/color/image_raw"},
            {"id": "front", "topic": "/camera_f/color/image_raw"},
            {"id": "right", "topic": "/camera_r/color/image_raw"},
        ],
        "joint_names": ["j"] * 14,
        "joint_units": ["rad"] * 14,
    }
    context = service._provider_context(
        {"runtime_id": "agilex_aloha_agx", "provider": "agilex", "profile": stale}
    )
    assert context["cameras"] == {
        "left": "wrist_left",
        "front": "scene",
        "right": "wrist_right",
    }
    assert service._local_gate(context, "a")["cross_modal_ready"] is True


def test_report_page_endpoints_expose_analysis_results(runner):
    """The report page reads these two endpoints; both must be present."""

    service, store, _src = runner
    settings = service.settings
    app = create_app(settings, background=False)
    app.state.analyses = service
    headers = {"Authorization": "Bearer " + settings.token}
    with TestClient(app) as http:
        created = http.post(
            "/api/jobs/job-1/analyses", headers=headers, json={"mode": "local"}
        )
        aid = created.json()["id"]
        row = store.get("analysis", aid)
        store.put(
            "analysis",
            aid,
            dict(
                row,
                state="COMPLETED",
                verdict="REVIEW",
                checks={
                    "stream_pairing": {
                        "assessed": 5,
                        "crossed": 0,
                        "undefined": 0,
                        "not_assessed": 0,
                        "skipped": 0,
                    },
                    "recorded_jumps": {"clean": 5, "flagged": 0, "skipped": 0},
                    "gripper_channels": {"clean": 5, "flagged": 0, "skipped": 0},
                    "capture_qc_flags": 0,
                    "capture_qc_not_assessed": 25,
                },
                cross_modal={"cross_modal_ready": True, "reason": None},
                episodes=5,
            ),
        )
        listed = http.get("/api/jobs/job-1/analyses", headers=headers).json()
        assert listed[0]["verdict"] == "REVIEW"
        detail = http.get(f"/api/analyses/{aid}", headers=headers).json()
        assert detail["checks"]["stream_pairing"]["assessed"] == 5
        assert detail["cross_modal"]["cross_modal_ready"] is True
        assert detail["episodes"] == 5


def test_layout_inapplicable_reasons_are_not_coverage_gaps(runner):
    """A check that cannot exist on this layout is not a missing-evidence gap."""

    service, _store, tmp = runner
    episodes = tmp / "episodes"
    write_episode(
        episodes,
        "episode_000000",
        pairing={"left_vs_left": 0.4, "crossed": False},
        jumps={"flagged": False},
        grippers={"flagged": False},
        not_assessed={
            "largest_action_not_in_video": (
                "the recording has joint angles but no gripper pose, which this "
                "check needs"
            ),
            "invalid_action_shape": (
                "we compute the actions from the recorded state, so their shape is "
                "always valid"
            ),
        },
    )
    summary = service._summarize(episodes)
    assert summary["checks"]["capture_qc_layout_not_applicable"] == 2
    assert summary["checks"]["capture_qc_missing_evidence"] == 0


def test_missing_evidence_is_still_incomplete(runner):
    service, _store, tmp = runner
    episodes = tmp / "episodes"
    write_episode(
        episodes,
        "episode_000000",
        pairing={"left_vs_left": 0.4, "crossed": False},
        jumps={"flagged": False},
        grippers={"flagged": False},
        not_assessed={
            "normalized_gripper_out_of_range": (
                "the gripper reading's unit is not known for this dataset, and this "
                "check needs an opening from 0 to 1"
            )
        },
    )
    summary = service._summarize(episodes)
    assert summary["checks"]["capture_qc_missing_evidence"] == 1
    assert summary["checks"]["capture_qc_layout_not_applicable"] == 0

    import json as _json

    clean = {
        "episodes": 1,
        "checks": {
            "stream_pairing": {
                "assessed": 1,
                "not_assessed": 0,
                "crossed": 0,
                "undefined": 0,
                "skipped": 0,
            },
            "recorded_jumps": {
                "flagged": 0,
                "clean": 1,
                "not_assessed": 0,
                "skipped": 0,
            },
            "gripper_channels": {"flagged": 0, "clean": 1, "skipped": 0},
            "capture_qc_flags": 0,
            "capture_qc_not_assessed": 1,
            "capture_qc_missing_evidence": 1,
        },
        "not_assessed": {},
    }
    row = service.enqueue("job-1", mode="local")
    service._finish(row, tmp / "w", _json.loads(_json.dumps(clean)), {})
    assert row["verdict"] == VERDICT_INCOMPLETE


def test_layout_classifier_matches_argus_wording(runner):
    """The classifier must follow Argus' own reason phrases, not an approximation."""

    service, _store, _tmp = runner
    absent = [
        "the recording has joint angles but no gripper pose, which this check needs",
        "action times are taken from the frame times, which are checked on their own",
        "we compute the actions from the recorded state, so their shape is always valid",
        "the pipeline keeps no separate full-rate copy of the signals to check",
    ]
    gaps = [
        "the gripper reading's unit is not known for this dataset, and this check "
        "needs an opening from 0 to 1",
        # Frame times come from k/fps, so the recording keeps no independent clock
        # to compare against: a real gap, and the check is worth reporting.
        "the frame times supplied to this check use frame number divided by frame rate",
        "The recorded actor identities do not establish one actor for each mounted camera.",
    ]
    for reason in absent:
        assert service._layout_not_applicable(reason), reason
    for reason in gaps:
        assert not service._layout_not_applicable(reason), reason
