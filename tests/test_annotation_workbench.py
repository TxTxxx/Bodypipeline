"""Real HTTP flow over a registered synthetic source (no model requests)."""

from copy import deepcopy

from test_subtask_review import client_for
from test_subtask_review import review_app as review_app

from pipeline.server.annotation_contract import content_key

BASE = "/api/annotation-workbench/source"


def save_body(head, **kw):
    return {
        "revision": head["revision"],
        "source_signature": head["source_signature"],
        "document": head["document"],
        **kw,
    }


def test_episode_isolated_revision_history_and_conflict_preserves_both(review_app):
    client = client_for(review_app)
    zero = client.get(BASE + "/episodes/0").json()
    one = client.get(BASE + "/episodes/1").json()
    zero["document"]["values"]["episode-comment"] = "window A"
    first = client.post(BASE + "/episodes/0", json=save_body(zero))
    assert first.status_code == 200, first.text
    one["document"]["values"]["episode-comment"] = "different episode"
    assert client.post(BASE + "/episodes/1", json=save_body(one)).status_code == 200
    zero["document"]["values"]["episode-comment"] = "window B unsaved"
    conflict = client.post(BASE + "/episodes/0", json=save_body(zero))
    assert conflict.status_code == 409
    current = client.get(BASE + "/episodes/0").json()
    assert current["document"]["values"]["episode-comment"] == "window A"
    assert zero["document"]["values"]["episode-comment"] == "window B unsaved"
    assert len(client.get(BASE + "/episodes/0/history").json()["history"]) == 2


def test_confirm_does_not_fill_gaps_and_completion_checks_requirements(review_app):
    client = client_for(review_app)
    head = client.get(BASE + "/episodes/0").json()
    response = client.post(
        BASE + "/episodes/0", json=save_body(head, confirm=[[0, 6]], complete=True)
    )
    assert response.status_code == 422
    confirmed = client.post(
        BASE + "/episodes/0", json=save_body(head, confirm=[[0, 6]])
    ).json()
    assert confirmed["status"]["gaps"] == [[2, 4]]
    assert confirmed["complete"] is False
    assert confirmed["status"]["pending"] == []
    assert len(confirmed["document"]["segments"]) == 2


def test_target_count_evidence_and_manual_draft_bundle_roundtrip(review_app):
    client = client_for(review_app)
    head = client.get(BASE + "/episodes/0").json()
    sid = head["document"]["attempts"][0]["subtaskId"]
    assert (
        client.get(BASE + f"/episodes?subtask={sid}&count=2").json()["episodes"] == []
    )
    found = client.get(BASE + f"/episodes?subtask={sid}&count=2&recorded=true").json()[
        "episodes"
    ]
    assert [e["episode"] for e in found] == [0]
    head = client.post(
        BASE + "/episodes/0",
        json=save_body(
            head, count_evidence={sid: "人工逐帧核查抓取次数，空白范围没有抓取"}
        ),
    ).json()
    assert client.get(BASE + f"/episodes?subtask={sid}&count=2").json()["episodes"][0][
        "counts"
    ]["complete"]
    one = client.get(BASE + "/episodes/1").json()
    selection = {
        "source_signature": head["source_signature"],
        "episodes": [0, 1, 0],
        "revisions": {"0": head["revision"], "1": one["revision"]},
        "filter": {"subtask": sid, "count": 2},
        "origins": {"0": "filter", "1": "manual"},
    }
    bundle = client.post(BASE + "/bundle", json=selection).json()
    assert bundle["selection"]["episodes"] == [0, 1]
    assert bundle["episodes"][0]["document"] == head["document"]
    assert bundle["episodes"][0]["complete"] is False
    assert bundle["episodes"][0]["status"]["gaps"] == [[2, 4]]
    assert (
        client.post(BASE + "/bundle", json={**selection, "episodes": []}).status_code
        == 422
    )
    assert (
        client.post(BASE + "/bundle", json={**selection, "episodes": [99]}).status_code
        == 404
    )


def test_candidate_identity_template_scope_and_undo_by_saving_prior_head(review_app):
    client = client_for(review_app)
    url = BASE + "/episodes/0"
    head = client.get(url).json()
    service = review_app.state.annotation_workbench
    doc = deepcopy(head["document"])
    doc["values"]["episode-quality"] = 5
    doc["attempts"][0]["outcome"] = "failure"
    candidate = service.add_candidate(
        "source",
        0,
        doc,
        run="mock-1",
        source_signature=head["source_signature"],
        base_revision=0,
    )
    before = deepcopy(head["document"])
    head["document"]["attempts"][0]["outcome"] = "success"
    current = client.post(url, json=save_body(head)).json()
    payload = {
        "revision": current["revision"],
        "source_signature": current["source_signature"],
        "candidate": candidate["id"],
        "start": 0,
        "end": 1,
    }
    assert client.post(url + "/apply", json=payload).status_code == 409
    applied = client.post(
        url + "/apply", json={**payload, "acknowledge_current_revision": True}
    )
    assert applied.status_code == 200, applied.text
    new = applied.json()
    assert new["document"]["values"] == before["values"]
    assert (
        next(
            a
            for a in new["document"]["attempts"]
            if a["id"] == before["attempts"][0]["id"]
        )["outcome"]
        == "success"
    )
    assert new["document"]["segments"][1]["attemptId"] == before["attempts"][0]["id"]
    assert (
        new["document"]["segments"][0]["attemptId"]
        != new["document"]["segments"][1]["attemptId"]
    )
    undone = client.post(
        url, json=save_body({**new, "document": current["document"]}, operation="undo")
    )
    assert undone.status_code == 200
    assert content_key(undone.json()["document"]) == content_key(current["document"])
    different = deepcopy(doc)
    different["templateSnapshot"]["subtasks"][0]["definition"] = (
        "changed semantics, same ID"
    )
    bad = service.add_candidate(
        "source",
        0,
        different,
        run="mock-2",
        source_signature=head["source_signature"],
        base_revision=undone.json()["revision"],
    )
    assert (
        client.post(
            url + "/apply",
            json={
                **payload,
                "candidate": bad["id"],
                "revision": undone.json()["revision"],
            },
        ).status_code
        == 409
    )


def test_applying_two_ranges_of_one_candidate_keeps_one_execution(review_app):
    client = client_for(review_app)
    url = BASE + "/episodes/0"
    head = client.get(url).json()
    candidate = review_app.state.annotation_workbench.add_candidate(
        "source",
        0,
        head["document"],
        run="mock-one-attempt",
        source_signature=head["source_signature"],
        base_revision=0,
    )
    args = {
        "source_signature": head["source_signature"],
        "candidate": candidate["id"],
        "start": 0,
        "end": 1,
        "revision": 0,
    }
    first = client.post(url + "/apply", json=args).json()
    second = client.post(
        url + "/apply",
        json={
            **args,
            "revision": first["revision"],
            "start": 1,
            "end": 2,
            "acknowledge_current_revision": True,
        },
    )
    assert second.status_code == 200, second.text
    spans = second.json()["document"]["segments"]
    assert spans[0]["attemptId"] == spans[1]["attemptId"]


def test_undo_restores_only_server_proven_review_and_legacy_result_stays_consistent(
    review_app,
):
    client = client_for(review_app)
    url = BASE + "/episodes/0"
    head = client.get(url).json()
    a = head["document"]["attempts"][0]
    a["values"]["attempt-outcome"] = "失败"
    a["outcome"] = "failure"
    confirmed = client.post(url, json=save_body(head, confirm=[[0, 6]])).json()
    changed = deepcopy(confirmed)
    changed["document"]["attempts"][0]["outcome"] = "success"
    changed = client.post(url, json=save_body(changed)).json()
    assert changed["document"]["attempts"][0]["values"]["attempt-outcome"] == "成功"
    assert changed["document"]["segments"][0].get("review") is None
    undo = client.post(
        url,
        json=save_body(
            {**changed, "document": confirmed["document"]}, operation="undo"
        ),
    )
    assert undo.status_code == 200, undo.text
    restored = undo.json()
    assert (
        restored["document"]["segments"][0]["review"]
        == confirmed["document"]["segments"][0]["review"]
    )
    fake = deepcopy(restored)
    fake["document"]["segments"][0]["note"] = "never reviewed content"
    fake = client.post(url, json=save_body(fake, operation="undo")).json()
    assert fake["document"]["segments"][0].get("review") is None


def test_different_episode_saves_can_overlap_without_dataset_conflict(
    review_app, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    client = client_for(review_app)
    a, b = [client.get(BASE + f"/episodes/{ep}").json() for ep in (0, 1)]
    entered, released = Event(), Event()
    service = review_app.state.annotation_workbench
    original = service._publish

    def publish(row, *args, **kwargs):
        if row["episode"] == 0:
            entered.set()
            assert released.wait(5), (
                "different Episode was blocked by dataset-wide lock"
            )
        return original(row, *args, **kwargs)

    monkeypatch.setattr(service, "_publish", publish)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(client.post, BASE + "/episodes/0", json=save_body(a))
        assert entered.wait(5)
        second = pool.submit(client.post, BASE + "/episodes/1", json=save_body(b))
        try:
            assert second.result(3).status_code == 200
        finally:
            released.set()
        assert first.result(3).status_code == 200


def test_gateway_workspace_writes_are_proxied_never_local(review_app, monkeypatch):
    import httpx
    from fastapi.testclient import TestClient

    from pipeline.server.viewer import create_gateway

    runner = review_app.state.runner
    gateway = create_gateway(runner.settings)
    real_client = httpx.AsyncClient
    calls = []

    async def response(request):
        calls.append(request)
        return httpx.Response(200, json={"from_server": True})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(response)),
    )
    headers = {"Authorization": "Bearer " + runner.settings.token}
    with TestClient(gateway) as client:
        for suffix in ("episodes/0", "episodes/0/apply", "confirm"):
            result = client.post(
                BASE + "/" + suffix,
                headers=headers,
                json={"deliberately": "not-a-local-model"},
            )
            assert result.status_code == 200
            assert result.json() == {"from_server": True}
        assert len(calls) == 3
        assert runner.store.all("annotation_episode") == []
        assert (
            client.post(
                BASE + "/%2e%2e/forbidden", headers=headers, json={}
            ).status_code
            != 200
        )


def test_batch_confirmation_accepts_normal_evidence_and_refuses_unresolved_risks(
    review_app,
):
    client = client_for(review_app)
    head = client.get(BASE + "/episodes/0").json()
    for a in head["document"]["attempts"]:
        a["outcome"] = "success"
    head["document"]["segments"].append(
        {"id": "background", "label": "NULL", "start": 2, "end": 4}
    )
    head["document"]["segments"][0]["evidence"] = [
        {"kind": "model", "note": "支持证据不是未解决风险"}
    ]
    saved = client.post(BASE + "/episodes/0", json=save_body(head)).json()
    body = {
        "source_signature": saved["source_signature"],
        "episodes": [0],
        "revisions": {"0": saved["revision"]},
    }
    assert client.post(BASE + "/confirm", json=body).status_code == 200
    head = client.get(BASE + "/episodes/0").json()
    head["document"]["segments"][0]["risks"] = ["边界证据不足，尚未单独核对"]
    saved = client.post(BASE + "/episodes/0", json=save_body(head)).json()
    body["revisions"]["0"] = saved["revision"]
    assert client.post(BASE + "/confirm", json=body).status_code == 422


def test_legacy_manual_atoms_are_lossless_and_share_undo_and_delivery(
    review_app, monkeypatch
):
    from pipeline.server.subtask_review import SubtaskReview

    source = SubtaskReview._source
    atoms = [
        {
            "role": "user",
            "style": "vqa",
            "content": "原问题",
            "timestamp": 0.2,
            "camera": "front",
            "tool_calls": [
                {"name": "inspect", "arguments": {"nested": [1, None, {"x": "保留"}]}}
            ],
        }
    ]

    def with_atoms(*args):
        data = source(*args)
        data["episodes"][0]["atoms"] = deepcopy(atoms)
        return data

    monkeypatch.setattr(SubtaskReview, "_source", with_atoms)
    client = client_for(review_app)
    head = client.get(BASE + "/episodes/0").json()
    assert head["document"]["manualAtoms"] == atoms
    head["document"]["manualAtoms"][0]["content"] = "修改问题"
    saved = client.post(BASE + "/episodes/0", json=save_body(head)).json()
    bundle = client.post(
        BASE + "/bundle",
        json={
            "source_signature": saved["source_signature"],
            "episodes": [0],
            "revisions": {"0": saved["revision"]},
        },
    ).json()
    assert (
        bundle["episodes"][0]["document"]["manualAtoms"][0]["tool_calls"]
        == atoms[0]["tool_calls"]
    )
    head["document"]["manualAtoms"] = atoms
    restored = client.post(
        BASE + "/episodes/0",
        json=save_body({**saved, "document": head["document"]}, operation="undo"),
    ).json()
    assert restored["document"]["manualAtoms"] == atoms


def test_filter_counts_only_the_named_subtask_not_all_executions(review_app):
    client = client_for(review_app)
    head = client.get(BASE + "/episodes/0").json()
    doc = head["document"]
    old_id = doc["attempts"][0]["subtaskId"]
    doc["templateSnapshot"]["subtasks"] = [
        {"id": s, "name": s} for s in ("grab", "carry", "place", "absent")
    ]
    doc["attempts"] = [
        {"id": f"try-{i}", "subtaskId": s, "values": {}, "outcome": "unknown"}
        for i, s in enumerate(("grab", "grab", "carry", "place"))
    ]
    doc["segments"] = [
        {
            "id": f"segment-{i}",
            "start": i,
            "end": i + 1,
            "label": "SUBTASK",
            "subtaskId": s,
            "attemptId": f"try-{i}",
        }
        for i, s in enumerate(("grab", "grab", "carry", "place"))
    ] + [{"id": "background", "start": 4, "end": 6, "label": "NULL"}]
    saved = client.post(
        BASE + "/episodes/0",
        json=save_body(
            head, template_mapping={old_id: "grab"}, confirm=[[0, 6]], complete=True
        ),
    )
    assert saved.status_code == 200, saved.text
    result = client.get(BASE + "/episodes?subtask=grab&count=2").json()["episodes"]
    assert [r["episode"] for r in result] == [0]
    assert result[0]["counts"]["recorded_count"] == 2
    assert client.get(BASE + "/episodes?subtask=grab&count=4").json()["episodes"] == []
    assert client.get(BASE + "/episodes?subtask=absent&count=0").json()["episodes"][0][
        "counts"
    ]["complete"]


def test_confirm_partial_span_preserves_execution_and_undo_review(review_app):
    client = client_for(review_app)
    url = BASE + "/episodes/0"
    head = client.get(url).json()
    original = deepcopy(head["document"])
    attempt = original["segments"][0]["attemptId"]
    checked = client.post(url, json=save_body(head, confirm=[[1, 2]])).json()
    assert [
        (s["start"], s["end"], bool(s.get("review")))
        for s in checked["document"]["segments"]
    ] == [(0, 1, False), (1, 2, True), (4, 6, False)]
    assert checked["document"]["segments"][1]["attemptId"] == attempt
    assert checked["document"]["attempts"] == original["attempts"]
    undo = client.post(
        url, json=save_body({**checked, "document": original}, operation="undo")
    )
    assert undo.status_code == 200, undo.text
    assert len(undo.json()["document"]["segments"]) == 2


def test_deleted_attempt_keeps_historical_template_through_upgrade_undo_and_export(
    review_app,
):
    from pipeline.server.annotation_contract import validate_annotation

    client = client_for(review_app)
    url = BASE + "/episodes/0"
    head = client.get(url).json()
    original = client.post(url, json=save_body(head, confirm=[[0, 6]])).json()
    old = deepcopy(original["document"])
    deleted = deepcopy(old)
    deleted["segments"] = []
    for a in deleted["attempts"]:
        a["archived"] = True
        a["provenance"]["archivedTemplateSnapshot"] = deepcopy(old["templateSnapshot"])
    archived = client.post(
        url, json=save_body({**original, "document": deleted})
    ).json()
    upgraded = deepcopy(archived["document"])
    upgraded["templateSnapshot"] = {
        "id": "empty",
        "name": "Removed label",
        "subtasks": [],
        "fields": [],
    }
    upgraded["templateId"] = "empty"
    upgraded["countEvidence"] = {}
    response = client.post(
        url, json=save_body({**archived, "document": upgraded}, template_mapping={})
    )
    assert response.status_code == 200, response.text
    current = response.json()
    assert current["document"]["attempts"] == deleted["attempts"]
    selection = {
        "source_signature": current["source_signature"],
        "episodes": [0],
        "revisions": {"0": current["revision"]},
    }
    bundle = client.post(BASE + "/bundle", json=selection).json()
    assert (
        validate_annotation(bundle["episodes"][0]["document"], 6)["attempts"]
        == deleted["attempts"]
    )
    undo = client.post(
        url,
        json=save_body({**current, "document": archived["document"]}, operation="undo"),
    )
    assert undo.status_code == 200, undo.text
    undo = client.post(
        url, json=save_body({**undo.json(), "document": old}, operation="undo")
    )
    assert undo.status_code == 200, undo.text
    assert undo.json()["document"]["attempts"] == old["attempts"]
    assert (
        undo.json()["document"]["segments"][0]["review"] == old["segments"][0]["review"]
    )
