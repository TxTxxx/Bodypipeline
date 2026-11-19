"""Server drafts for structured per-Episode language instructions."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pipeline.server.app import create_app
from pipeline.server.instruction_review import InstructionReview
from pipeline.server.merges import source_snapshot
from pipeline.server.recycle import dataset_lock
from pipeline.server.viewer_data import PreviewRegistry


def instruction_template():
    return {
        "parts": [
            {"type": "text", "text": "请将"},
            {"type": "variable", "key": "object"},
            {"type": "text", "text": "放入"},
            {"type": "variable", "key": "target"},
        ],
        "variables": {
            "object": {
                "label": "物体",
                "options": ["红色方块", "蓝色圆柱"],
                "default": "红色方块",
            },
            "target": {
                "label": "目标位置",
                "options": ["左侧盒子", "右侧盒子"],
                "default": "左侧盒子",
            },
        },
    }


@pytest.fixture
def instruction_app(settings_factory, monkeypatch):
    settings = settings_factory("server")
    app = create_app(settings, background=False)
    store = app.state.runner.store
    path = settings.root / "source"
    (path / "meta").mkdir(parents=True)
    (path / "meta/info.json").write_text(
        json.dumps(
            {
                "codebase_version": "v3.0",
                "fps": 10,
                "total_episodes": 2,
                "total_frames": 11,
                "robot_type": "test_arm",
            }
        )
    )
    store.put(
        "job",
        "source",
        {
            "id": "source",
            "state": "CONVERTED",
            "dataset_path": str(path),
            "dataset_name": "fixture",
            "created_at": 1,
            "robot_type": "test_arm",
        },
    )
    PreviewRegistry(store, settings.viewer_ttl).register("source", path)
    monkeypatch.setattr(
        InstructionReview,
        "_source",
        lambda *args: {
            "fps": 10,
            "tasks": [
                {"task_index": 0, "task": "pick"},
                {"task_index": 1, "task": "place"},
            ],
            "episodes": [
                {
                    "episode_index": 0,
                    "frames": 6,
                    "task_index": 0,
                    "instruction": "pick",
                },
                {
                    "episode_index": 1,
                    "frames": 5,
                    "task_index": 1,
                    "instruction": "place",
                },
            ],
        },
    )
    return app


def client_for(app):
    return TestClient(
        app,
        headers={"Authorization": "Bearer " + app.state.runner.settings.token},
    )


def versioned(payload):
    return {
        "revision": payload["revision"],
        "source_signature": payload["source_signature"],
    }


def test_initial_template_episode_selections_and_restart_persistence(instruction_app):
    client = client_for(instruction_app)
    head = client.get("/api/instructions/source").json()
    assert head["revision"] == 0
    assert head["template"]["variables"]["instruction"]["options"] == ["pick", "place"]
    assert [item["current_instruction"] for item in head["episodes"]] == ["pick", "place"]

    saved = client.put(
        "/api/instructions/source/template",
        json={**versioned(head), "template": instruction_template()},
    )
    assert saved.status_code == 200, saved.text
    saved = saved.json()
    assert saved["revision"] == 1

    episode = client.put(
        "/api/instructions/source/episodes/0",
        json={
            **versioned(saved),
            "selections": {
                "object": "蓝色圆柱",
                "target": "右侧盒子",
            },
        },
    )
    assert episode.status_code == 200, episode.text
    episode = episode.json()
    assert episode["rendered"] == "请将蓝色圆柱放入右侧盒子"
    assert episode["revision"] == 2

    restarted = create_app(instruction_app.state.runner.settings, background=False)
    loaded = client_for(restarted).get("/api/instructions/source/episodes/0").json()
    assert loaded["selections"] == {
        "object": "蓝色圆柱",
        "target": "右侧盒子",
    }
    assert loaded["rendered"] == "请将蓝色圆柱放入右侧盒子"
    assert loaded["has_draft"] is True
    store = restarted.state.runner.store
    assert len(store.all("instruction_revision")) == 2


def test_template_changes_report_removed_values_without_destroying_assignments(instruction_app):
    client = client_for(instruction_app)
    head = client.get("/api/instructions/source").json()
    head = client.put(
        "/api/instructions/source/template",
        json={**versioned(head), "template": instruction_template()},
    ).json()
    head = client.put(
        "/api/instructions/source/episodes/0",
        json={
            **versioned(head),
            "selections": {"object": "蓝色圆柱", "target": "左侧盒子"},
        },
    ).json()
    changed = instruction_template()
    changed["variables"]["object"]["options"] = ["红色方块"]
    changed["variables"]["object"]["default"] = "红色方块"
    result = client.put(
        "/api/instructions/source/template",
        json={**versioned(head), "template": changed},
    )
    assert result.status_code == 200, result.text
    assert result.json()["warnings"][0]["episode_index"] == 0
    episode = client.get("/api/instructions/source/episodes/0").json()
    assert episode["selections"]["object"] == "蓝色圆柱"
    assert episode["valid"] is False
    assert "已移除选项" in episode["error"]


def test_episode_apply_freezes_only_the_requested_episode(instruction_app):
    client = client_for(instruction_app)
    head = client.get("/api/instructions/source").json()
    head = client.put(
        "/api/instructions/source/template",
        json={**versioned(head), "template": instruction_template()},
    ).json()
    for episode, selections in (
        (0, {"object": "蓝色圆柱", "target": "右侧盒子"}),
        (1, {"object": "红色方块", "target": "左侧盒子"}),
    ):
        head = client.put(
            f"/api/instructions/source/episodes/{episode}",
            json={**versioned(head), "selections": selections},
        ).json()

    started = client.post(
        "/api/instructions/source/episodes/0/apply",
        json=versioned(head),
    )
    assert started.status_code == 202, started.text
    job = instruction_app.state.runner.store.get(
        "instruction_apply", started.json()["id"]
    )
    assert json.loads(Path(job["spec_path"]).read_text())["assignments"] == [
        {"episode_index": 0, "instruction": "请将蓝色圆柱放入右侧盒子"}
    ]


def test_active_apply_job_does_not_absorb_a_different_episode_scope(
    instruction_app,
):
    client = client_for(instruction_app)
    head = client.get("/api/instructions/source").json()
    head = client.put(
        "/api/instructions/source/template",
        json={**versioned(head), "template": instruction_template()},
    ).json()
    for episode, selections in (
        (0, {"object": "蓝色圆柱", "target": "右侧盒子"}),
        (1, {"object": "红色方块", "target": "左侧盒子"}),
    ):
        head = client.put(
            f"/api/instructions/source/episodes/{episode}",
            json={**versioned(head), "selections": selections},
        ).json()

    first = client.post(
        "/api/instructions/source/episodes/0/apply",
        json=versioned(head),
    )
    duplicate = client.post(
        "/api/instructions/source/episodes/0/apply",
        json=versioned(head),
    )
    other_scope = client.post(
        "/api/instructions/source/episodes/1/apply",
        json=versioned(head),
    )

    assert first.status_code == 202, first.text
    assert duplicate.status_code == 202, duplicate.text
    assert duplicate.json()["id"] == first.json()["id"]
    assert other_scope.status_code == 409, other_scope.text
    assert "另一批语言指令" in other_scope.json()["detail"]


def test_dataset_apply_reuses_pre_scope_upgrade_active_job(instruction_app):
    client = client_for(instruction_app)
    head = client.get("/api/instructions/source").json()
    head = client.put(
        "/api/instructions/source/template",
        json={**versioned(head), "template": instruction_template()},
    ).json()
    head = client.put(
        "/api/instructions/source/episodes/0",
        json={
            **versioned(head),
            "selections": {
                "object": "蓝色圆柱",
                "target": "右侧盒子",
            },
        },
    ).json()
    review = InstructionReview(
        instruction_app.state.runner.settings,
        instruction_app.state.runner.store,
    )
    key, path, _ = review.resolve("source")
    instruction_app.state.runner.store.put(
        "instruction_apply",
        "legacy-apply",
        {
            "id": "legacy-apply",
            "dataset_id": "source",
            "dataset_key": key,
            "dataset_path": str(path),
            "source_signature": head["source_signature"],
            "revision": head["revision"],
            "fingerprint": "fingerprint-from-scope-less-release",
            "state": "QUEUED",
        },
    )

    duplicate = client.post(
        "/api/instructions/source/apply",
        json=versioned(head),
    )

    assert duplicate.status_code == 202, duplicate.text
    assert duplicate.json()["id"] == "legacy-apply"


def test_unrelated_episode_edit_does_not_invalidate_queued_scoped_apply(
    instruction_app, monkeypatch
):
    runner = instruction_app.state.instruction_apply
    client = client_for(instruction_app)
    head = client.get("/api/instructions/source").json()
    head = client.put(
        "/api/instructions/source/template",
        json={**versioned(head), "template": instruction_template()},
    ).json()
    head = client.put(
        "/api/instructions/source/episodes/0",
        json={
            **versioned(head),
            "selections": {
                "object": "蓝色圆柱",
                "target": "右侧盒子",
            },
        },
    ).json()
    head = client.put(
        "/api/instructions/source/episodes/1",
        json={
            **versioned(head),
            "selections": {
                "object": "红色方块",
                "target": "左侧盒子",
            },
        },
    ).json()
    queued = client.post(
        "/api/instructions/source/episodes/0/apply",
        json=versioned(head),
    )
    assert queued.status_code == 202, queued.text

    saved = client.put(
        "/api/instructions/source/episodes/1",
        json={
            **versioned(head),
            "selections": {
                "object": "蓝色圆柱",
                "target": "左侧盒子",
            },
        },
    )
    assert saved.status_code == 200, saved.text
    retry = client.post(
        "/api/instructions/source/episodes/0/apply",
        json=versioned(saved.json()),
    )
    assert retry.status_code == 202, retry.text
    assert retry.json()["id"] == queued.json()["id"]

    monkeypatch.setattr(
        runner,
        "_worker",
        lambda action, *_args: (
            {"recovered": []}
            if action == "recover"
            else {
                "updated_episodes": [0],
                "rewritten_shards": 1,
                "total_tasks": 2,
                "noop": False,
            }
        ),
    )
    runner.tick()
    completed = runner.store.get("instruction_apply", queued.json()["id"])
    assert completed["state"] == "CONVERTED", completed
    assert completed["report"]["updated_episodes"] == [0]


def test_apply_failure_runs_transaction_recovery_before_reporting_rolled_back(
    instruction_app, monkeypatch
):
    runner = instruction_app.state.instruction_apply
    client = client_for(instruction_app)
    head = client.get("/api/instructions/source").json()
    head = client.put(
        "/api/instructions/source/template",
        json={**versioned(head), "template": instruction_template()},
    ).json()
    head = client.put(
        "/api/instructions/source/episodes/0",
        json={
            **versioned(head),
            "selections": {
                "object": "蓝色圆柱",
                "target": "右侧盒子",
            },
        },
    ).json()
    queued = client.post(
        "/api/instructions/source/episodes/0/apply",
        json=versioned(head),
    )
    job = runner.store.get("instruction_apply", queued.json()["id"])
    marker = runner.settings.root / "partial-write"
    calls = []

    def fail_after_partial_write(action, *_args):
        calls.append(action)
        if action == "recover":
            marker.unlink(missing_ok=True)
            return {"recovered": []}
        marker.write_text("partial")
        raise RuntimeError("simulated worker timeout")

    monkeypatch.setattr(runner, "_worker", fail_after_partial_write)
    runner.run(job)

    failed = runner.store.get("instruction_apply", queued.json()["id"])
    assert failed["state"] == "FAILED", failed
    assert calls == ["recover", "apply", "recover"]
    assert not marker.exists()


def test_restart_reconciles_a_durable_committed_apply_receipt(instruction_app):
    runner = instruction_app.state.runner
    client = client_for(instruction_app)
    head = client.get("/api/instructions/source").json()
    head = client.put(
        "/api/instructions/source/template",
        json={**versioned(head), "template": instruction_template()},
    ).json()
    head = client.put(
        "/api/instructions/source/episodes/0",
        json={
            **versioned(head),
            "selections": {
                "object": "蓝色圆柱",
                "target": "右侧盒子",
            },
        },
    ).json()
    queued = client.post(
        "/api/instructions/source/episodes/0/apply",
        json=versioned(head),
    ).json()
    job = runner.store.get("instruction_apply", queued["id"])
    job.update(state="RUNNING", stage="写入中")
    runner.store.put("instruction_apply", job["id"], job)

    (runner.settings.root / "source/applied.marker").write_text("committed")
    receipt = (
        runner.settings.root
        / f"source/meta/.bodypipeline-instruction-journal-{job['id']}.json"
    )
    receipt.write_text(
        json.dumps(
            {
                "operation_id": job["id"],
                "state": "committed",
                "files": [],
                "validation": {"valid": True},
                "report": {
                    "updated_episodes": [0],
                    "rewritten_shards": 1,
                    "total_tasks": 2,
                    "noop": False,
                },
            }
        )
    )

    restarted = create_app(instruction_app.state.runner.settings, background=False)
    with client_for(restarted) as restarted_client:
        completed = restarted.state.runner.store.get(
            "instruction_apply", job["id"]
        )
        assert completed["state"] == "CONVERTED", completed
        draft = restarted.state.runner.store.get(
            "instruction_draft", completed["dataset_key"]
        )
        snapshot = source_snapshot(Path(completed["dataset_path"]))
        assert draft["source_signature"] == snapshot["signature"], (
            draft["source_signature"],
            snapshot["signature"],
            draft,
        )
        response = restarted_client.get("/api/instructions/source")
        assert response.status_code == 200, response.text
    assert completed["report"]["updated_episodes"] == [0]
    assert not receipt.exists()


def test_repeated_finalize_for_the_same_apply_is_idempotent(
    instruction_app, monkeypatch
):
    runner = instruction_app.state.runner
    review = InstructionReview(runner.settings, runner.store)
    client = client_for(instruction_app)
    head = client.get("/api/instructions/source").json()
    saved = client.put(
        "/api/instructions/source/template",
        json={**versioned(head), "template": instruction_template()},
    )
    assert saved.status_code == 200, saved.text
    key, _, _ = review.resolve("source")
    operation = {"operation_id": "duplicate-finalize"}
    ticks = iter([101.0, 102.0, 201.0, 202.0])
    monkeypatch.setattr(
        "pipeline.server.instruction_review.now",
        lambda: next(ticks),
    )

    first_snapshot, first = review.finalize_apply(
        key, saved.json()["source_signature"], operation
    )
    first_revision = first["revision"]
    first_updated_at = first["updated_at"]
    first_applied_at = first["last_applied_at"]
    first_history_count = len(runner.store.all("instruction_revision"))

    second_snapshot, second = review.finalize_apply(
        key, saved.json()["source_signature"], operation
    )

    assert second_snapshot == first_snapshot
    assert second["revision"] == first_revision
    assert second["updated_at"] == first_updated_at
    assert second["last_applied_at"] == first_applied_at
    assert len(runner.store.all("instruction_revision")) == first_history_count


def test_stale_drafts_source_changes_and_locks_are_rejected(instruction_app):
    runner = instruction_app.state.runner
    client = client_for(instruction_app)
    head = client.get("/api/instructions/source").json()
    first = client.put(
        "/api/instructions/source/template",
        json={**versioned(head), "template": instruction_template()},
    )
    assert first.status_code == 200
    stale = client.put(
        "/api/instructions/source/template",
        json={**versioned(head), "template": instruction_template()},
    )
    assert stale.status_code == 409
    assert "其他窗口" in stale.json()["detail"]

    review = InstructionReview(runner.settings, runner.store)
    key, _, _ = review.resolve("source")
    with dataset_lock(runner.store, key):
        assert (
            client.put(
                "/api/instructions/source/template",
                json={**versioned(first.json()), "template": instruction_template()},
            ).status_code
            == 409
        )

    (runner.settings.root / "source/changed").write_text("new")
    assert client.get("/api/instructions/source").status_code == 409


def test_concurrent_instruction_reads_do_not_report_dataset_as_processing(
    instruction_app, monkeypatch
):
    first_read_entered = threading.Event()
    release_first_read = threading.Event()
    call_lock = threading.Lock()
    fixture_source = InstructionReview._source
    call_count = 0

    def slow_first_source(*args):
        nonlocal call_count
        with call_lock:
            call_count += 1
            current_call = call_count
        if current_call == 1:
            first_read_entered.set()
            assert release_first_read.wait(2)
        return fixture_source(*args)

    monkeypatch.setattr(InstructionReview, "_source", slow_first_source)
    first_client = client_for(instruction_app)
    second_client = client_for(instruction_app)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(first_client.get, "/api/instructions/source")
        assert first_read_entered.wait(2)
        second = executor.submit(
            second_client.get, "/api/instructions/source/episodes/0"
        )
        try:
            second_response = second.result(timeout=2)
        finally:
            release_first_read.set()
        first_response = first.result(timeout=2)

    assert first_response.status_code == 200, first_response.text
    assert second_response.status_code == 200, second_response.text


def test_api_validation_episode_bounds_and_dataset_identity(instruction_app, tmp_path):
    client = client_for(instruction_app)
    assert TestClient(instruction_app).get("/api/instructions/source").status_code == 401
    assert client.get("/api/instructions/missing").status_code == 404
    head = client.get("/api/instructions/source").json()
    assert client.get("/api/instructions/source/episodes/99").status_code == 404
    assert (
        client.put(
            "/api/instructions/source/episodes/0",
            json={
                **versioned(head),
                "selections": {"object": "not-an-option", "target": "左侧盒子"},
            },
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/instructions/source/template",
            json={**versioned(head), "template": {"parts": [], "variables": {}}},
        ).status_code
        == 422
    )

    store = instruction_app.state.runner.store
    outside = tmp_path / "outside"
    (outside / "meta").mkdir(parents=True)
    (outside / "meta/info.json").write_text("{}")
    PreviewRegistry(store, 60).register("source", outside)
    assert client.get("/api/instructions/source").status_code == 403


def test_active_dataset_job_blocks_apply_and_restart_requeues_running_apply(
    instruction_app,
):
    runner = instruction_app.state.runner
    client = client_for(instruction_app)
    head = client.get("/api/instructions/source").json()
    head = client.put(
        "/api/instructions/source/template",
        json={**versioned(head), "template": instruction_template()},
    ).json()
    head = client.put(
        "/api/instructions/source/episodes/0",
        json={
            **versioned(head),
            "selections": {
                "object": "红色方块",
                "target": "左侧盒子",
            },
        },
    ).json()
    request = versioned(head)

    runner.store.put(
        "job",
        "busy-work",
        {
            "id": "busy-work",
            "state": "RUNNING",
            "dataset_path": str(runner.settings.root / "source"),
            "created_at": 2,
        },
    )
    blocked = client.post("/api/instructions/source/apply", json=request)
    assert blocked.status_code == 409
    assert "运行中的工作台任务" in blocked.json()["detail"]
    runner.store.delete("job", "busy-work")

    queued = client.post("/api/instructions/source/apply", json=request).json()
    running = runner.store.get("instruction_apply", queued["id"])
    running.update(state="RUNNING", stage="写入中")
    runner.store.put("instruction_apply", running["id"], running)
    instruction_app.state.instruction_apply.recover()
    recovered = runner.store.get("instruction_apply", queued["id"])
    assert recovered["state"] == "QUEUED"
    assert "恢复中断任务" in recovered["stage"]
