import json
import subprocess
import sys
import threading
import time

import h5py
import numpy as np
import pytest

from pipeline.client.app import create_app
from pipeline.client.replay_providers.agilex import AgileXReplayProvider as AgileXTool
from pipeline.client.replay_providers.agilex_formats import (
    ARMS,
    PUPPET_ARMS,
    REPLAY_SCHEMA,
    prepare,
)
from pipeline.client.replay_providers.agilex_runner import tool_arguments
from pipeline.shared.core import file_hash
from pipeline.shared.hdf5 import AGILEX_ALIGNED_SCHEMA, AGILEX_HDF5_SCHEMA


def raw_package(path):
    with h5py.File(path, "w") as h:
        h.attrs["schema"] = AGILEX_HDF5_SCHEMA
        for i, arm in enumerate(ARMS):
            h[f"streams/arm/jointState/{arm}/timestamps"] = [10 + i * 0.01, 10.1, 10.2]
            values = np.arange(21, dtype=np.float32).reshape(3, 7) - 2
            h[f"streams/arm/jointState/{arm}/position"] = values + i * 100


def test_raw_adaptation_preserves_values_and_uses_causal_samples(tmp_path):
    source = tmp_path / "episode.hdf5"
    raw_package(source)
    before = file_hash(source)
    target = prepare(source, tmp_path / "cache", 10)
    with h5py.File(source) as src, h5py.File(target) as dst:
        assert dst.attrs["schema"] == REPLAY_SCHEMA
        assert dst["size"][()] == 2
        assert dst.attrs["source_start"] == 10.01
        assert dst.attrs["fps"] == 10
        for arm in ARMS:
            np.testing.assert_array_equal(
                dst[f"arm/jointStatePosition/{arm}"][:],
                src[f"streams/arm/jointState/{arm}/position"][:2],
            )
        assert "camera" not in dst  # do not fabricate absent streams
    assert file_hash(source) == before
    assert prepare(source, tmp_path / "cache", 10) == target


def test_aligned_action_order_and_fps(tmp_path):
    source = tmp_path / "aligned.hdf5"
    action = np.arange(42).reshape(3, 14)
    with h5py.File(source, "w") as h:
        h.attrs["schema"] = AGILEX_ALIGNED_SCHEMA
        h.attrs["fps"] = 15
        h["action"] = action
        h["observation.state"] = action + 1000
    target = prepare(source, tmp_path / "cache")
    with h5py.File(target) as h:
        assert h.attrs["fps"] == 15
        np.testing.assert_array_equal(
            h["arm/jointStatePosition/masterLeft"][:], action[:, :7]
        )
        np.testing.assert_array_equal(
            h["arm/jointStatePosition/masterRight"][:], action[:, 7:]
        )
    state_target = prepare(source, tmp_path / "cache", trajectory_source="state")
    assert state_target != target
    with h5py.File(state_target) as h:
        for i, arm in enumerate(ARMS):
            np.testing.assert_array_equal(
                h[f"arm/jointStatePosition/{arm}"][:],
                action[:, i * 7 : (i + 1) * 7] + 1000,
            )


def test_puppet_source_uses_own_timestamps_and_separate_cache(tmp_path):
    source, cache = tmp_path / "raw.hdf5", tmp_path / "cache"
    raw_package(source)
    with h5py.File(source, "a") as h:
        for i, arm in enumerate(PUPPET_ARMS):
            h[f"streams/arm/jointState/{arm}/timestamps"] = [10.05, 10.15, 10.25]
            h[f"streams/arm/jointState/{arm}/position"] = (
                np.arange(21).reshape(3, 7) + 1000 + i * 100
            )
    before = file_hash(source)
    master = prepare(source, cache, 10)
    # Simulate the previous release's cache, which must also never be reused.
    old = cache / f"{before}-10-v1" / "episode.hdf5"
    old.parent.mkdir()
    old.write_bytes(master.read_bytes())
    tool = AgileXTool({"trajectory_source": "state"}, {"output_fps": 10})
    info = tool.probe(source)
    assert info["supported"] and info["source_arms"] == list(PUPPET_ARMS)
    assert info["source_start"] == 10.05
    target = tool.prepare(source, cache)
    assert target not in {master, old}
    with h5py.File(source) as src, h5py.File(target) as dst:
        assert dst.attrs["trajectory_source"] == "state"
        assert dst["size"][()] == 3
        for arm, output_arm in zip(PUPPET_ARMS, ARMS, strict=True):
            np.testing.assert_array_equal(
                dst[f"arm/jointStatePosition/{output_arm}"][:],
                src[f"streams/arm/jointState/{arm}/position"][:],
            )
    assert file_hash(source) == before
    assert tool.prepare(source, cache) == target


def test_missing_puppet_never_falls_back_to_master(tmp_path):
    source = tmp_path / "raw.hdf5"
    raw_package(source)
    assert not AgileXTool({"trajectory_source": "state"}).probe(source)["supported"]
    with pytest.raises(ValueError, match="trajectory_source"):
        AgileXTool({"trajectory_source": "typo"})


def test_legacy_puppet_mapping(tmp_path):
    source = tmp_path / "legacy.hdf5"
    with h5py.File(source, "w") as h:
        h["size"] = 2
        for i, arm in enumerate(PUPPET_ARMS):
            h[f"arm/jointStatePosition/{arm}"] = np.arange(14).reshape(2, 7) + i * 100
    target = prepare(source, tmp_path / "cache", trajectory_source="state")
    assert target != source
    with h5py.File(source) as src, h5py.File(target) as dst:
        for arm, output_arm in zip(PUPPET_ARMS, ARMS, strict=True):
            np.testing.assert_array_equal(
                dst[f"arm/jointStatePosition/{output_arm}"][:],
                src[f"arm/jointStatePosition/{arm}"][:],
            )


def test_original_operator_receives_only_available_command_streams():
    args = tool_arguments("episode.hdf5", 30)
    assert args.arm_joint_state_names == list(ARMS)
    assert args.arm_joint_state_topics == ["/joint_left_states", "/joint_right_states"]
    assert args.localization_pose_names == []
    assert args.camera_color_names == []
    assert args.publishIndex == -1


def test_runner_loads_existing_operator_without_reimplementing_it(tmp_path):
    from pathlib import Path

    tool = tmp_path / "original_tool.py"
    tool.write_text("""
import json
class RosOperator:
    def __init__(self, args): print(json.dumps(vars(args)), flush=True)
    def destroy_node(self): pass
class rclpy:
    @staticmethod
    def init(): pass
    @staticmethod
    def spin(node): pass
    @staticmethod
    def ok(): return True
    @staticmethod
    def shutdown(): pass
""")
    runner = Path("pipeline/client/replay_providers/agilex_runner.py")
    result = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--tool",
            str(tool),
            "--dataset",
            "example.hdf5",
            "--fps",
            "30",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    values = json.loads(result.stdout)
    assert values["datasetDir"] == "example.hdf5"
    assert values["arm_joint_state_names"] == list(ARMS)


def test_platform_capture_to_simulated_replay(settings_factory):
    settings = settings_factory(replay={"enabled": True, "provider": "simulated"})
    app = create_app(settings, background=False)
    capture, replay = app.state.service, app.state.replay
    row = capture.create(
        dict(
            idempotency_key="replay-e2e-001",
            dataset_name="折叠毛巾",
            instruction="",
            local_root="default",
            remote_target="default",
        )
    )
    capture.start(row["id"])
    time.sleep(0.15)
    capture.stop(row["id"])
    capture.keep(row["id"])
    record = replay.captures()[0]
    assert record["id"] == row["id"]
    assert record["dataset_name"] == "折叠毛巾"
    assert (
        replay.inspect(record["root"], record["path"])["format"] == "bodypipeline_raw"
    )
    replay.start(record["root"], record["path"], "replay-e2e-001")
    deadline = time.monotonic() + 10
    while replay.status()["state"] in {"PREPARING", "RUNNING"}:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert replay.status()["state"] == "COMPLETED", replay.status()
    assert replay.status()["simulated"]
    assert replay.status()["frame"] > 0
    replay.close()


def test_stop_during_preparation_never_launches_tool(settings_factory, tmp_path):
    settings = settings_factory(
        replay={
            "enabled": True,
            "provider": "simulated",
            "roots": {"sample": str(tmp_path)},
        }
    )
    app = create_app(settings, background=False)
    replay = app.state.replay
    source = tmp_path / "episode.hdf5"
    raw_package(source)
    started, release = threading.Event(), threading.Event()

    def prepare_blocked(path, cache):
        started.set()
        release.wait(3)
        return path

    replay.provider.prepare = prepare_blocked
    replay.start("sample", source.name, "stop-prep-001")
    assert started.wait(2)
    replay.stop()
    release.set()
    replay.close()
    assert replay.status()["state"] == "STOPPED"
    assert replay.process is None
