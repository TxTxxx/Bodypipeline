"""The template mode has one business schema, independent executions and evidence."""

import pytest

from pipeline.server.argus_template_contract import (
    logical_retry_reason,
    parse_template_reply,
    task_contract,
    validate_task_reply,
)
from pipeline.server.argus_template_runtime import boundary_groups, dense_frames
from pipeline.server.template_annotation import make_document
from pipeline.shared.core import atomic_json, digest


@pytest.fixture
def specification(tmp_path, monkeypatch):
    template = {
        "id": "cup",
        "name": "杯子",
        "subtasks": [
            {
                "id": "grab",
                "name": "抓取",
                "definition": "接近到提起",
                "startCondition": "明确接近",
                "endCondition": "提起或放弃",
            }
        ],
        "fields": [
            {
                "id": "quality",
                "label": "质量",
                "scope": "attempt",
                "kind": "rating",
                "minimum": 0,
                "maximum": 10,
                "scale": "0=差，10=好",
            }
        ],
    }
    from pipeline.server.task_annotations import TaskTemplate

    template = TaskTemplate.model_validate(template).model_dump(exclude_none=True)
    spec = {
        "version": 2,
        "template": template,
        "signature": digest(template),
        "boundary_target_s": 0.2,
        "boundary_window_s": 1.0,
    }
    atomic_json(tmp_path / "task-template.json", spec)
    monkeypatch.setenv(
        "PIPELINE_ARGUS_TASK_TEMPLATE", str(tmp_path / "task-template.json")
    )
    return spec


def business(spec):
    return {
        "version": 2,
        "template_id": "cup",
        "template_signature": spec["signature"],
        "values": {},
        "attempts": [
            {
                "id": "try-a",
                "subtask_id": "grab",
                "outcome": "failure",
                "values": {"quality": None},
            },
            {
                "id": "try-b",
                "subtask_id": "grab",
                "outcome": "unknown",
                "values": {"quality": 9},
            },
        ],
        "segments": [
            {
                "id": "visible-1",
                "start_s": 0.0,
                "end_s": 0.2,
                "label": "SUBTASK",
                "subtask_id": "grab",
                "attempt_id": "try-a",
            },
            {
                "id": "occluded",
                "start_s": 0.2,
                "end_s": 0.3,
                "label": "UNKNOWN",
                "attempt_id": "try-a",
            },
            {
                "id": "visible-2",
                "start_s": 0.3,
                "end_s": 0.5,
                "label": "SUBTASK",
                "subtask_id": "grab",
                "attempt_id": "try-a",
            },
            {
                "id": "retry",
                "start_s": 0.5,
                "end_s": 0.8,
                "label": "SUBTASK",
                "subtask_id": "grab",
                "attempt_id": "try-b",
            },
            {
                "id": "outside",
                "start_s": 0.8,
                "end_s": 0.9,
                "label": "OUT_OF_TEMPLATE",
                "description": "清理桌面",
                "attempt_id": "try-b",
            },
            {"id": "last", "start_s": 0.9, "end_s": 1.0, "label": "NULL"},
        ],
        "frames": [],
    }


def test_template_only_reply_needs_no_native_timeline_and_keeps_missing_scores(
    specification,
):
    import json

    spec = specification
    task = business(spec)
    context = {
        "episode_id": "episode_000000",
        "episode_index": 0,
        "fps": 10,
        "n_state_frames": 10,
    }
    labels, ok = parse_template_reply(
        json.dumps({"bodypipeline_task": task}), {"bodypipeline_context": context}
    )
    assert ok and set(labels) == {"bodypipeline_task"}
    assert "Do not produce the native Argus" in task_contract(context)
    document, warnings = make_document(
        spec,
        context,
        [
            (
                context,
                {
                    "parse_ok": True,
                    "labels": labels,
                    "prompt_text": "ACTUAL FIXTURE REQUEST",
                    "config": {"data_frame_indices": [0, 2, 5, 9]},
                },
            )
        ],
    )
    assert document["version"] == 2
    assert len(document["attempts"]) == 2
    assert (
        document["segments"][0]["attemptId"]
        == document["segments"][1]["attemptId"]
        == document["segments"][2]["attemptId"]
    )
    assert (
        document["segments"][-1]["start"] == 9 and document["segments"][-1]["end"] == 10
    )
    assert document["attempts"][0]["values"]["quality"] is None
    assert document["attempts"][1]["outcome"] == "unknown"
    assert (
        document["provenance"]["requests"][0]["prompt_text"] == "ACTUAL FIXTURE REQUEST"
    )
    assert document["segments"][0].get("review") is None


def test_unknown_identity_and_second_native_timeline_are_validation_errors(
    specification,
):
    import json

    task = business(specification)
    task["attempts"][0]["subtask_id"] = "invented"
    with pytest.raises(ValueError, match="unknown Subtask"):
        validate_task_reply(task, specification)
    labels, ok = parse_template_reply(
        json.dumps(
            {
                "bodypipeline_task": business(specification),
                "timeline": [{"verb_class": "invented"}],
            }
        )
    )
    assert not ok and labels["_template_invalid"]


def test_request_retry_count_is_shared_across_network_format_and_template_errors(
    specification, monkeypatch
):
    variants = [
        {"final_rejection": {"http_status": 503}},
        {"parse_ok": False, "response": {}, "labels": {"_template_invalid": True}},
        {"no_reply_kind": "client_timeout"},
        {"final_dispatch_outcome": "unverified"},
    ]
    for i, event in enumerate(variants, 1):
        assert bool(logical_retry_reason({**event, "final_attempt_index": i})) == (
            i < 4
        )
    for status in (400, 401, 402, 403, 404, 422):
        assert (
            logical_retry_reason(
                {"final_rejection": {"http_status": status}, "final_attempt_index": 1}
            )
            is None
        )
    monkeypatch.delenv("PIPELINE_ARGUS_TASK_TEMPLATE")
    assert logical_retry_reason(
        {"no_reply_kind": "client_timeout", "final_attempt_index": 1}
    )
    assert logical_retry_reason(
        {"parse_ok": False, "response": {}, "final_attempt_index": 2}
    )


def test_dense_evidence_is_local_to_candidate_boundaries(specification):
    task = business(specification)
    groups = boundary_groups(task, 1.0)
    assert len(groups) == 5
    assert any(len(g["members"]) == 2 for g in groups)
    # A long clip's evidence stays local, with the two actual neighboring frames.
    frames = dense_frames([{"coarse_s": 100.0}], 30, 9000, 1.0, 0.2)
    assert min(frames) == 2970 and max(frames) == 3030
    assert 2999 in frames and 3000 in frames
    assert len(frames) < 100
    assert 0 not in frames and 8999 not in frames


def test_partial_requests_keep_valid_content_instead_of_throwing_it_away(specification):
    from pipeline.server.template_annotation_v2 import make_v2_document

    ctx = {"episode_id": "episode_000000", "fps": 10, "n_state_frames": 30}
    piece = {
        **ctx,
        "n_state_frames": 10,
        "piece": {"t0_s": 1.0, "of": "episode_000000"},
    }
    document, warnings = make_v2_document(
        specification,
        ctx,
        [
            (
                piece,
                {
                    "parse_ok": True,
                    "labels": {"bodypipeline_task": business(specification)},
                },
            )
        ],
    )
    assert document["segments"][0]["start"] == 10
    assert document["segments"][-1]["end"] == 20
    assert document["values"] == {}
    assert warnings


def test_capability_detection_distinguishes_native_old_template_and_v2(tmp_path):
    from types import SimpleNamespace

    from pipeline.server.argus_annotation import (
        available,
        execution_available,
        template_available,
    )

    root = tmp_path / "argus"
    (root / "review").mkdir(parents=True)
    (root / "label").mkdir()
    python = root / "python"
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    settings = SimpleNamespace(argus_src=str(root), argus_python=str(python))
    assert available(settings)[0]
    assert not template_available(settings)
    assert not execution_available(settings)
    for name in ("episode.py", "harness.py", "pipeline_template.py"):
        (root / "label" / name).write_text(
            "# pipeline-argus-task-template:v1\n# pipeline-argus-template-retry:v1\n"
        )
    assert available(settings)[0] and not template_available(settings)
    for name in ("label/episode.py", "label/harness.py", "review/__main__.py"):
        p = root / name
        p.write_text(
            (p.read_text() if p.exists() else "")
            + "\n# pipeline-argus-workbench-contract:v2\n"
        )
    with (root / "label/pipeline_template.py").open("a") as f:
        f.write("\nCONTRACT_VERSION = 2\n")
    (root / "label/pipeline_runtime.py").write_text(
        "def refine_outputs(): pass\ndef select_scope(): pass\n"
    )
    assert template_available(settings)
    assert execution_available(settings)


@pytest.mark.parametrize(
    "flag,value",
    [
        ("continues_before", True),
        ("continues_after", True),
        ("continues_before", "false"),
        ("continues_after", 1),
    ],
)
def test_non_piece_continuations_fail_the_shared_parser_and_retry(
    specification, flag, value
):
    import json

    task = business(specification)
    task["attempts"][0][flag] = value
    ctx = {"fps": 10, "n_state_frames": 10}
    with pytest.raises(ValueError, match="continuation"):
        validate_task_reply(task, specification, ctx)
    _, valid = parse_template_reply(
        json.dumps({"bodypipeline_task": task}), {"bodypipeline_context": ctx}
    )
    assert not valid
    for index in range(1, 5):
        reason = logical_retry_reason(
            {"parse_ok": False, "response": {}, "final_attempt_index": index}
        )
        assert bool(reason) == (index < 4)


def test_piece_continuations_require_real_clipped_edges(specification):
    from copy import deepcopy

    ctx = {
        "fps": 10,
        "n_state_frames": 10,
        "piece": {"t0_s": 1, "index": 2, "count": 3},
    }
    task = business(specification)
    task["attempts"][0]["continues_before"] = True
    task["attempts"][1]["continues_after"] = True
    task["segments"][-1].update(label="UNKNOWN", attempt_id="try-b")
    validate_task_reply(task, specification, ctx)
    last = deepcopy(ctx)
    last["piece"]["index"] = 3
    with pytest.raises(ValueError, match="continuation after"):
        validate_task_reply(task, specification, last)
    task["segments"][0]["start_s"] = 0.1
    with pytest.raises(ValueError, match="continuation before"):
        validate_task_reply(task, specification, ctx)


def test_resume_classifies_bad_continuation_only(specification, tmp_path):
    from copy import deepcopy

    from pipeline.server.template_annotation import (
        invalid_received_units,
        retry_manifest,
    )

    for ep in (0, 1):
        name = f"episode_{ep:06}"
        atomic_json(
            tmp_path / f"argus/episodes/{name}/context.json",
            {"episode_id": name, "fps": 10, "n_state_frames": 10},
        )
        task = deepcopy(business(specification))
        if ep == 1:
            task["attempts"][0]["continues_before"] = True
        atomic_json(
            tmp_path / f"argus/run/out/{name}.json",
            {
                "parse_ok": True,
                "labels": {"bodypipeline_task": task},
                "response_text": str(ep),
            },
        )
    assert invalid_received_units(tmp_path) == ["episode_000001"]
    assert len(retry_manifest(tmp_path)["invalid_replies"]) == 1
