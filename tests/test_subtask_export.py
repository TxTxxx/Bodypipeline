"""Subtask export: frame planning, queue behaviour and a real LeRobot round trip."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pipeline.server import subtask_export
from pipeline.server.app import create_app
from pipeline.server.datasets import dataset_for_job
from pipeline.server.merges import source_snapshot
from pipeline.server.recycle import dataset_lock
from pipeline.server.subtask_export import (
    UNANNOTATED_TEXT,
    build_plan,
)
from pipeline.shared.core import DomainError

SCENARIO = Path(__file__).parent / "fixtures/merge_scenario.py"


def admin(settings):
    return {"Authorization": "Bearer " + settings.token}


def event(t_s, end_s, verb, obj="", contribution="advancing"):
    return {
        "t_s": t_s,
        "end_s": end_s,
        "verb_class": verb,
        "object": obj,
        "contribution": contribution,
    }


def seed_annotation(app, aid="ann", episodes=None, path=None, **updates):
    """Register a completed annotation with Argus-shaped episode results."""

    settings = app.state.runner.settings
    store = app.state.runner.store
    job = store.get("job", "source")
    work = Path(path or settings.root / "annotations" / aid)
    (work / "argus/qa").mkdir(parents=True, exist_ok=True)
    for index, labels in (episodes or {0: [event(0.0, 0.4, "grasp")]}).items():
        episode_id = f"episode_{index:06d}"
        context = work / "argus/episodes" / episode_id
        context.mkdir(parents=True, exist_ok=True)
        (context / "context.json").write_text(
            json.dumps(
                {
                    "episode_id": episode_id,
                    "episode_index": index,
                    "fps": 10.0,
                    "n_state_frames": 6 if index == 0 else 5,
                }
            )
        )
        (work / "argus/qa" / f"{episode_id}.json").write_text(
            json.dumps(
                {
                    "event_labels": labels,
                    "duration_s": 0.6 if index == 0 else 0.5,
                    "_meta": {"episode_id": episode_id},
                }
            )
        )
    row = {
        "id": aid,
        "job_id": job["id"],
        "dataset_name": job.get("dataset_name"),
        "dataset_path": str(dataset_for_job(settings, job)),
        "robot_type": job.get("robot_type"),
        "state": "COMPLETED",
        "work_path": str(work),
        "episodes_done": len(episodes or {0: []}),
        **updates,
    }
    store.put("annotation", aid, row)
    return row


@pytest.fixture
def subtask_app(settings_factory, monkeypatch):
    settings = settings_factory("server", letools_bin=sys.executable)
    monkeypatch.setattr(
        "pipeline.server.subtask_export.executable_path", lambda _: sys.executable
    )
    return create_app(settings, background=False)


def seed_job(app, jid="source", version="v3.0", episodes=2, frames=11):
    settings = app.state.runner.settings
    path = settings.root / "work" / jid / "dataset"
    (path / "meta").mkdir(parents=True)
    (path / "meta/info.json").write_text(
        json.dumps(
            {
                "codebase_version": version,
                "robot_type": "test_arm",
                "fps": 10,
                "total_episodes": episodes,
                "total_frames": frames,
                "features": {"action": {"dtype": "float32", "shape": [2]}},
            }
        )
    )
    job = {
        "id": jid,
        "state": "CONVERTED",
        "operation": "conversion",
        "dataset_name": "fixture",
        "robot_type": "test_arm",
        "dataset_path": str(path),
        "created_at": 1.0,
    }
    app.state.runner.store.put("job", jid, job)
    return job, path


# --------------------------------------------------------------------- planning
def test_frame_bounds_are_half_open_and_clamped():
    bounds = subtask_export._frame_bounds
    assert bounds(0.0, 1.5, 30, 804) == (0, 45)
    assert bounds(1.5, 4.5, 30, 804) == (45, 135)
    # A span shorter than one frame still owns the frame that starts inside it.
    assert bounds(0.01, 0.02, 10, 10) == (1, 1)
    assert bounds(0.0, 99.0, 10, 10) == (0, 10)
    assert bounds(-1.0, 0.5, 10, 10) == (0, 5)


def test_timeline_splits_overlaps_and_merges_adjacent_equal_text():
    events = subtask_export._events(
        {
            "event_labels": [
                event(0.0, 2.0, "hold position", contribution="idle"),
                event(1.0, 3.0, "turn the screw"),
                event(3.0, 4.0, "turn the screw"),
                event(5.0, 6.0, "retreat", contribution="wasteful"),
            ]
        }
    )
    spans = subtask_export._timeline(events, 6.0)
    assert spans == [
        (0.0, 1.0, "hold position"),
        (1.0, 2.0, "hold position | turn the screw"),
        (2.0, 4.0, "turn the screw"),
        (5.0, 6.0, "retreat"),
    ]


def test_uncovered_frames_stay_unannotated_instead_of_guessing():
    vocabulary = {}
    spans = [(0.5, 1.0, "grasp"), (2.0, 2.5, "place")]
    ranges = subtask_export._frame_ranges(spans, 10, 30, vocabulary)
    assert ranges == [[5, 10, 0], [20, 25, 1]]
    assert subtask_export._covered_frames(ranges) == 10
    assert vocabulary == {"grasp": 0, "place": 1}


def test_plan_reports_coverage_vocabulary_and_skips(subtask_app):
    seed_job(subtask_app)
    row = seed_annotation(
        subtask_app,
        episodes={
            0: [event(0.0, 0.2, "grasp"), event(0.2, 0.4, "turn the screw")],
            1: [event(0.1, 0.3, "grasp")],
        },
    )
    plan = build_plan(subtask_app.state.runner.settings, row)
    assert plan["dataset"]["total_episodes"] == 2
    assert [ep["episode_index"] for ep in plan["episodes"]] == [0, 1]
    assert plan["episodes"][0]["segments"] == [[0, 2, 0], [2, 4, 1]]
    assert plan["episodes"][1]["segments"] == [[1, 3, 0]]
    # One vocabulary is shared by every episode.
    assert [item["subtask"] for item in plan["vocabulary"]] == ["grasp", "turn the screw"]
    assert plan["coverage"] == {
        "annotated_episodes": 2,
        "dataset_episodes": 2,
        "unannotated_episodes": 0,
        "frames": 11,
        "labelled_frames": 6,
        "unannotated_frames": 5,
        "ratio": round(6 / 11, 6),
        "labelled_ratio": 1.0,
    }
    assert plan["unannotated_index"] == -1
    assert plan["unannotated_text"] == UNANNOTATED_TEXT


def test_plan_skips_failed_labels_and_unknown_episodes(subtask_app):
    seed_job(subtask_app)
    row = seed_annotation(subtask_app, episodes={})
    work = Path(row["work_path"])
    (work / "argus/qa/episode_000000.json").write_text(
        json.dumps({"parse_ok": False, "no_reply": True, "event_labels": []})
    )
    (work / "argus/qa/episode_000009.json").write_text(
        json.dumps({"event_labels": [event(0.0, 0.2, "grasp")]})
    )
    plan = build_plan(subtask_app.state.runner.settings, row)
    assert plan["episodes"] == []
    reasons = {item["episode_id"]: item["reason"] for item in plan["skipped"]}
    assert "模型未返回可用标注" in reasons["episode_000000"]
    assert "数据集没有 Episode 9" in reasons["episode_000009"]


def test_plan_rejects_missing_episode_total(subtask_app):
    job, path = seed_job(subtask_app)
    (path / "meta/info.json").write_text(json.dumps({"codebase_version": "v3.0"}))
    row = seed_annotation(subtask_app)
    with pytest.raises(DomainError, match="Episode 总数"):
        build_plan(subtask_app.state.runner.settings, row)


def test_segment_text_does_not_repeat_the_object(subtask_app):
    assert subtask_export._segment_text(
        {"verb_class": "grasp screwdriver", "object": "electric screwdriver"}
    ) == "grasp screwdriver"
    assert subtask_export._segment_text(
        {"verb_class": "move toward enclosure", "object": "black box"}
    ) == "move toward enclosure black box"


# ----------------------------------------------------------------------- queue
def test_preview_and_enqueue_require_labels_and_a_free_annotation(subtask_app):
    seed_job(subtask_app)
    row = seed_annotation(subtask_app)
    runner = subtask_app.state.runner
    preview = runner.subtask_exports.preview(row["id"])
    assert preview["available"] and preview["can_export"]
    assert preview["summary"]["annotated_episodes"] == 1
    assert preview["summary"]["unannotated_text"] == UNANNOTATED_TEXT

    running = dict(row, state="RUNNING")
    subtask_app.state.runner.store.put("annotation", row["id"], running)
    with pytest.raises(DomainError, match="仍在运行"):
        runner.subtask_exports.enqueue(row["id"])
    preview = runner.subtask_exports.preview(row["id"])
    assert preview["can_export"] is False and preview["annotations_running"] is True


def test_enqueue_names_the_copy_and_is_idempotent(subtask_app):
    seed_job(subtask_app)
    row = seed_annotation(subtask_app)
    runner = subtask_app.state.runner
    job = runner.subtask_exports.enqueue(row["id"])
    assert job["dataset_name"] == "fixture-subtasks"
    assert job["operation"] == "subtask_export"
    assert job["state"] == "QUEUED"
    assert job["source_annotation_id"] == row["id"]
    assert job["source_format"] == "v3.0"
    assert job["target_format"] == "v3.0"
    assert runner.subtask_exports.enqueue(row["id"])["id"] == job["id"]
    named = runner.subtask_exports.enqueue(row["id"], "  自定义 名称 ")
    assert named["id"] != job["id"] and named["dataset_name"] == "自定义 名称"
    assert [item["id"] for item in runner.subtask_exports.listing(row["id"])] == [
        named["id"],
        job["id"],
    ]


def test_enqueue_rejects_a_dataset_that_moved(subtask_app):
    seed_job(subtask_app)
    row = seed_annotation(subtask_app)
    subtask_app.state.runner.store.put(
        "annotation", row["id"], dict(row, dataset_path="/nowhere/else")
    )
    with pytest.raises(DomainError, match="路径已变化"):
        subtask_app.state.runner.subtask_exports.enqueue(row["id"])


def test_enqueue_requires_the_letools_python(settings_factory, monkeypatch):
    settings = settings_factory("server", letools_bin="/bin/true")
    app = create_app(settings, background=False)
    seed_job(app)
    row = seed_annotation(app)
    monkeypatch.setattr(
        "pipeline.server.subtask_export.executable_path", lambda _: "/bin/true"
    )
    with pytest.raises(DomainError, match="LeTools Python"):
        app.state.runner.subtask_exports.enqueue(row["id"])


def test_api_preview_and_start(subtask_app):
    seed_job(subtask_app)
    row = seed_annotation(subtask_app)
    client = TestClient(subtask_app)
    headers = admin(subtask_app.state.runner.settings)
    preview = client.get(f"/api/annotations/{row['id']}/subtask-export-preview", headers=headers)
    assert preview.status_code == 200
    assert preview.json()["can_export"] is True
    assert client.get("/api/annotations/missing/subtask-export-preview", headers=headers).status_code == 404
    started = client.post(
        f"/api/annotations/{row['id']}/subtask-exports",
        json={"name": None},
        headers=headers,
    )
    assert started.status_code == 202
    body = started.json()
    assert body["dataset_name"] == "fixture-subtasks"
    assert body["operation"] == "subtask_export"
    # The API key never exists here, but the record must not grow one either.
    assert "api_key" not in body


def test_run_failure_keeps_source_and_never_publishes(subtask_app, monkeypatch):
    job, path = seed_job(subtask_app)
    row = seed_annotation(subtask_app)
    runner = subtask_app.state.runner
    before = source_snapshot(path)
    opened, closed = [], []
    monkeypatch.setattr(
        runner, "_open_allocation", lambda work: opened.append(work) or None
    )
    monkeypatch.setattr(runner, "_close_allocation", lambda: closed.append(True))

    def command(args, work, name, allocation=None):
        report, err = work / f"{name}.json", work / f"{name}.stderr.log"
        report.write_text("")
        err.write_text("boom")
        return 1, report, err

    monkeypatch.setattr(runner, "command", command)
    export = runner.subtask_exports.enqueue(row["id"])
    runner.tick()
    result = runner.store.get("job", export["id"])
    assert result["state"] == "FAILED" and "boom" in result["error"]
    assert opened and len(closed) == 1
    assert source_snapshot(path) == before
    assert not (Path(result["work_path"]) / "dataset").exists()


def test_run_rejects_a_plan_that_changed_after_enqueue(subtask_app, monkeypatch):
    seed_job(subtask_app)
    row = seed_annotation(subtask_app)
    runner = subtask_app.state.runner
    monkeypatch.setattr(runner, "_open_allocation", lambda work: None)
    monkeypatch.setattr(runner, "_close_allocation", lambda: None)
    export = runner.subtask_exports.enqueue(row["id"])
    work = Path(row["work_path"])
    (work / "argus/qa/episode_000000.json").write_text(
        json.dumps({"event_labels": [event(0.0, 0.5, "another step")]})
    )
    runner.tick()
    result = runner.store.get("job", export["id"])
    assert result["state"] == "FAILED"
    assert "标注结果在提交后发生变化" in result["error"]


def test_runner_holds_the_dataset_lock_and_validates_the_output(
    subtask_app, monkeypatch
):
    job, path = seed_job(subtask_app)
    row = seed_annotation(subtask_app)
    runner = subtask_app.state.runner
    monkeypatch.setattr(runner, "_open_allocation", lambda work: "alloc")
    monkeypatch.setattr(runner, "_close_allocation", lambda: None)
    seen = []

    def command(args, work, name, allocation=None):
        assert allocation == "alloc"
        with pytest.raises(DomainError, match="正在处理"):
            with dataset_lock(runner.store, runner.subtask_exports._key(path)):
                pass
        report, err = work / f"{name}.json", work / f"{name}.stderr.log"
        err.write_text("")
        if name == "validate":
            report.write_text("")
        else:
            pending = work / "pending-dataset"
            (pending / "meta").mkdir(parents=True)
            (pending / "meta/info.json").write_text(
                json.dumps(
                    {
                        "codebase_version": "v3.0",
                        "total_episodes": 2,
                        "total_frames": 11,
                        "features": {"subtask_index": {"dtype": "int64"}},
                    }
                )
            )
            report.write_text('{"validated": true, "episodes": 2, "frames": 11}')
        seen.append(name)
        return 0, report, err

    monkeypatch.setattr(runner, "command", command)
    export = runner.subtask_exports.enqueue(row["id"])
    runner.tick()
    result = runner.store.get("job", export["id"])
    assert result["state"] == "CONVERTED", result.get("error")
    assert seen == ["subtask-export", "validate"]
    assert result["dataset_ready"] is True
    published = Path(result["dataset_path"])
    assert (published / "meta/info.json").is_file()
    lineage = json.loads((published / "meta/bodypipeline-subtask-export.json").read_text())
    assert lineage["annotation_id"] == row["id"]
    assert lineage["target_format"] == "v3.0"
    assert dataset_for_job(runner.settings, result) == published


def test_run_rejects_a_pending_dataset_without_the_feature(subtask_app, monkeypatch):
    job, _ = seed_job(subtask_app)
    row = seed_annotation(subtask_app)
    runner = subtask_app.state.runner
    monkeypatch.setattr(runner, "_open_allocation", lambda work: None)
    monkeypatch.setattr(runner, "_close_allocation", lambda: None)

    def command(args, work, name, allocation=None):
        report, err = work / f"{name}.json", work / f"{name}.stderr.log"
        err.write_text("")
        if name == "validate":
            report.write_text("")
        else:
            pending = work / "pending-dataset"
            (pending / "meta").mkdir(parents=True)
            (pending / "meta/info.json").write_text(
                json.dumps(
                    {
                        "codebase_version": "v3.0",
                        "total_episodes": 2,
                        "total_frames": 11,
                        "features": {},
                    }
                )
            )
            report.write_text('{"validated": true}')
        return 0, report, err

    monkeypatch.setattr(runner, "command", command)
    export = runner.subtask_exports.enqueue(row["id"])
    runner.tick()
    result = runner.store.get("job", export["id"])
    assert result["state"] == "FAILED"
    assert "特征声明" in result["error"]
    assert not (Path(result["work_path"]) / "dataset").exists()


# ------------------------------------------------------------- real round trip
@pytest.mark.parametrize("version", ["v3.0", "v2.1"])
def test_real_export_writes_contiguous_subtask_index(
    settings_factory, tmp_path, letools_binary, version
):
    python = str(Path(letools_binary).with_name("python"))
    fixtures = tmp_path / "fixtures"
    subprocess.run(
        [python, str(SCENARIO), "create", str(fixtures), version], check=True
    )
    data_root = tmp_path / "shared"
    data_root.mkdir()
    settings = settings_factory(
        "server",
        letools_bin=letools_binary,
        data_root=data_root,
        import_roots=[fixtures],
    )
    app = create_app(settings, background=False)
    runner = app.state.runner
    source = fixtures / "input-0"
    digest_before = {
        str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in source.rglob("*")
        if p.is_file()
    }
    job = {
        "id": "source",
        "state": "CONVERTED",
        "operation": "conversion",
        "dataset_name": "fixture",
        "robot_type": "test_arm",
        "dataset_path": str(source),
        "created_at": 1.0,
    }
    runner.store.put("job", job["id"], job)
    row = seed_annotation(
        app,
        aid="real-ann",
        path=tmp_path / "annotation",
        episodes={
            0: [event(0.0, 0.3, "grasp"), event(0.3, 0.5, "place")],
            1: [event(0.2, 0.4, "grasp")],
        },
    )
    export = runner.subtask_exports.enqueue(row["id"])
    runner.tick()
    result = runner.store.get("job", export["id"])
    assert result["state"] == "CONVERTED", result.get("error")

    published = Path(result["dataset_path"])
    info = json.loads((published / "meta/info.json").read_text())
    assert info["codebase_version"] == "v3.0"
    assert info["features"]["subtask_index"] == {
        "dtype": "int64",
        "shape": [1],
        "names": None,
    }
    assert info["total_episodes"] == 2 and info["total_frames"] == 11
    # The source dataset is untouched.
    assert {
        str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in source.rglob("*")
        if p.is_file()
    } == digest_before

    checked = subprocess.run(
        [letools_binary, "validate", str(published), "--deep"],
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 0, checked.stderr

    inspect = subprocess.run(
        [
            python,
            "-c",
            (
                "import json,sys\n"
                "import pyarrow.parquet as pq\n"
                "root=sys.argv[1]\n"
                "rows=[]\n"
                "from pathlib import Path\n"
                "root=Path(root)\n"
                "for shard in sorted((root/'data').rglob('*.parquet')):\n"
                "    d=pq.read_table(shard, columns=['episode_index','frame_index','subtask_index']).to_pydict()\n"
                "    rows += list(zip(d['episode_index'], d['frame_index'], d['subtask_index']))\n"
                "vocab=pq.read_table(root/'meta/subtasks.parquet').to_pydict()\n"
                "print(json.dumps({'rows': rows, 'vocab': vocab}))\n"
            ),
            str(published),
        ],
        capture_output=True,
        text=True,
    )
    assert inspect.returncode == 0, inspect.stderr
    payload = json.loads(inspect.stdout)
    rows = payload["rows"]
    by_episode = {}
    for episode, frame, subtask in rows:
        by_episode.setdefault(episode, {})[frame] = subtask
    assert sorted(by_episode) == [0, 1]
    # Episode 0: 6 frames at 10 fps, labels for [0.0,0.3) and [0.3,0.5).
    assert [by_episode[0][frame] for frame in range(6)] == [0, 0, 0, 1, 1, -1]
    # Episode 1: 5 frames, only [0.2,0.4) is labelled; the rest stays -1.
    assert [by_episode[1][frame] for frame in range(5)] == [-1, -1, 0, 0, -1]
    vocabulary = dict(zip(payload["vocab"]["subtask_index"], payload["vocab"]["subtask"]))
    assert vocabulary[0] == "grasp"
    assert vocabulary[1] == "place"
    assert vocabulary[-1] == UNANNOTATED_TEXT
    assert result["subtask_report"]["unannotated_frames"] == 4
    assert result["subtask_report"]["validated"] is True
