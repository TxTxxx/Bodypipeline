"""Executable consensus: identities, review and target-specific count evidence."""

from copy import deepcopy

import pytest

from pipeline.server.annotation_contract import (
    AnnotationDocument,
    completion,
    content_key,
    count_attempts,
    migrate_document,
    reconcile_review,
)


def legacy():
    return {
        "version": 1,
        "templateId": "t",
        "templateSnapshot": {
            "id": "t",
            "name": "杯子",
            "subtasks": [{"id": "a", "name": "抓取"}],
            "fields": [
                {
                    "id": "attempt-comment",
                    "label": "片段备注",
                    "scope": "attempt",
                    "kind": "textarea",
                }
            ],
        },
        "values": {},
        "attempts": [
            {
                "id": "try-a",
                "subtaskId": "a",
                "subtaskName": "抓取",
                "start": 0,
                "end": 4,
                "values": {"attempt-comment": "原备注"},
            }
        ],
        "nullSegments": [],
        "frames": [],
    }


def test_migration_keeps_attempt_identity_scope_and_original_document():
    old = legacy()
    doc = migrate_document(old)
    assert doc["version"] == 2
    assert doc["attempts"][0]["id"] == "try-a"
    assert doc["segments"][0]["attemptId"] == "try-a"
    assert doc["attempts"][0]["values"]["attempt-comment"] == "原备注"
    assert doc["templateSnapshot"]["fields"][0]["scope"] == "attempt"
    assert doc["history"][0]["document"] == old
    assert migrate_document(doc) == doc


def test_unknown_complete_episode_does_not_prove_exact_count():
    doc = migrate_document(legacy())
    doc["segments"] = [
        {
            "id": "one",
            "start": 0,
            "end": 2,
            "label": "SUBTASK",
            "subtaskId": "a",
            "attemptId": "try-a",
        },
        {"id": "blind", "start": 2, "end": 3, "label": "UNKNOWN"},
        {
            "id": "two",
            "start": 3,
            "end": 4,
            "label": "SUBTASK",
            "subtaskId": "a",
            "attemptId": "try-a",
        },
    ]
    doc["attempts"][0]["outcome"] = "failure"
    doc = reconcile_review(None, doc, confirm=[(0, 4)], actor="human")
    assert completion(doc, 4)["eligible"]
    assert count_attempts(doc, "a", 4)["recorded_count"] == 1
    assert count_attempts(doc, "a", 4)["complete"] is False
    doc["segments"][1]["attemptId"] = "try-a"
    doc = reconcile_review(doc, doc, confirm=[(0, 4)], actor="human")
    assert count_attempts(doc, "a", 4)["complete"] is True


def test_missing_is_not_zero_and_explicit_evidence_is_content_bound():
    doc = migrate_document(legacy())
    doc["segments"] = []
    doc["attempts"] = []
    assert count_attempts(doc, "a", 4) == {
        "recorded_count": 0,
        "complete": False,
        "basis": "标注或人工计数依据不足",
    }
    doc["countEvidence"] = {
        "a": {
            "reason": "逐帧核查其他传感器，未执行抓取",
            "contentKey": content_key(doc),
        }
    }
    assert count_attempts(doc, "a", 4)["complete"]
    doc["segments"] = [{"id": "null", "start": 0, "end": 4, "label": "NULL"}]
    assert not count_attempts(doc, "a", 4)["complete"]


def test_review_invalidation_covers_shared_attempt_but_keeps_unrelated_segment():
    doc = migrate_document(legacy())
    doc["segments"] += [{"id": "rest", "start": 4, "end": 5, "label": "NULL"}]
    doc = reconcile_review(None, doc, confirm=[(0, 5)], actor="person")
    changed = deepcopy(doc)
    changed["attempts"][0]["outcome"] = "success"
    changed = reconcile_review(doc, changed)
    assert changed["segments"][0].get("review") is None
    assert changed["segments"][1].get("review") is not None
    assert not completion(changed, 5)["eligible"]


def test_null_cannot_link_attempt_and_formal_unknown_id_is_refused():
    doc = migrate_document(legacy())
    doc["segments"][0]["label"] = "NULL"
    with pytest.raises(ValueError):
        AnnotationDocument.model_validate(doc)
    doc = migrate_document(legacy())
    doc["attempts"][0]["subtaskId"] = "not-in-template"
    with pytest.raises(ValueError):
        AnnotationDocument.model_validate(doc)
