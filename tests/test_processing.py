import hashlib
import json
import subprocess
import time
from pathlib import Path

import h5py
import pytest

from pipeline.client.service import CaptureService
from pipeline.server.alignment import prepare_agilex
from pipeline.server.ingest import IngestService
from pipeline.server.jobs import JobRunner
from pipeline.shared.core import DomainError


def captured(settings_factory, duration=0.3):
    c = CaptureService(settings_factory("client"))
    r = c.create(
        {
            "instruction": "fold cloth",
            "idempotency_key": "capture-test",
            "local_root": "default",
            "remote_target": "default",
        }
    )
    c.start(r["id"])
    time.sleep(duration)
    c.stop(r["id"])
    r = c.keep(r["id"])
    raw = Path(r["raw_root"])
    m = json.loads((raw.parent / "manifest.json").read_text())
    return raw, m


def upload_sample(ingest, raw, m):
    uid = ingest.create(m, "default", "")["id"]
    upload_root = raw if m["files"][0]["path"].startswith("episode0/") else raw.parent
    for i, f in enumerate(m["files"]):
        data = (upload_root / f["path"]).read_bytes()
        chunk_size = ingest.settings.chunk_bytes
        for offset in range(0, len(data), chunk_size):
            chunk = data[offset : offset + chunk_size]
            ingest.chunk(uid, i, offset, hashlib.sha256(chunk).hexdigest(), chunk)
    ingest.complete(uid)
    return uid


def test_mock_full_processing_not_fake_lerobot(settings_factory, tmp_path):
    raw, m = captured(settings_factory)
    s = settings_factory("server")
    ingest = IngestService(s)
    uid = upload_sample(ingest, raw, m)
    runner = JobRunner(s, ingest)
    job = runner.enqueue(uid)
    assert runner.enqueue(uid)["id"] == job["id"]
    assert runner.tick()
    result = ingest.store.get("job", job["id"])
    assert result["state"] == "DEMO_READY", result
    assert result["alignment_report"]["frames"] >= 2
    assert "doctor_report" not in result
    assert not (s.root / "datasets").exists() or not any(
        (s.root / "datasets").iterdir()
    )


def test_real_data_blocked_without_semantic_approval(settings_factory):
    raw, m = captured(settings_factory)
    m["capture_mode"] = "ros2"
    s = settings_factory("server")
    ingest = IngestService(s)
    uid = upload_sample(ingest, raw, m)
    runner = JobRunner(s, ingest)
    job = runner.enqueue(uid)
    runner.tick()
    assert ingest.store.get("job", job["id"])["state"] == "BLOCKED"


def test_alignment_rejects_temporal_gap(settings_factory, tmp_path):
    raw, m = captured(settings_factory)
    d = raw / "episode0/camera/color/front"
    files = sorted(d.glob("*.jpg"))
    for f in files[1:-1]:
        f.unlink()
    with pytest.raises(DomainError, match="时间对齐越界"):
        prepare_agilex(raw, tmp_path / "aligned", m["profile"])


def test_alignment_rejects_nan(settings_factory, tmp_path):
    raw, m = captured(settings_factory)
    f = sorted((raw / "episode0/arm/jointState/masterLeft").glob("*.json"))[0]
    f.write_text(json.dumps({"position": [float("nan")] * 7}))
    with pytest.raises(DomainError, match="有限 position"):
        prepare_agilex(raw, tmp_path / "aligned", m["profile"])


def test_real_letools_optional_integration(settings_factory, letools_binary):
    """Runs actual converter + deep validator + Doctor if installed; never touches ROS."""
    binary = letools_binary
    raw, m = captured(settings_factory)
    s = settings_factory("server", processing_mode="real", letools_bin=binary)
    ingest = IngestService(s)
    uid = upload_sample(ingest, raw, m)
    runner = JobRunner(s, ingest)
    job = runner.enqueue(uid)
    runner.tick()
    result = ingest.store.get("job", job["id"])
    assert result["state"] == "QUARANTINED", result
    assert Path(result["dataset_path"], "meta/info.json").is_file()
    work = Path(result["work_path"])
    aligned = work / "aligned/episode0.hdf5"
    assert aligned.is_file()
    assert not (work / "sources/0/aligned").exists()
    with h5py.File(aligned, "r") as handle:
        assert handle.attrs["schema"] == "bodypipeline.agilex.aligned_hdf5.v1"
        expected_shape = (result["alignment_report"]["frames"], 14)
        assert handle["action"].shape == expected_shape
        assert handle["observation.state"].shape == expected_shape
        assert "observation.images.left" in handle
        assert "observation.images.front" in handle
        assert "observation.images.right" in handle
    assert json.loads((work / "validate.json").read_text())["valid"]
    assert result["doctor_report"]["total_episodes"] == 1
    assert (
        result["quality_gate"]["coverage"]["frames"]
        == result["alignment_report"]["frames"]
    )
    assert result["doctor_report"]["overall_severity"] == "WARN"
    # PyAV belongs to the isolated toolchain, not the Client/Server environment.
    probe = subprocess.run(
        [
            str(Path(binary).with_name("python")),
            "-c",
            "import av, json, sys; from pathlib import Path; codecs = []; "
            "\nfor video in Path(sys.argv[1]).rglob('*.mp4'):"
            "\n    with av.open(str(video)) as container:"
            "\n        codecs.append(container.streams.video[0].codec_context.name)"
            "\nprint(json.dumps(codecs))",
            str(work / "dataset/videos"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(probe.stdout) == ["h264"] * 3
    assert not (s.root / "datasets").exists() or not any(
        (s.root / "datasets").iterdir()
    )


def test_real_letools_positive_gate_for_synthetic_only(
    settings_factory, letools_binary
):
    binary = letools_binary
    raw, m = captured(settings_factory, duration=3.6)
    s = settings_factory("server", processing_mode="real", letools_bin=binary)
    ingest = IngestService(s)
    uid = upload_sample(ingest, raw, m)
    runner = JobRunner(s, ingest)
    job = runner.enqueue(uid)
    runner.tick()
    result = ingest.store.get("job", job["id"])
    assert result["state"] == "SYNTHETIC_READY", result
    assert result["quality_gate"]["passed"]
    assert result["quality_gate"]["skipped"]
    assert Path(result["dataset_path"], "meta/info.json").exists()
    assert not (s.root / "datasets").exists() or not any(
        (s.root / "datasets").iterdir()
    )


def test_worker_recovery_keeps_work_directory_as_diagnostics(settings_factory):
    s = settings_factory("server")
    ingest = IngestService(s)
    runner = JobRunner(s, ingest)
    job = {"id": "interrupted-test", "state": "RUNNING", "work_path": "/work/item"}
    ingest.store.put("job", job["id"], job)
    runner.recover()
    recovered = ingest.store.get("job", job["id"])
    assert recovered["state"] == "INTERRUPTED"
    assert "诊断数据保留" in recovered["error"]
    assert not (s.root / "datasets").exists() or not any(
        (s.root / "datasets").iterdir()
    )
