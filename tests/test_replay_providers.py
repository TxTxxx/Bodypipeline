"""Prove the common replay API works without AgileX arrays, HDF5 or ROS."""

import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import h5py
import pytest
from fastapi.testclient import TestClient

from pipeline.client.app import create_app
from pipeline.client.replay import ReplayService
from pipeline.client.replay_providers import registry
from pipeline.client.replay_providers.agilex import AgileXReplayProvider
from pipeline.client.replay_providers.base import ReplayCapabilities
from pipeline.client.service import CaptureService
from pipeline.shared.core import file_hash


class FixtureReplayProvider:
    """Test-only three-axis robot, irregular timestamps and JSON trajectories."""

    capabilities = ReplayCapabilities(
        id="fixture_xyz",
        version="1",
        label="Three-axis fixture",
        robot_types=("fixture_xyz",),
        file_extensions=(".traj",),
        input_description="Three-axis JSON trajectory",
        simulated=True,
    )

    def __init__(self, config, profile):
        self.config = config
        self.profile = profile

    def probe(self, path):
        data = json.loads(path.read_text())
        if data["robot_type"] != self.profile["robot_type"]:
            return {"supported": False, "error": "wrong robot", "frames": 0}
        assert all(len(row) == 3 for row in data["positions"])
        return {
            "supported": True,
            "frames": len(data["positions"]),
            "format": "fixture_xyz_v1",
            "description": "Irregular three-axis data",
            "metadata": {"axes": ["x", "y", "z"], "units": "m"},
        }

    def prepare(self, path, cache):
        cache.mkdir(parents=True, exist_ok=True)
        target = cache / "prepared.traj"
        target.write_bytes(path.read_bytes())
        return target

    def command(self, path):
        # This process prints progress only; no device imports or networking.
        frames = self.probe(path)["frames"]
        code = (
            "import json; [print(json.dumps({'submitted': i+1}),flush=True) "
            f"for i in range({frames})]"
        )
        return [sys.executable, "-u", "-c", code]

    @staticmethod
    def frame(line):
        return json.loads(line)["submitted"] - 1 if line.startswith("{") else None

    @staticmethod
    def error(line):
        return "fixture failure" if line.startswith("FATAL") else None


@pytest.fixture
def fixture_provider(monkeypatch):
    monkeypatch.setattr(registry, "_PROVIDERS", dict(registry._PROVIDERS))
    registry.register_provider(FixtureReplayProvider)


def test_different_robot_and_format_use_same_client_api(
    settings_factory, tmp_path, fixture_provider
):
    data = tmp_path / "trajectories"
    data.mkdir()
    path = data / "motion.traj"
    path.write_text(
        json.dumps(
            {
                "robot_type": "fixture_xyz",
                "timestamps": [0, 0.1, 0.35],
                "positions": [[1, 2, 3], [2, 3, 4], [3, 4, 5]],
            }
        )
    )
    before = file_hash(path)
    (data / "wrong.hdf5").write_text("not a supported format")
    settings = settings_factory(
        replay={
            "enabled": True,
            "provider": "fixture_xyz",
            "roots": {"files": str(data)},
        }
    )
    profile = json.loads(Path(settings.profile_path).read_text())
    profile.update(robot_type="fixture_xyz", joint_names=["x", "y", "z"])
    Path(settings.profile_path).write_text(json.dumps(profile))
    app = create_app(settings, background=False)
    with TestClient(app) as http:
        headers = {"Authorization": "Bearer " + settings.token}
        status = http.get("/api/replay/status", headers=headers).json()
        assert status["provider"]["id"] == "fixture_xyz"
        assert status["simulated"]  # derived from capability, not a magic provider name
        files = http.get(
            "/api/replay/files", params={"root": "files"}, headers=headers
        ).json()
        assert [row["name"] for row in files["entries"]] == ["motion.traj"]
        info = http.get(
            "/api/replay/inspect",
            params={"root": "files", "path": path.name},
            headers=headers,
        ).json()
        assert info["supported"] and "source_arms" not in info
        body = {"root": "files", "path": path.name, "request_id": "fixture-request-001"}
        assert (
            http.post("/api/replay/start", json=body, headers=headers).status_code
            == 202
        )
        deadline = time.monotonic() + 5
        while True:
            result = http.get("/api/replay/status", headers=headers).json()
            if result["state"] not in {"PREPARING", "RUNNING"}:
                break
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert result["state"] == "COMPLETED", result
        assert result["frame"] == 3 and result["fps"] is None
        assert result["source_metadata"]["axes"] == ["x", "y", "z"]
        assert result["provider_id"] == "fixture_xyz"
        assert "/fixture_xyz/1/" in result["prepared_path"]
        assert (
            http.post("/api/replay/start", json=body, headers=headers).json()[
                "started_at"
            ]
            == result["started_at"]
        )
    assert file_hash(path) == before


@pytest.mark.parametrize(
    "config,profile,mode,match",
    [
        ({"enabled": True}, {"robot_type": "agilex_aloha"}, "ros2", "显式配置"),
        ({"provider": "missing"}, {"robot_type": "agilex_aloha"}, "ros2", "未知"),
        ({"provider": "agilex_tool"}, {"robot_type": "arx_x5"}, "ros2", "不支持"),
        (
            {"provider": "simulated"},
            {"robot_type": "agilex_aloha"},
            "ros2",
            "mock Client",
        ),
        ({"provider": ["agilex_tool"]}, {"robot_type": "agilex_aloha"}, "mock", "未知"),
    ],
)
def test_invalid_bindings_never_fall_back(config, profile, mode, match):
    with pytest.raises(ValueError, match=match):
        registry.create_provider(config, profile, mode)


def test_disabled_unknown_robot_can_still_use_client(settings_factory):
    settings = settings_factory()
    capture = CaptureService(settings)
    capture.profile["robot_type"] = "unimplemented_robot"
    replay = ReplayService(settings, capture)
    assert replay.provider is None and not replay.status()["enabled"]
    assert replay.status()["provider"] is None
    assert not replay.inspect("capture:default", "missing.traj")["supported"]


def test_simulation_capability_is_enforced_for_new_providers(fixture_provider):
    with pytest.raises(ValueError, match="mock Client"):
        registry.create_provider(
            {"provider": "fixture_xyz"}, {"robot_type": "fixture_xyz"}, "ros2"
        )


def test_agilex_rejects_other_robot_metadata(tmp_path):
    path = tmp_path / "wrong.hdf5"
    with h5py.File(path, "w") as handle:
        handle.attrs["robot_type"] = "arx_x5"
    info = AgileXReplayProvider({}).probe(path)
    assert not info["supported"] and "robot_type" in info["error"]


def test_registry_duplicates_and_cache_versions(settings_factory, fixture_provider):
    with pytest.raises(ValueError, match="已注册"):
        registry.register_provider(FixtureReplayProvider)
    settings = settings_factory(replay={"enabled": True, "provider": "agilex_tool"})
    capture = CaptureService(settings)
    first = ReplayService(settings, capture)

    class AgileXV2(AgileXReplayProvider):
        capabilities = replace(AgileXReplayProvider.capabilities, version="2")

    registry.register_provider(AgileXV2, replace=True)
    second = ReplayService(settings, capture)
    assert first.cache != second.cache
    assert first.cache.name == "1" and second.cache.name == "2"


def test_new_provider_fatal_output_is_not_agilex_specific(
    settings_factory, fixture_provider
):
    settings = settings_factory(replay={"enabled": True, "provider": "fixture_xyz"})
    capture = CaptureService(settings)
    capture.profile["robot_type"] = "fixture_xyz"
    replay = ReplayService(settings, capture)
    replay.row.update(state="RUNNING", frame=0, frames=2)
    import subprocess

    process = subprocess.Popen(
        [sys.executable, "-u", "-c", "print('FATAL native runner failed')"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    replay._run(process)
    assert replay.status()["state"] == "FAILED"
    assert replay.status()["error"] == "fixture failure"
