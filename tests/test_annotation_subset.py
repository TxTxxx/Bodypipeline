"""R31 exercises actual Arrow/video extraction, not a mocked export worker."""

import json
import subprocess
from pathlib import Path

import pytest
from test_subtask_review import SCENARIO, client_for, hashes

from pipeline.server.app import create_app
from pipeline.server.recycle import RecycleBin
from pipeline.server.viewer_data import PreviewRegistry


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
def test_frozen_whole_episode_subset_is_independent_and_source_unchanged(
    settings_factory, tmp_path, letools_binary, version
):
    python = str(Path(letools_binary).with_name("python"))
    fixtures = tmp_path / "fixtures"
    subprocess.run(
        [python, str(SCENARIO), "create", str(fixtures), version],
        check=True,
        capture_output=True,
    )
    settings = settings_factory(
        "server", letools_bin=letools_binary, import_roots=[fixtures]
    )
    app = create_app(settings, background=False)
    runner = app.state.runner
    source = fixtures / "input-0"
    runner.store.put(
        "job",
        "source",
        {
            "id": "source",
            "state": "CONVERTED",
            "dataset_path": str(source),
            "dataset_name": "subset-fixture",
            "created_at": 1,
            "robot_type": "test_arm",
        },
    )
    PreviewRegistry(runner.store, settings.viewer_ttl).register("source", source)
    before = hashes(source)
    client = client_for(app)
    base = "/api/annotation-workbench/source"
    head = client.get(base + "/episodes/1").json()
    doc = head["document"]
    doc["templateSnapshot"]["subtasks"] = [
        {"id": "grasp", "name": "抓取", "definition": "含接近、调整与提起"}
    ]
    doc["attempts"] = [
        {
            "id": "failure",
            "subtaskId": "grasp",
            "subtaskName": "抓取",
            "outcome": "failure",
            "values": {},
        },
        {
            "id": "retry",
            "subtaskId": "grasp",
            "subtaskName": "抓取",
            "outcome": "success",
            "values": {},
        },
    ]
    doc["segments"] = [
        {
            "id": "a",
            "start": 0,
            "end": 1,
            "label": "SUBTASK",
            "subtaskId": "grasp",
            "attemptId": "failure",
            "note": "局部滑动",
        },
        {
            "id": "blind",
            "start": 1,
            "end": 2,
            "label": "UNKNOWN",
            "attemptId": "failure",
        },
        {
            "id": "b",
            "start": 2,
            "end": 3,
            "label": "SUBTASK",
            "subtaskId": "grasp",
            "attemptId": "retry",
        },
        {
            "id": "outside",
            "start": 3,
            "end": 4,
            "label": "OUT_OF_TEMPLATE",
            "description": "清理桌面",
            "attemptId": "failure",
        },
    ]
    saved = client.post(
        base + "/episodes/1",
        json={
            "revision": head["revision"],
            "source_signature": head["source_signature"],
            "document": doc,
            "template_mapping": {},
            "confirm": [[0, 4]],
        },
    )
    assert saved.status_code == 200, saved.text
    head = saved.json()
    selection = {
        "source_signature": head["source_signature"],
        "episodes": [1, 1],
        "revisions": {"1": head["revision"]},
        "filter": {"subtask": "grasp", "count": 2, "basis": "recorded"},
        "origins": {"1": "manual"},
        "name": "  螺丝对准 · 已调整副本  ",
    }
    queued = client.post(base + "/exports", json=selection)
    assert queued.status_code == 202, queued.text
    jid = queued.json()["id"]
    assert queued.json()["dataset_name"] == "螺丝对准 · 已调整副本"
    listed = next(row for row in client.get("/api/jobs").json() if row["id"] == jid)
    assert listed["dataset_name"] == "螺丝对准 · 已调整副本"
    assert listed["state"] == "QUEUED"
    frozen = runner.store.get("job", jid)["revision_plan"]
    # Later annotation edits do not alter the submitted plan.
    head["document"]["segments"][0]["note"] = "later local change"
    changed = client.post(
        base + "/episodes/1",
        json={
            "revision": head["revision"],
            "source_signature": head["source_signature"],
            "document": head["document"],
        },
    )
    assert changed.status_code == 200, changed.text
    runner.tick()
    result = runner.store.get("job", jid)
    assert result["state"] == "CONVERTED", result.get("error")
    listed = next(
        row for row in client.get("/api/jobs?summary=true").json() if row["id"] == jid
    )
    assert listed["dataset_name"] == "螺丝对准 · 已调整副本"
    assert listed["dataset_available"] is True
    status = client.get(base + f"/exports/{jid}").json()
    assert status["viewer_url"] == (
        settings.viewer_origin + f"/bp/{jid}/episode_0?tab=annotations"
    )
    assert status["data_url"] == settings.server_url + f"/?dataset={jid}#results"
    assert status["dataset_name"] == listed["dataset_name"]
    output = Path(result["dataset_path"])
    assert hashes(source) == before
    mapping = json.loads(
        (output / "meta/bodypipeline-episode-mapping.json").read_text()
    )
    assert mapping["source_to_output"] == {"1": 0}
    bundle = json.loads(
        (output / "meta/bodypipeline-annotation-bundle.json").read_text()
    )
    assert len(bundle["episodes"]) == 1
    assert bundle["episodes"][0]["source_episode"] == 1
    assert bundle["episodes"][0]["document"] == frozen["episodes"][0]["task_document"]
    # Remove the source directory from its old location; output remains readable.
    source.rename(fixtures / "source-hidden")
    inspected = subprocess.run(
        [python, str(SCENARIO), "inspect", str(output)],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(inspected.stdout)
    assert data["episodes"] == 1 and data["frames"] == 5
    assert data["indices"] == [0]
    assert all(r["episode_index"] == 0 for r in data["rows"][0])
    assert [r["index"] for r in data["rows"][0]] == list(range(5))
    assert data["rows"][0][0]["observation.state"] == [0.0, pytest.approx(0.01)]
    assert [r["subtask_index"] for r in data["rows"][0]] == [0, -1, 0, -1, -1]
    PreviewRegistry(runner.store, settings.viewer_ttl).register(jid, output)
    reopened = client.get(f"/api/annotation-workbench/{jid}/episodes/0")
    assert reopened.status_code == 200, reopened.text
    final = reopened.json()
    assert final["document"] == frozen["episodes"][0]["task_document"]
    assert final["status"]["gaps"] == [[4, 5]]
    media = client.get(f"/api/annotation-workbench/{jid}/episodes/0/media")
    assert media.status_code == 200, media.text
    camera = media.json()["cameras"][0]["key"]
    image = client.get(
        f"/api/annotation-workbench/{jid}/episodes/0/frame/4",
        params={"camera": camera, "signature": final["source_signature"]},
    )
    assert image.status_code == 200, image.text
    assert image.headers["x-data-frame"] == "4"


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
def test_recycle_renumbers_annotations_preserves_manual_edits_and_can_export_again(
    settings_factory, tmp_path, letools_binary, version
):
    python = str(Path(letools_binary).with_name("python"))
    fixtures = tmp_path / "fixtures"
    subprocess.run(
        [python, str(SCENARIO), "create", str(fixtures), version],
        check=True,
        capture_output=True,
    )
    settings = settings_factory(
        "server", letools_bin=letools_binary, import_roots=[fixtures]
    )
    app = create_app(settings, background=False)
    runner = app.state.runner
    source = fixtures / "input-0"
    runner.store.put(
        "job",
        "source",
        {
            "id": "source",
            "state": "CONVERTED",
            "dataset_path": str(source),
            "dataset_name": "cleanup-fixture",
            "created_at": 1,
            "robot_type": "test_arm",
        },
    )
    registry = PreviewRegistry(runner.store, settings.viewer_ttl)
    registry.register("source", source)
    client = client_for(app)
    base = "/api/annotation-workbench/source"
    heads = []
    for ep in range(2):
        head = client.get(base + f"/episodes/{ep}").json()
        doc = {
            "version": 2,
            "templateId": "fixture",
            "templateSnapshot": {
                "id": "fixture",
                "name": "Fixture",
                "subtasks": [
                    {"id": "a", "name": "First"},
                    {"id": "b", "name": "Second"},
                ],
                "fields": [],
            },
            "values": {},
            "frames": [],
            "manualAtoms": [],
            "history": [],
            "countEvidence": {},
            "provenance": {},
            "attempts": [
                {
                    "id": "attempt",
                    "subtaskId": "a" if ep == 0 else "b",
                    "subtaskName": "First" if ep == 0 else "Second",
                    "outcome": "success",
                    "values": {},
                }
            ],
            "segments": [
                {
                    "id": "segment",
                    "label": "SUBTASK",
                    "start": 0,
                    "end": head["frames"],
                    "subtaskId": "a" if ep == 0 else "b",
                    "attemptId": "attempt",
                }
            ],
        }
        saved = client.post(
            base + f"/episodes/{ep}",
            json={
                "source_signature": head["source_signature"],
                "revision": head["revision"],
                "document": doc,
                "template_mapping": {},
            },
        )
        assert saved.status_code == 200, saved.text
        heads.append(saved.json())
    queued = client.post(
        base + "/exports",
        json={
            "source_signature": heads[0]["source_signature"],
            "episodes": [0, 1],
            "revisions": {str(i): h["revision"] for i, h in enumerate(heads)},
            "name": "before-cleanup",
        },
    )
    assert queued.status_code == 202, queued.text
    jid = queued.json()["id"]
    runner.tick()
    result = runner.store.get("job", jid)
    assert result["state"] == "CONVERTED", result.get("error")
    assert client.get(base + f"/exports/{jid}").status_code == 200
    copied = f"/api/annotation-workbench/{jid}"
    old = client.get(copied + "/episodes/1").json()
    old["document"]["segments"][0]["note"] = "Manual edit on the retained Episode"
    saved = client.post(
        copied + "/episodes/1",
        json={
            "source_signature": old["source_signature"],
            "revision": old["revision"],
            "document": old["document"],
            "confirm": [[0, old["frames"]]],
            "complete": True,
        },
    )
    assert saved.status_code == 200, saved.text
    old = saved.json()
    recycle = RecycleBin(settings, runner.store, registry)
    key, path = recycle.resolve(f"bp/{jid}")
    row = recycle._load(key, path)
    row["entries"] = {"0": {"episode": 0, "deleted_at": 1}}
    row["operation"] = {
        "id": "annotation-cleanup",
        "state": "pending",
        "episodes": [0],
        "entries_before": row["entries"],
    }
    runner.store.put("recycle", key, row)
    cleanup = subprocess.run(
        [
            python,
            "-m",
            "pipeline.server.recycle_worker",
            "--store",
            str(runner.store.path),
            "--key",
            key,
            "--operation",
            "annotation-cleanup",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=settings.tool_environment(),
    )
    assert cleanup.returncode == 0, cleanup.stderr
    catalog = client.get(copied + "/episodes")
    assert catalog.status_code == 200, catalog.text
    assert len(catalog.json()["episodes"]) == 1
    rebased = client.get(copied + "/episodes/0")
    assert rebased.status_code == 200, rebased.text
    rebased = rebased.json()
    assert rebased["document"] == old["document"]
    assert rebased["complete"] is True
    assert rebased["revision"] == old["revision"] + 1
    assert rebased["source_signature"] != old["source_signature"]
    # Old clients cannot overwrite either retained data or its new neighbor.
    stale = client.post(
        copied + "/episodes/0",
        json={
            "source_signature": old["source_signature"],
            "revision": old["revision"],
            "document": old["document"],
        },
    )
    assert stale.status_code == 409
    next_copy = client.post(
        copied + "/exports",
        json={
            "source_signature": rebased["source_signature"],
            "episodes": [0],
            "revisions": {"0": rebased["revision"]},
            "name": "after-cleanup",
        },
    )
    assert next_copy.status_code == 202, next_copy.text
    runner.tick()
    final = runner.store.get("job", next_copy.json()["id"])
    assert final["state"] == "CONVERTED", final.get("error")
    bundle = json.loads(
        (
            Path(final["dataset_path"]) / "meta/bodypipeline-annotation-bundle.json"
        ).read_text()
    )
    assert bundle["episodes"][0]["document"] == old["document"]
    # A real inconsistency still fails closed, with a useful error rather than
    # exposing a worker traceback in the Annotation page.
    assert client.get(copied + f"/exports/{final['id']}").status_code == 200
    sidecar = Path(final["dataset_path"]) / "meta/bodypipeline-task-annotations.json"
    broken = json.loads(sidecar.read_text())
    broken["episodes"]["0"]["segments"][0]["start"] = 1
    sidecar.write_text(json.dumps(broken))
    rejected = client.get(f"/api/annotation-workbench/{final['id']}/episodes/0")
    assert rejected.status_code == 409, rejected.text
    assert "Episode 0" in rejected.json()["detail"]
    assert "Traceback" not in rejected.text
