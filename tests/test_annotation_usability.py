"""Usability contracts: AI starts a draft; review remains an explicit choice."""

import json
from copy import deepcopy
from pathlib import Path

from test_subtask_review import client_for
from test_subtask_review import review_app as review_app

BASE = "/api/annotation-workbench/source"


def test_template_import_preview_accepts_downloads_and_never_writes(review_app):
    client = client_for(review_app)
    before = client.get("/api/task-templates").json()
    for name, count in [
        ("template-example.json", 1),
        ("screw-alignment-en.json", 4),
        ("screw-workflow-en.json", 8),
    ]:
        response = client.get("/workbench-assets/" + name)
        assert response.status_code == 200
        template = response.json()
        for payload in (template, [template], {"templates": [template]}):
            result = client.post(
                "/api/annotation-workbench/templates/validate", json=payload
            )
            assert result.status_code == 200, result.text
            assert len(result.json()["templates"][0]["subtasks"]) == count
            assert result.json()["templates"][0] == template
    assert client.get("/api/task-templates").json() == before
    assert client.get("/workbench-assets/unknown.json").status_code == 404


def test_template_import_reports_position_and_rejects_duplicate_ids(review_app):
    client = client_for(review_app)
    template = json.loads(
        (Path(__file__).parents[1] / "pipeline/web/template-example.json").read_text()
    )
    bad = deepcopy(template)
    del bad["subtasks"][0]["name"]
    response = client.post("/api/annotation-workbench/templates/validate", json=bad)
    assert response.status_code == 422
    assert "第 1 套模板 → subtasks → 1 → name" in response.json()["detail"]
    assert (
        client.post(
            "/api/annotation-workbench/templates/validate", json=[template, template]
        ).status_code
        == 422
    )
    for data in (
        {"templates": []},
        {"templates": "invalid"},
        {"id": "bad", "name": "bad"},
    ):
        assert (
            client.post(
                "/api/annotation-workbench/templates/validate", json=data
            ).status_code
            == 422
        )


def test_ai_initial_draft_is_pinned_and_never_overwrites_human_head(
    review_app, monkeypatch
):
    client = client_for(review_app)
    service = review_app.state.annotation_workbench
    original = client.get(BASE + "/episodes/0").json()
    incoming = deepcopy(original["document"])
    incoming["values"]["episode-comment"] = "AI result"
    # A pristine source has no manual/semantic contents. Preserve its template.
    empty = deepcopy(original["document"])
    for key in ("segments", "attempts", "frames", "manualAtoms"):
        empty[key] = []
    empty["values"] = {}
    monkeypatch.setattr(service.legacy, "_document", lambda *args: deepcopy(empty))
    candidate = service.add_candidate(
        "source",
        0,
        incoming,
        run="ai-1",
        source_signature=original["source_signature"],
        base_revision=0,
        provenance={"annotation": "ai-1"},
    )
    head = client.get(BASE + "/episodes/0").json()
    assert head["revision"] == 1
    assert head["document"]["values"]["episode-comment"] == "AI result"
    assert head["document"]["provenance"]["initialAI"] == candidate["id"]
    assert head["complete"] is False
    assert all(not s.get("review") for s in head["document"]["segments"])
    # Direct export needs no review. The model's provenance remains truthful.
    payload = {
        "source_signature": head["source_signature"],
        "episodes": [0],
        "revisions": {"0": 1},
    }
    bundle = client.post(BASE + "/bundle", json=payload).json()
    assert bundle["episodes"][0]["complete"] is False
    assert bundle["episodes"][0]["document"] == head["document"]
    # An intentionally emptied human draft must not be repopulated on refresh.
    head["document"]["segments"] = []
    head["document"]["attempts"] = []
    head["document"]["values"]["episode-comment"] = "my edit"
    response = client.post(
        BASE + "/episodes/0",
        json={
            **{k: head[k] for k in ("revision", "source_signature", "document")},
            "operation": "edit",
        },
    )
    assert response.status_code == 200, response.text
    incoming["values"]["episode-comment"] = "later AI"
    service.add_candidate(
        "source",
        0,
        incoming,
        run="ai-2",
        source_signature=head["source_signature"],
        base_revision=1,
        provenance={"annotation": "ai-2"},
    )
    after = client.get(BASE + "/episodes/0").json()
    assert after["document"]["values"]["episode-comment"] == "my edit"
    assert after["document"]["segments"] == []
    assert after["revision"] == 2
    # No AI task was enqueued by reads, edits, or export.
    assert review_app.state.runner.store.all("annotation") == []


def test_existing_source_annotations_remain_authoritative(review_app):
    client = client_for(review_app)
    head = client.get(BASE + "/episodes/0").json()
    doc = deepcopy(head["document"])
    doc["values"]["episode-comment"] = "new AI"
    review_app.state.annotation_workbench.add_candidate(
        "source",
        0,
        doc,
        run="ai",
        source_signature=head["source_signature"],
        base_revision=0,
        provenance={"annotation": "ai"},
    )
    assert client.get(BASE + "/episodes/0").json()["document"] == head["document"]


def test_ready_export_registers_its_viewer_link(review_app):
    client = client_for(review_app)
    store = review_app.state.runner.store
    source = store.get("job", "source")
    store.put(
        "job",
        "preview-export",
        {
            **source,
            "id": "preview-export",
            "state": "CONVERTED",
            "source_kind": "lerobot_annotation_subset",
            "source_job_id": "source",
        },
    )
    response = client.get(BASE + "/exports/preview-export")
    assert response.status_code == 200, response.text
    settings = review_app.state.runner.settings
    assert (
        response.json()["viewer_url"]
        == settings.viewer_origin + "/bp/preview-export/episode_0?tab=annotations"
    )
    registered = review_app.state.annotation_workbench.legacy.registry.lookup(
        "bp/preview-export"
    )
    assert registered["dataset_path"] == source["dataset_path"]


def test_whole_episode_check_preserves_labels_and_edits_clear_completion(review_app):
    client = client_for(review_app)
    url = BASE + "/episodes/0"
    head = client.get(url).json()
    doc = head["document"]
    doc["segments"].append(
        {"id": "gap", "label": "NULL", "start": 2, "end": 4, "values": {}}
    )
    doc["segments"].sort(key=lambda s: s["start"])
    for attempt in doc["attempts"]:
        attempt["outcome"] = "unknown"
        attempt["values"]["attempt-outcome"] = "无法判断"
    geometry = [
        (s["id"], s["start"], s["end"], s.get("attemptId")) for s in doc["segments"]
    ]
    result = client.post(
        url,
        json={
            "revision": head["revision"],
            "source_signature": head["source_signature"],
            "document": doc,
            "confirm": [[0, head["frames"]]],
            "complete": True,
            "operation": "human-review",
        },
    )
    assert result.status_code == 200, result.text
    checked = result.json()
    assert checked["complete"] is True
    assert checked["status"]["pending"] == []
    assert [
        (s["id"], s["start"], s["end"], s.get("attemptId"))
        for s in checked["document"]["segments"]
    ] == geometry
    assert client.get(BASE + "/episodes").json()["episodes"][0]["complete"] is True
    # A later annotation edit invalidates the Episode check without any AI run.
    checked["document"]["segments"][0]["note"] = "new edit after checking"
    edited = client.post(
        url, json={k: checked[k] for k in ("revision", "source_signature", "document")}
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["complete"] is False
    assert client.get(BASE + "/episodes").json()["episodes"][0]["complete"] is False
    assert review_app.state.runner.store.all("annotation") == []
