"""Optional exact pinned Argus + actual video + loopback model integration."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from pipeline.shared.core import atomic_json, digest


@pytest.mark.parametrize("unlimited", [False, True], ids=["capped", "unlimited"])
def test_pinned_argus_trial_then_full_reuses_success_and_refines_real_frames(
    tmp_path, unlimited
):
    root = Path(__file__).resolve().parents[1]
    argus = Path(
        os.environ.get("PIPELINE_TEST_ARGUS_ROOT", root / ".runtime/vendor/argus")
    )
    python = Path(
        os.environ.get(
            "PIPELINE_TEST_ARGUS_PYTHON", root / ".runtime/argus-venv/bin/python"
        )
    )
    source = root / ".runtime/body2-preview/dataset"
    if not (
        python.is_file()
        and (argus / "label/pipeline_runtime.py").is_file()
        and source.is_dir()
        and (root / ".runtime/toolchain/usr/bin/ffmpeg").is_file()
    ):
        pytest.skip(
            "requires pinned patched Argus, synthetic numbered fixture and local ffmpeg; see preview_workbench.py"
        )
    template = {
        "id": "probe",
        "name": "合成抓取",
        "subtasks": [
            {
                "id": "grab",
                "name": "抓取",
                "definition": "synthetic fixture only",
                "startCondition": "numbered frame evidence",
                "endCondition": "numbered frame evidence",
            }
        ],
        "fields": [],
    }
    atomic_json(
        tmp_path / "task-template.json",
        {
            "version": 2,
            "template": template,
            "signature": digest(template),
            "boundary_target_s": 0.2,
            "boundary_window_s": 0.5,
        },
    )
    result = subprocess.run(
        [
            str(python),
            str(root / "tests/fixtures/argus_workbench_probe.py"),
            str(argus),
            str(tmp_path),
            str(source),
        ],
        capture_output=True,
        text=True,
        timeout=420,
        env={
            **os.environ,
            "PIPELINE_TEST_ARGUS_UNLIMITED": "1" if unlimited else "0",
        },
    )
    assert result.returncode == 0, result.stderr[-7000:] + result.stdout[-2000:]
    summary = json.loads(result.stdout.splitlines()[-1])
    assert summary["native_parser"] and summary["native_retry"]
    assert summary["continuation_rejected_before_retry"]
    assert summary["mixed_transport_sends"] == 4
    assert summary["trial_success_reused"] and summary["coarse_requests"] == 15
    assert summary["fine_requests"] >= 12
    assert summary["trial_logical_sends"] == 4
    assert summary["unlimited_cli"] == unlimited
    assert summary["cap_usd"] == (None if unlimited else 1)
    if unlimited:
        assert summary["trial_cost_usd"] > 20
