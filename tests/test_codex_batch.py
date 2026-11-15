"""Optional exact Argus + real tiny videos + controlled CLI queue integration.

No real account/model request. Fixture data is generated outside user state.
"""

import json
import os
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import pytest

from pipeline.server.annotations import AnnotationRunner
from pipeline.server.task_annotations import TemplateLibrary, TemplateRequest
from pipeline.shared.config import Settings
from pipeline.shared.core import Store

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def batch(tmp_path, monkeypatch):
    argus = Path(
        os.environ.get("PIPELINE_TEST_ARGUS_ROOT", ROOT / ".runtime/vendor/argus")
    )
    python = ROOT / ".runtime/argus-venv/bin/python"
    source = ROOT / ".runtime/codex-batch-fixture/dataset"
    tools = ROOT / ".runtime/toolchain/usr"
    if not all(
        p.exists()
        for p in (
            argus / "label/pipeline_codex.py",
            python,
            source,
            tools / "bin/ffmpeg",
        )
    ):
        pytest.skip(
            "requires patched Argus and isolated three-episode CPU fixture; see docs/local-codex-annotation.md"
        )
    for key, value in {
        "FFMPEG": str(tools / "bin/ffmpeg"),
        "FFPROBE": str(tools / "bin/ffprobe"),
        "LD_LIBRARY_PATH": str(tools / "lib/x86_64-linux-gnu"),
        "CUDA_VISIBLE_DEVICES": "",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PATH", str(tools / "bin") + os.pathsep + os.environ["PATH"])
    cli = tmp_path / "codex-fixture"
    cli.write_text(
        f"#!{sys.executable}\n"
        + (ROOT / "tests/fixtures/fake_codex_cli.py")
        .read_text()
        .replace("from __future__ import annotations", "")
    )
    cli.chmod(0o755)
    control = tmp_path / "control.json"
    control.write_text('{"delay":0.3}')
    settings = Settings(
        role="server",
        root=tmp_path / "state",
        import_roots=[source.parent],
        argus_src=str(argus),
        argus_python=str(python),
        argus_annotation_unlimited=True,
        codex_bin=str(cli),
        codex_model="fixture-model",
        codex_models=["fixture-model"],
        codex_reasoning="high",
        profile_path=str(ROOT / "configs/agilex-aloha.json"),
        letools_bin=str(ROOT / ".runtime/letools-venv/bin/letools"),
        min_free_bytes=0,
        argus_annotation_timeout=90,
    )
    store = Store(settings.root / "state")
    store.put(
        "job",
        "fixture",
        {
            "id": "fixture",
            "state": "CONVERTED",
            "dataset_ready": True,
            "dataset_path": str(source),
            "dataset_name": "Controlled CLI fixture",
            "robot_type": "synthetic_arm",
            "target_format": "v3.0",
        },
    )
    TemplateLibrary(store).save(
        TemplateRequest(
            revision=0,
            templates=[
                {
                    "id": "fixture",
                    "name": "Controlled CLI fixture",
                    "subtasks": [{"id": "align", "name": "Align"}],
                    "fields": [],
                }
            ],
        )
    )
    return AnnotationRunner(settings, store), tmp_path, control


def calls(root):
    path = root / "invocations.jsonl"
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


def start_row(runner, concurrency=2):
    return runner.enqueue(
        "fixture",
        backend="codex",
        template_id="fixture",
        run_scope="selection",
        episodes=[0, 1, 2],
        concurrency=concurrency,
        sample_fps=3,
    )


def test_exact_argus_batch_bound_and_explicit_auth_resume_reuses_success(batch):
    runner, root, control = batch
    control.write_text(
        json.dumps({"delay": 0.3, "episodes": {"episode_000002": "authentication"}})
    )
    row = runner.run(start_row(runner))
    assert row["state"] == "PARTIAL", row.get("error")
    assert row["episodes_done"] == 2 and row["episodes_failed"] == 1
    assert "未认证" in row["error"]
    initial = Counter(r["episode"] for r in calls(root) if r["phase"] == "start")
    assert initial == {"episode_000000": 1, "episode_000001": 1, "episode_000002": 1}
    active = peak = 0
    # A rejected invocation has no finish event; account for its process exit
    # with the durable CLI receipt rather than mistaking it for an orphan.
    events = []
    for r in row["cli_calls"]:
        events += [(r["started_at"], 1), (r["finished_at"], -1)]
    for _, delta in sorted(events):
        active += delta
        peak = max(peak, active)
    assert 1 <= peak <= 2
    control.write_text('{"delay":0.1}')
    resumed = runner.resume(row["id"])
    result = runner.run(resumed)
    assert result["state"] == "COMPLETED", result.get("error")
    final = Counter(r["episode"] for r in calls(root) if r["phase"] == "start")
    assert final == {"episode_000000": 1, "episode_000001": 1, "episode_000002": 2}
    assert result["cli_call_count"] == 4 and result["cost_usd"] is None
    assert result["cli_usage"]["input_tokens"] == 300
    assert len(runner.workbench_candidates(row["id"])["candidates"]) == 3


@pytest.mark.parametrize("stop_kind", ["cancel", "restart"])
def test_cancel_and_restart_only_own_group_preserve_success_and_pending_work(
    batch, stop_kind
):
    runner, root, control = batch
    control.write_text('{"delay":10}')
    queued = start_row(runner, concurrency=1)
    thread = threading.Thread(target=runner.run, args=(queued,))
    thread.start()
    deadline = time.monotonic() + 50
    while not calls(root) and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert calls(root), runner.store.get("annotation", queued["id"])
    if stop_kind == "cancel":
        runner.cancel(queued["id"])
    else:
        runner.shutdown_requested.set()
    thread.join(timeout=15)
    assert not thread.is_alive()
    result = runner.store.get("annotation", queued["id"])
    assert result["state"] == ("CANCELLED" if stop_kind == "cancel" else "INTERRUPTED")
    started = calls(root)[0]["pid"]
    # kill(pid,0) can see a reparented zombie briefly; a live process cannot remain.
    stat = Path(f"/proc/{started}/stat")
    assert not stat.exists() or stat.read_text().split()[2] == "Z"
    assert result["cli_calls"][0]["status"] == "claimed"
    # Recreate the runner as a server restart would, keeping exactly this state.
    control.write_text('{"delay":0.1}')
    recovered = AnnotationRunner(runner.settings, runner.store)
    recovered.recover()
    final = recovered.run(recovered.resume(queued["id"]))
    assert final["state"] == "PARTIAL", final.get("error")
    counts = Counter(r["episode"] for r in calls(root) if r["phase"] == "start")
    assert counts["episode_000000"] == 1  # unknown in-flight charge is NOT resent
    assert counts["episode_000001"] == counts["episode_000002"] == 1
    assert final["episodes_done"] == 2 and final["cost_usd"] is None


def test_budget_splits_two_camera_episode_and_resumes_only_failed_part(batch):
    runner, root, control = batch
    runner.settings.codex_max_part_seconds = 1.5
    control.write_text(
        json.dumps(
            {"delay": 0.05, "episodes": {"episode_000000__p02": "authentication"}}
        )
    )
    queued = runner.enqueue(
        "fixture",
        backend="codex",
        template_id="fixture",
        run_scope="trial",
        episodes=[0],
        concurrency=1,
        sample_fps=3,
    )
    first = runner.run(queued)
    assert first["state"] == "PARTIAL", first.get("error")
    work = Path(first["work_path"])
    parts = sorted((work / "argus/pieces").glob("episode_000000__p*/context.json"))
    assert len(parts) > 1
    for path in parts:
        ctx = json.loads(path.read_text())
        assert len(ctx["cameras"]) == 2
        assert ctx["n_state_frames"] / ctx["fps"] <= 1.5 + 1e-6
    before = Counter(r["episode"] for r in calls(root) if r["phase"] == "start")
    control.write_text('{"delay":0.05}')
    final = runner.run(runner.resume(first["id"]))
    assert final["state"] == "COMPLETED", final.get("error")
    after = Counter(r["episode"] for r in calls(root) if r["phase"] == "start")
    assert after["episode_000000__p01"] == before["episode_000000__p01"] == 1
    assert after["episode_000000__p02"] == 2
    assert all(
        r["images"] <= runner.settings.codex_max_images for r in final["cli_calls"]
    )
    document = runner.episode(final["id"], "episode_000000")["task_document"]
    assert max(s["end"] for s in document["segments"]) == 48
