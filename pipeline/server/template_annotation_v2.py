"""Recover canonical independent executions/segments from template requests."""

from __future__ import annotations

from copy import deepcopy

from pipeline.shared.core import digest

from .annotation_contract import validate_annotation
from .argus_template_contract import validate_task_reply


def make_v2_document(spec, context, units):
    import math

    template = spec["template"]
    fps = context["fps"]
    count = context["n_state_frames"]
    layout = []
    for ctx, raw in units:
        if (
            raw.get("parse_ok") is False
            or raw.get("dry_run")
            or raw.get("_label_failed")
        ):
            raise ValueError("request did not return valid template data")
        task = deepcopy((raw.get("labels") or {}).get("bodypipeline_task"))
        validate_task_reply(task, spec, ctx)
        if task.get("version", 1) == 1:
            attempts = []
            segments = []
            for i, a in enumerate(task["attempts"]):
                aid = f"legacy-a-{i}"
                attempts.append(
                    {
                        "id": aid,
                        "subtask_id": a["subtask_id"],
                        "values": a["values"],
                        "outcome": {
                            "成功": "success",
                            "失败": "failure",
                            "无法判断": "unknown",
                        }.get(a["values"].get("attempt-outcome")),
                        "continues_before": a.get("continues_before", False),
                        "continues_after": a.get("continues_after", False),
                    }
                )
                segments.append(
                    {
                        "id": f"legacy-s-{i}",
                        "label": "SUBTASK",
                        "subtask_id": a["subtask_id"],
                        "attempt_id": aid,
                        "start_s": a["start_s"],
                        "end_s": a["end_s"],
                    }
                )
            for i, s in enumerate(task.get("null_segments", [])):
                segments.append({**s, "id": f"legacy-n-{i}", "label": "NULL"})
            task = {**task, "version": 2, "attempts": attempts, "segments": segments}
            task.pop("null_segments", None)
        piece = ctx.get("piece") or {}
        offset = round(float(piece.get("t0_s", 0)) * fps)
        size = ctx["n_state_frames"]
        if (
            ctx.get("fps") != fps
            or type(size) is not int
            or size < 1
            or offset < 0
            or offset + size > count
        ):
            raise ValueError("request frame base does not match Episode")
        layout.append((offset, offset + size, ctx, raw, task))
    layout.sort(key=lambda x: x[0])
    if not layout or any(
        a[0] >= b[0] or a[1] >= b[1] for a, b in zip(layout, layout[1:])
    ):
        raise ValueError("request layout is empty or ambiguous")
    doc = {
        "version": 2,
        "templateId": template["id"],
        "templateSnapshot": deepcopy(template),
        "values": deepcopy(layout[0][4].get("values", {}))
        if len(layout) == 1 and layout[0][0] == 0 and layout[0][1] == count
        else {},
        "attempts": [],
        "segments": [],
        "frames": [],
        "countEvidence": {},
        "history": [],
        "provenance": {
            "mode": "template",
            "templateSignature": spec["signature"],
            "requests": [],
        },
    }
    warnings = []
    continuing = {}
    names = {s["id"]: s["name"] for s in template["subtasks"]}
    for i, (offset, end, ctx, raw, task) in enumerate(layout):
        lo = (
            (layout[i - 1][1] + offset) // 2
            if i and layout[i - 1][1] >= offset
            else offset
        )
        hi = (
            (end + layout[i + 1][0]) // 2
            if i + 1 < len(layout) and end >= layout[i + 1][0]
            else end
        )
        namespace = digest([spec["signature"], context["episode_id"], offset, end])[:24]
        mapping = {
            a["id"]: "ai-attempt-" + digest([namespace, a["id"]])[:32]
            for a in task["attempts"]
        }
        boundaries = raw.get("pipeline_boundary") or {}
        evidence = []
        pending = set(boundaries.get("pending", []))
        risk_members = set()
        for request in boundaries.get("requests", {}).values():
            for group in request.get("groups", []):
                if group["id"] in pending:
                    risk_members.update(sid for sid, _ in group["members"])
            for proof in request.get("evidence", []):
                evidence.append(
                    {
                        **proof,
                        "request_id": digest(request.get("prompt_text", "")),
                        "sampledFrames": request.get("frames", []),
                        "sampledTimes": request.get("samples", []),
                        "imageReceipts": request.get("images", []),
                    }
                )
                if not proof["supported"]:
                    risk_members.update(sid for sid, _ in proof["members"])
        for a in task["attempts"]:
            sid = a.get("subtask_id")
            associated = [s for s in task["segments"] if s.get("attempt_id") == a["id"]]
            starts = [math.ceil(s["start_s"] * fps - 1e-6) + offset for s in associated]
            stops = [math.ceil(s["end_s"] * fps - 1e-6) + offset for s in associated]
            if a.get("continues_before") and (
                not ctx.get("piece")
                or not starts
                or min(starts) != offset
                or offset == 0
            ):
                raise ValueError(
                    "continuation before is not at a synthetic request boundary"
                )
            if a.get("continues_after") and (
                not ctx.get("piece") or not stops or max(stops) != end or end == count
            ):
                raise ValueError(
                    "continuation after is not at a synthetic request boundary"
                )
            previous = (
                next(
                    (
                        x
                        for x in reversed(doc["attempts"])
                        if continuing.get(x["id"]) == i - 1
                        and x.get("subtaskId") == sid
                        and any(
                            s.get("attemptId") == x["id"] and s["end"] == lo
                            for s in doc["segments"]
                        )
                    ),
                    None,
                )
                if i and a.get("continues_before")
                else None
            )
            if previous:
                mapping[a["id"]] = previous["id"]
                if previous.get("outcome") != a.get("outcome"):
                    previous.pop("outcome", None)
                previous["values"] = {
                    k: v
                    for k, v in previous["values"].items()
                    if a.get("values", {}).get(k) == v
                }
                previous["provenance"].setdefault("continuations", []).append(
                    deepcopy(a)
                )
                execution = previous
            else:
                execution = {
                    "id": mapping[a["id"]],
                    "subtaskId": sid,
                    "subtaskName": names.get(sid),
                    "outcome": a.get("outcome"),
                    "values": deepcopy(a.get("values", {})),
                    "provenance": {
                        "request": namespace,
                        "localAttemptId": a["id"],
                        "rawAttempt": deepcopy(a),
                    },
                }
                doc["attempts"].append(execution)
            continuing[execution["id"]] = i if a.get("continues_after") else -1
        for s in task["segments"]:
            start = max(lo, offset + math.ceil(s["start_s"] * fps - 1e-6))
            stop = min(hi, offset + math.ceil(s["end_s"] * fps - 1e-6))
            if start >= stop:
                continue
            proof = [
                p for p in evidence if any(sid == s["id"] for sid, _ in p["members"])
            ]
            risks = (
                ["交界附近证据不足或预算/重试未完成，请人工比较实际帧"]
                if s["id"] in risk_members
                else []
            )
            if not boundaries and (
                s["start_s"] > 0 or s["end_s"] < (end - offset) / fps
            ):
                risks.append("尚无边界加密取证记录")
            doc["segments"].append(
                {
                    "id": "ai-segment-" + digest([namespace, s["id"]])[:32],
                    "start": start,
                    "end": stop,
                    "label": s["label"],
                    "subtaskId": s.get("subtask_id"),
                    "attemptId": mapping.get(s.get("attempt_id")),
                    "note": s.get("note", ""),
                    "description": s.get("description", ""),
                    "values": deepcopy(s.get("values", {})),
                    "evidence": proof,
                    "risks": risks,
                    "provenance": {
                        "request": namespace,
                        "localSegmentId": s["id"],
                        "boundaryConvention": "half-open seconds -> ceil(frame-epsilon)",
                    },
                }
            )
        for j, f in enumerate(task.get("frames", [])):
            frame = offset + math.ceil(f["t_s"] * fps - 0.5 - 1e-6)
            if lo <= frame < hi:
                doc["frames"].append(
                    {
                        "id": "ai-frame-" + digest([namespace, j])[:32],
                        "frame": frame,
                        "values": deepcopy(f["values"]),
                    }
                )
        if offset or end < count:
            if task.get("values"):
                warnings.append("分段请求的 Episode 字段未采用，原值保存在请求追溯中。")
        doc["provenance"]["requests"].append(
            {
                "id": namespace,
                "frames": [offset, end],
                "model": raw.get("model"),
                "model_served": raw.get("model_served"),
                "prompt_text": raw.get("prompt_text"),
                "request_text_parts": raw.get("request_text_parts"),
                "input_identity": raw.get("input_identity"),
                "config": raw.get("config"),
                "boundary_refinement": boundaries,
                "raw_business_result": task,
                "auxiliary_observations": (raw.get("labels") or {}).get(
                    "auxiliary_observations"
                ),
                "usage": raw.get("usage"),
            }
        )
    doc["segments"].sort(key=lambda s: s["start"])
    active = {s.get("attemptId") for s in doc["segments"]}
    for a in doc["attempts"]:
        a["archived"] = a["id"] not in active
    if len(layout) > 1:
        warnings.append(
            "分段生成，Episode 共享字段留空；跨请求关联仅依据明确的合成边界延续。"
        )
    if (
        layout[0][0] > 0
        or layout[-1][1] < count
        or any(a[1] < b[0] for a, b in zip(layout, layout[1:]))
    ):
        warnings.append("部分请求未完成，保留有效片段和真实空白。")
    return validate_annotation(doc, count), warnings
