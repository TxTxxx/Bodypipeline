"""R31 exercises actual Arrow/video extraction, not a mocked export worker."""

import json
import subprocess
from pathlib import Path

import pytest
from test_subtask_review import SCENARIO, client_for, hashes

from pipeline.server.app import create_app
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
    }
    queued = client.post(base + "/exports", json=selection)
    assert queued.status_code == 202, queued.text
    jid = queued.json()["id"]
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
