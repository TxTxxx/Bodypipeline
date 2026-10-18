import json
import os
from pathlib import Path

import pytest

from pipeline.shared.config import Settings
from pipeline.shared.core import executable_path


@pytest.fixture
def letools_binary():
    """Use the separate server toolchain without installing it into the API venv."""
    override = os.environ.get("PIPELINE_TEST_LETOOLS_BIN")
    if override:
        binary = executable_path(override)
        if not binary:
            pytest.fail("PIPELINE_TEST_LETOOLS_BIN does not name an executable")
        return binary
    local = Path(__file__).resolve().parents[1] / ".runtime/letools-venv/bin/letools"
    binary = executable_path(str(local)) or executable_path("letools")
    if not binary:
        pytest.skip("optional toolchain missing; run bash scripts/install_letools.sh")
    return binary


@pytest.fixture
def settings_factory(tmp_path):
    def make(role="client", **kwargs):
        profile = json.loads(Path("configs/agilex-aloha.json").read_text())
        profile["minimum_frames"] = 2
        path = tmp_path / f"profile-{role}.json"
        path.write_text(json.dumps(profile))
        base = dict(
            role=role,
            root=(tmp_path / role).resolve(),
            token="human-" + "x" * 32,
            device_token="device-" + "x" * 32,
            profile_path=str(path),
            min_free_bytes=0,
            processing_mode="demo",
            poll_seconds=0.05,
        )
        base.update(kwargs)
        return Settings(**base)

    return make
