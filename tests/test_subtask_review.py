"""Viewer Subtask review: persistence, conflicts, identities and real rewrites."""

import hashlib
import json
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from pipeline.server.app import create_app
from pipeline.server.merges import source_snapshot
from pipeline.server.recycle import dataset_lock
from pipeline.server.subtask_review import SubtaskReview
from pipeline.server.viewer import create_gateway
from pipeline.server.viewer_bootstrap import patch_subtask_review
from pipeline.server.viewer_data import PreviewRegistry

SCENARIO = Path(__file__).parent / "fixtures/merge_scenario.py"


def segment(start=0, end=2, text="grasp"):
    return {"start_frame": start, "end_frame": end, "text": text}


@pytest.fixture
def review_app(settings_factory, monkeypatch):
    settings = settings_factory("server")
    app = create_app(settings, background=False)
    store = app.state.runner.store
    path = settings.root / "source"
    (path / "meta").mkdir(parents=True)
    (path / "meta/info.json").write_text(json.dumps({"codebase_version": "v3.0", "fps": 10, "total_episodes": 2, "total_frames": 11, "robot_type": "test_arm"}))
    store.put("job", "source", {"id": "source", "state": "CONVERTED", "dataset_path": str(path), "dataset_name": "fixture", "created_at": 1, "robot_type": "test_arm"})
    PreviewRegistry(store, settings.viewer_ttl).register("source", path)
    monkeypatch.setattr(SubtaskReview, "_source", lambda *args: {"fps": 10, "episodes": [
        {"episode_index": 0, "frames": 6, "segments": [segment(), segment(4, 6)]},
        {"episode_index": 1, "frames": 5, "segments": [segment(1, 3, "place")]},
    ]})
    return app


def client_for(app):
    return TestClient(app, headers={"Authorization": "Bearer " + app.state.runner.settings.token})


def body(payload, segments):
    return {"revision": payload["revision"], "source_signature": payload["source_signature"], "segments": segments}


URL = "/api/subtask-review/source/episodes/0"


def test_source_loading_and_empty_draft_authority(review_app):
    client = client_for(review_app)
    initial = client.get(URL).json()
    assert initial["frames"] == 6 and initial["revision"] == 0
    assert initial["segments"] == [segment(), segment(4, 6)]
    assert initial["has_draft"] is False
    assert client.post(URL, json=body(initial, [])).json()["revision"] == 1
    # A new service instance sees authoritative [], not the original labels.
    settings = review_app.state.runner.settings
    restarted = create_app(settings, background=False)
    result = client_for(restarted).get(URL).json()
    assert result["segments"] == [] and result["has_draft"] is True
    store = restarted.state.runner.store
    assert len(store.all("subtask_draft_history")) == 1


def test_two_windows_cannot_overwrite_a_draft(review_app):
    client = client_for(review_app)
    a = client.get(URL).json()
    assert client.post(URL, json=body(a, [segment(0, 1, "new")])).status_code == 200
    stale = client.post(URL, json=body(a, []))
    assert stale.status_code == 409 and "其他窗口" in stale.json()["detail"]
    assert client.get(URL).json()["segments"] == [segment(0, 1, "new")]


def test_repeated_text_edit_only_changes_selected_segment(review_app):
    client = client_for(review_app)
    initial = client.get(URL).json()
    edited = [segment(0, 2, "changed"), segment(4, 6)]
    assert client.post(URL, json=body(initial, edited)).status_code == 200
    assert client.get(URL).json()["segments"] == edited
    assert client.get("/api/subtask-review/source/episodes/1").json()["segments"] == [segment(1, 3, "place")]


@pytest.mark.parametrize("segments", [
    [segment(-1, 2)], [segment(2, 2)], [segment(2, 8)],
    [segment(0, 3), segment(2, 4)], [segment(text="  ")],
    [segment(text="x" * 401)], [{**segment(), "end_frame": 1.5}],
    [{**segment(), "start_frame": True}], [{**segment(), "extra": "ignored?"}],
])
def test_invalid_segments_rejected(review_app, segments):
    client = client_for(review_app)
    initial = client.get(URL).json()
    assert client.post(URL, json=body(initial, segments)).status_code == 422
    assert client.get(URL).json()["revision"] == 0


def test_dataset_and_episode_constraints(review_app, tmp_path):
    client = client_for(review_app)
    assert TestClient(review_app).get(URL).status_code == 401
    assert client.get("/api/subtask-review/unknown/episodes/0").status_code == 404
    assert client.get("/api/subtask-review/source/episodes/99").status_code == 404
    assert client.get(URL + "?local_path=/etc").json()["frames"] == 6
    store = review_app.state.runner.store
    path = tmp_path / "outside"
    (path / "meta").mkdir(parents=True)
    (path / "meta/info.json").write_text("{}")
    PreviewRegistry(store, 60).register("source", path)
    assert client.get(URL).status_code == 403


def test_dataset_lock_source_change_and_identity_change(review_app):
    runner = review_app.state.runner
    client = client_for(review_app)
    review = SubtaskReview(runner.settings, runner.store)
    key, path, _ = review.resolve("source")
    initial = client.get(URL).json()
    with dataset_lock(runner.store, key):
        assert client.post(URL, json=body(initial, [])).status_code == 409
    assert client.post(URL, json=body(initial, [])).status_code == 200
    (path / "changed").write_text("new source content")
    assert client.get(URL).status_code == 409
    assert client.post(URL, json=body(initial, [])).status_code == 409
    job = runner.store.get("job", "source")
    job["state"] = "DELETED"
    runner.store.put("job", "source", job)
    assert client.get(URL).status_code == 409


def test_gateway_forwards_only_authenticated_registered_id(review_app, monkeypatch):
    runner = review_app.state.runner
    gateway = create_gateway(runner.settings)
    real_client = httpx.AsyncClient
    calls = []

    def make_client(**kwargs):
        async def response(request):
            calls.append(request)
            assert request.headers["authorization"] == "Bearer " + runner.settings.token
            return httpx.Response(200, json={"revision": 3})
        return real_client(transport=httpx.MockTransport(response))

    monkeypatch.setattr("pipeline.server.viewer.httpx.AsyncClient", make_client)
    with TestClient(gateway) as client:
        assert client.get(URL).status_code == 401
        headers = {"Authorization": "Bearer " + runner.settings.token}
        assert client.get(URL, headers=headers).json() == {"revision": 3}
        payload = {"revision": 0, "source_signature": "f" * 64, "segments": []}
        assert client.post(URL, headers=headers, json=payload).status_code == 200
        assert len(calls) == 2
        assert json.loads(calls[1].content)["segments"] == []
        assert client.get("/api/subtask-review/missing/episodes/0", headers=headers).status_code == 404
        assert len(calls) == 2
        assert client.post("/api/login", json={"token": runner.settings.token}).status_code == 200
        assert client.post(URL, json=payload).status_code == 403  # Cookie needs Origin.


def test_patch_is_repeatable_on_pinned_viewer(tmp_path):
    path = tmp_path / "source/src/app/[org]/[dataset]/[episode]/episode-viewer.tsx"
    path.parent.mkdir(parents=True)
    path.write_text('''import { AnnotationsPanel } from "@/components/annotations-panel";
              <div className="grounding-intro">legacy</div>
              <PlaybackBar />
              <AnnotationsTimeline duration={data.duration} />
              <AnnotationsPanel
                cameraKeys={videosInfo.map((v) => v.filename)}
              />''')
    patch_subtask_review(tmp_path)
    first = path.read_text()
    patch_subtask_review(tmp_path)
    assert path.read_text() == first
    assert first.count("<SubtaskReview ") == 1
    assert "<PlaybackBar />" in first


def test_export_revision_conflict_and_recycle_guard(review_app, monkeypatch, tmp_path):
    runner = review_app.state.runner
    tool = tmp_path / "letools"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    tool.with_name("python").write_text("placeholder")
    monkeypatch.setattr(runner.settings, "letools_bin", str(tool))
    client = client_for(review_app)
    initial = client.get(URL).json()
    request = {"revision": initial["revision"], "source_signature": initial["source_signature"]}
    assert client.post(URL, json=body(initial, [])).status_code == 200
    assert client.post("/api/subtask-review/source/exports", json=request).status_code == 409
    head = client.get(URL).json()
    request["revision"] = head["revision"]
    review = SubtaskReview(runner.settings, runner.store)
    key, path, _ = review.resolve("source")
    runner.store.put("recycle", key, {"entries": {"0": {}}, "revision": 1})
    assert client.post("/api/subtask-review/source/exports", json=request).status_code == 409
    runner.store.delete("recycle", key)
    queued = client.post("/api/subtask-review/source/exports", json=request)
    assert queued.status_code == 202, queued.text
    jid = queued.json()["id"]
    status = client.get(f"/api/subtask-review/source/exports/{jid}")
    assert status.json()["state"] == "QUEUED"
    assert "revision_plan" not in status.json()
    # A changed source fails before any output work or compute allocation.
    (path / "modified").write_text("changed after enqueue")
    runner.tick()
    result = runner.store.get("job", jid)
    assert result["state"] == "FAILED" and "提交后发生变化" in result["error"]
    assert not result.get("dataset_path")


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
def test_real_review_roundtrip(settings_factory, tmp_path, letools_binary, version):
    python = str(Path(letools_binary).with_name("python"))
    fixtures = tmp_path / "fixtures"
    subprocess.run([python, str(SCENARIO), "create", str(fixtures), version], check=True)
    settings = settings_factory("server", letools_bin=letools_binary, import_roots=[fixtures])
    app = create_app(settings, background=False)
    runner = app.state.runner
    source = fixtures / "input-0"
    runner.store.put("job", "source", {"id": "source", "state": "CONVERTED", "dataset_path": str(source), "dataset_name": "fixture", "created_at": 1, "robot_type": "test_arm"})
    PreviewRegistry(runner.store, settings.viewer_ttl).register("source", source)
    before = hashes(source)
    client = client_for(app)
    initial = client.get(URL)
    assert initial.status_code == 200, initial.text
    initial = initial.json()
    assert initial["segments"] == []  # No labels in the source fixture.
    edited = [segment(0, 2), segment(4, 6)]
    assert client.post(URL, json=body(initial, edited)).status_code == 200
    saved = client.get(URL).json()
    export_body = {"revision": saved["revision"], "source_signature": saved["source_signature"]}
    queued = client.post("/api/subtask-review/source/exports", json=export_body)
    assert queued.status_code == 202, queued.text
    export = queued.json()
    assert client.post("/api/subtask-review/source/exports", json=export_body).json()["id"] == export["id"]
    # Freeze the queued plan: a later saved empty draft does not affect it.
    assert client.post(URL, json=body(saved, [])).status_code == 200
    runner.tick()
    result = runner.store.get("job", export["id"])
    assert result["state"] == "CONVERTED", result.get("error")
    assert hashes(source) == before
    revised = Path(result["dataset_path"])
    PreviewRegistry(runner.store, settings.viewer_ttl).register(result["id"], revised)
    url = f"/api/subtask-review/{result['id']}/episodes/0"
    loaded = client.get(url).json()
    assert loaded["segments"] == edited  # Includes the deliberate two-frame gap.
    assert client.get(f"/api/subtask-review/{result['id']}/episodes/1").json()["segments"] == []
    # Rewrite an ALREADY labelled source: edit only one repeated-text interval.
    changed = [segment(0, 1, "human corrected"), segment(4, 6)]
    assert client.post(url, json=body(loaded, changed)).status_code == 200
    head = client.get(url).json()
    queued2 = client.post(f"/api/subtask-review/{result['id']}/exports", json={"revision": head["revision"], "source_signature": head["source_signature"]}).json()
    runner.tick()
    result2 = runner.store.get("job", queued2["id"])
    assert result2["state"] == "CONVERTED", result2.get("error")
    assert result2["subtask_report"]["unannotated_frames"] == 8
    target = Path(result2["dataset_path"])
    PreviewRegistry(runner.store, settings.viewer_ttl).register(result2["id"], target)
    assert client.get(f"/api/subtask-review/{result2['id']}/episodes/0").json()["segments"] == changed
    assert client.get(url).json()["segments"] == changed  # Saved draft still exists.
    # Deleting every segment must export -1 for ALL frames, even on labelled input.
    assert client.post(url, json=body(head, [])).status_code == 200
    empty = client.get(url).json()
    queued3 = client.post(f"/api/subtask-review/{result['id']}/exports", json={"revision": empty["revision"], "source_signature": empty["source_signature"]}).json()
    runner.tick()
    result3 = runner.store.get("job", queued3["id"])
    assert result3["state"] == "CONVERTED", result3.get("error")
    assert result3["subtask_report"]["unannotated_frames"] == 11
    assert result3["subtask_report"]["vocabulary"] == 0
    assert hashes(source) == before
    assert source_snapshot(revised) == result2["source_snapshot"]
