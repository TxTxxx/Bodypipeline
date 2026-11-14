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
