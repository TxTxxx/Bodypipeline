"""Conversion options, position relabeling, and immutable source regression tests."""

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import h5py
import numpy as np
import pytest
from fastapi.testclient import TestClient
from test_aloha_hdf5_import import hdf5_settings, profile, write_episode
from test_conversion_workbench import seed_upload

from pipeline.server.app import create_app
from pipeline.server.conversion_options import normalize_options
from pipeline.server.jobs import JobRunner
from pipeline.server.providers.aloha_hdf5 import AlohaHDF5Provider
from pipeline.server.providers.gripper_closure import (
    DEFAULT_PARAMETERS,
    capabilities,
    correct_actions,
    holding_mask,
)
from pipeline.shared.core import DomainError


def trajectory(fps=30):
    """Open, close, hold, release, open, empty close, idle."""
    scale = fps / 30
    return np.concatenate(
        [
            np.full(round(20 * scale), -1.4),
            np.linspace(-1.4, -0.7, round(12 * scale)),
            np.full(round(45 * scale), -0.7),
            np.linspace(-0.7, -1.4, round(12 * scale)),
            np.full(round(20 * scale), -1.4),
            np.linspace(-1.4, -0.05, round(12 * scale)),
            np.full(round(20 * scale), -0.05),
        ]
    ).astype(np.float32)


def arrays():
    x = trajectory()
    rng = np.random.default_rng(123)
    state = rng.normal(size=(len(x), 14)).astype(np.float32)
    state[:, 6] = x
    state[:, 13] = x
    return state, state.copy()


def source_episode(path):
    state, action = arrays()
    write_episode(path, frames=len(state))
    with h5py.File(path, "r+") as handle:
        handle["observations/qpos"][:] = state
        handle["action"][:] = action
    return state, action


@pytest.mark.parametrize("fps", [15, 30, 60])
def test_detects_holding_platform_without_relabeling_open_release_or_idle(fps):
    x = trajectory(fps)
    mask = holding_mask(x, fps, 0, DEFAULT_PARAMETERS)
    assert mask.any()
    # All selected frames lie on the holding platform, not the closing ramp.
    assert np.all(x[mask] == np.float32(-0.7))
    assert not mask[: round(25 * fps / 30)].any()
    assert not mask[round(80 * fps / 30) :].any()
    assert mask[round(40 * fps / 30) : round(65 * fps / 30)].all()


@pytest.mark.parametrize(
    "x",
    [
        np.full(100, -0.7),  # Cropped static initial holding pose is not a new grasp.
        np.r_[np.full(20, -1.4), np.linspace(-1.4, -0.05, 12), np.full(50, -0.05)],
        np.array([-1.4, -0.7]),  # Too short to infer holding.
        np.r_[np.full(20, -1.4), np.linspace(-1.4, -0.7, 12), np.full(5, -0.7)],
    ],
)
def test_ambiguous_or_empty_closing_is_unchanged(x):
    assert not holding_mask(x, 30, 0, DEFAULT_PARAMETERS).any()


def test_two_grasps_remain_separate_and_opening_motion_is_unchanged():
    x = np.tile(trajectory(), 2)
    mask = holding_mask(x, 30, 0, DEFAULT_PARAMETERS)
    assert mask[: len(x) // 2].any() and mask[len(x) // 2 :].any()
    assert np.all(x[mask] == np.float32(-0.7))


def test_minimum_closure_only_changes_profile_grippers_and_never_weakens_target():
    state, action = arrays()
    original_state = state.copy()
    action[42:50, 13] = -0.15  # Already more closed than the requested 90%.
    original_action = action.copy()
    revised, report = correct_actions(state, action, profile(), {"minimum_percent": 90})
    assert np.array_equal(state, original_state)
    assert np.array_equal(action, original_action)
    assert np.array_equal(
        revised[:, [*range(6), *range(7, 13)]], action[:, [*range(6), *range(7, 13)]]
    )
    assert np.all(revised[:, [6, 13]] >= action[:, [6, 13]])
    assert np.all(revised[42:50, 13] == np.float32(-0.15))
    assert report["channels"]["left_gripper"]["target"] == float(np.float32(-0.314))
    assert (
        report["channels"]["right_gripper"]["changed_frames"]
        < report["channels"]["left_gripper"]["changed_frames"]
    )
    assert report["changed_frames_any_channel"] > 0
    changed = (revised != action).any(axis=1)
    assert np.all(state[changed, 6] == np.float32(-0.7))
    unchanged, no_op = correct_actions(state, action, profile(), {"minimum_percent": 0})
    assert np.array_equal(unchanged, action)
    assert no_op["changed_frames_any_channel"] == 0 and no_op["warnings"]


def test_generic_channel_mapping_supports_other_dimensions_and_reversed_direction():
    p = profile()
    p["robot_type"] = "test_six_axis"
    p["joint_names"] = ["j0", "finger", "j2", "j3", "j4", "j5"]
    p["joint_units"] = ["raw"] * 6
    p["conversion"]["gripper_closure"]["channels"] = [
        {"joint": "finger", "open": 3.14, "closed": 0}
    ]
    state = np.zeros((len(trajectory()), 6), dtype=np.float32)
    state[:, 1] = -trajectory()
    action = state.copy()
    action[45:50, 1] = 0.1
    revised, report = correct_actions(state, action, p, {"minimum_percent": 95})
    assert np.all(revised[:, 1] <= action[:, 1])
    assert revised[40, 1] == np.float32(0.157)
    assert np.array_equal(revised[45:50], action[45:50])
    assert report["channels"]["finger"]["dimension"] == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update(algorithm="unknown"),
        lambda c: c["channels"][0].update(joint="missing_joint"),
        lambda c: c["channels"].append(copy.deepcopy(c["channels"][0])),
        lambda c: c["channels"][0].update(open=0, closed=0),
        lambda c: c["channels"][0].update(open=float("nan")),
        lambda c: c.update(phase_parameters={"median_frames": 4}),
        lambda c: c.update(phase_parameters={"typo": 1}),
    ],
)
def test_invalid_trusted_configuration_fails_closed(mutate):
    p = profile()
    mutate(p["conversion"]["gripper_closure"])
    with pytest.raises(DomainError):
        capabilities(p)


def test_provider_preserves_source_and_feedback_and_reports_actual_changes(tmp_path):
    source = tmp_path / "episode.hdf5"
    state, action = source_episode(source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    provider = AlohaHDF5Provider()
    baseline = provider.prepare(source, tmp_path / "baseline", profile())
    adjusted = provider.prepare(
        source,
        tmp_path / "adjusted",
        profile(),
        options={"gripper_closure": {"minimum_percent": 90}},
    )
    with (
        h5py.File(baseline["aligned_path"], "r") as old,
        h5py.File(adjusted["aligned_path"], "r") as new,
    ):
        assert np.array_equal(old["action"][:], action)
        assert np.array_equal(new["observation.state"][:], state)
        assert not np.array_equal(new["action"][:], action)
        for key in old:
            if key.startswith("observation.images."):
                assert all(np.array_equal(a, b) for a, b in zip(old[key], new[key]))
    assert "action_corrections" not in baseline
    assert (
        adjusted["action_corrections"]["gripper_closure"]["changed_frames_any_channel"]
        > 0
    )
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    p = profile()
    p.pop("conversion")
    with pytest.raises(DomainError, match="不支持"):
        provider.prepare(
            source,
            tmp_path / "unsupported",
            p,
            options={"gripper_closure": {"minimum_percent": 90}},
        )


def test_api_capability_validation_dedup_snapshot_and_lineage(
    settings_factory, tmp_path
):
    settings = hdf5_settings(settings_factory, tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    source_episode(source / "episode_0.hdf5")
    source_episode(source / "episode_1.hdf5")
    app = create_app(settings, background=False)
    admin = {"Authorization": "Bearer " + settings.token}
    with TestClient(app) as http:
        runtime = http.get("/api/config", headers=admin).json()["runtimes"][0]
        assert (
            runtime["conversion_capabilities"]["gripper_closure"]["default_percent"]
            == 90
        )
        assert "channels" not in runtime["conversion_capabilities"]["gripper_closure"]
        imported = http.post(
            "/api/imports",
            headers=admin,
            json={"path": str(source), "runtime_id": "arx"},
        ).json()
        path = f"/api/collections/{imported['collection_id']}/process"
        body = {"instruction": "Unscrew the screw.", "run_doctor": False}
        baseline = http.post(path, headers=admin, json=body).json()
        assert baseline["conversion_options"] == {}
        assert (
            http.post(
                path,
                headers=admin,
                json={**body, "conversion_options": {"gripper_closure": None}},
            ).json()["id"]
            == baseline["id"]
        )
        jobs = []
        for percent in (90, 95):
            response = http.post(
                path,
                headers=admin,
                json={
                    **body,
                    "conversion_options": {
                        "gripper_closure": {"minimum_percent": percent}
                    },
                },
            )
            assert response.status_code == 202, response.text
            jobs.append(response.json())
            again = http.post(
                path,
                headers=admin,
                json={
                    **body,
                    "conversion_options": {
                        "gripper_closure": {"minimum_percent": float(percent)}
                    },
                },
            )
            assert again.json()["id"] == jobs[-1]["id"]
        assert len({baseline["id"], jobs[0]["id"], jobs[1]["id"]}) == 3
        for value in (-1, 101, True, "90", None):
            response = http.post(
                path,
                headers=admin,
                json={
                    **body,
                    "conversion_options": {
                        "gripper_closure": {"minimum_percent": value}
                    },
                },
            )
            assert response.status_code == 422, response.text
        assert (
            http.post(
                path,
                headers=admin,
                json={
                    **body,
                    "conversion_options": {
                        "gripper_closure": {"minimum_percent": 90, "dimensions": [6]}
                    },
                },
            ).status_code
            == 422
        )
        assert (
            http.post(
                path,
                headers=admin,
                json={**body, "conversion_options": {"arbitrary_option": {}}},
            ).status_code
            == 422
        )
    # Execute the actual queued snapshots, including the disabled baseline.
    # Closing the HTTP lifespan intentionally stops its worker; use a fresh one.
    # A later deployment edit must not change targets of already queued jobs.
    settings.profiles["arx"]["profile"]["conversion"]["gripper_closure"]["channels"][0][
        "open"
    ] = -6.28
    runner = JobRunner(settings, app.state.ingest)
    for _ in range(3):
        runner.tick()
    result = app.state.ingest.store.get("job", jobs[0]["id"])
    assert result["state"] == "DEMO_READY", result
    assert result["conversion_options"]["gripper_closure"]["minimum_percent"] == 90
    assert len(result["alignment_report"]["episodes"]) == 2
    lineage = json.loads((Path(result["work_path"]) / "lineage.json").read_text())
    assert lineage["conversion_options"] == result["conversion_options"]
    assert len(lineage["action_corrections"]) == 2
    for report in lineage["action_corrections"]:
        assert report["gripper_closure"]["minimum_percent"] == 90
        assert report["gripper_closure"]["changed_frames_any_channel"] > 0
        assert report["gripper_closure"]["channels"]["left_gripper"]["target"] == float(
            np.float32(-0.314)
        )
    # Work products retain the established shared read/write permission policy.
    assert (
        Path(result["work_path"]) / "aligned/episode0.hdf5"
    ).stat().st_mode & 0o777 == 0o666


def test_unsupported_runtime_rejects_options_at_api_and_runner(settings_factory):
    settings = settings_factory("server")
    app = create_app(settings, background=False)
    uid = seed_upload(app.state.ingest, settings)
    options = {"gripper_closure": {"minimum_percent": 90}}
    with TestClient(app) as http:
        admin = {"Authorization": "Bearer " + settings.token}
        assert (
            http.get("/api/config", headers=admin).json()["runtimes"][0][
                "conversion_capabilities"
            ]
            == {}
        )
        response = http.post(
            f"/api/uploads/{uid}/process",
            headers=admin,
            json={"conversion_options": options},
        )
        assert response.status_code == 400 and "不支持" in response.text
    with pytest.raises(DomainError, match="不支持"):
        app.state.runner.enqueue(uid, conversion_options=options)
    assert app.state.ingest.store.all("job") == []


@pytest.mark.parametrize(
    "percent", [float("nan"), float("inf"), -float("inf"), True, "90"]
)
def test_runner_rejects_nonfinite_or_coerced_percentages(percent):
    with pytest.raises(DomainError):
        normalize_options(
            {"gripper_closure": {"minimum_percent": percent}}, capabilities(profile())
        )


@pytest.mark.parametrize("target_format", ["v2.1", "v3.0"])
def test_real_conversion_contains_corrected_actions_and_fresh_statistics(
    settings_factory, tmp_path, letools_binary, target_format
):
    settings = hdf5_settings(settings_factory, tmp_path)
    settings.processing_mode = "real"
    settings.letools_bin = letools_binary
    source = tmp_path / "source"
    source.mkdir()
    state, action = source_episode(source / "episode_0.hdf5")
    app = create_app(settings, background=False)
    imported = app.state.imports.import_path(str(source), runtime_id="arx")
    runner = app.state.runner
    job = runner.enqueue_collection(
        imported["collection_id"],
        "Unscrew the screw.",
        target_format,
        False,
        conversion_options={"gripper_closure": {"minimum_percent": 90}},
    )
    runner.tick()
    result = app.state.ingest.store.get("job", job["id"])
    assert result["state"] == "CONVERTED", result
    dataset = Path(result["dataset_path"])
    # Inspect through the isolated LeTools interpreter (Arrow is not an API dependency).
    script = """
import json,sys
from pathlib import Path
import numpy as np, pyarrow.parquet as pq
root=Path(sys.argv[1])
tables=[pq.read_table(p) for p in sorted((root/'data').rglob('*.parquet'))]
action=np.concatenate([np.asarray(t['action'].to_pylist()) for t in tables])
state=np.concatenate([np.asarray(t['observation.state'].to_pylist()) for t in tables])
stats_path=root/'meta/stats.json'
stats=json.loads(stats_path.read_text()) if stats_path.exists() else json.loads((root/'meta/episodes_stats.jsonl').read_text().splitlines()[0])['stats']
print(json.dumps({'action':action.tolist(),'state':state.tolist(),'stats':stats['action']}))
"""
    python = str(Path(letools_binary).parent / "python")
    payload = json.loads(
        subprocess.check_output([python, "-c", script, str(dataset)], text=True)
    )
    revised = np.asarray(payload["action"], dtype=np.float32)
    assert np.array_equal(payload["state"], state)
    assert np.all(revised[:, [6, 13]] >= action[:, [6, 13]])
    assert revised[45, 6] == np.float32(-0.314)
    assert np.array_equal(revised[:, 0], action[:, 0])
    assert np.allclose(payload["stats"]["mean"], revised.mean(axis=0), atol=1e-6)
    assert np.allclose(payload["stats"]["min"], revised.min(axis=0))
    assert dataset.stat().st_mode & 0o777 == 0o777
