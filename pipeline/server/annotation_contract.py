"""Canonical v2 annotation contract. Frames are data frames, ranges half-open.

The legacy v1 document remains readable. Migration preserves its exact snapshot,
IDs, field scopes and raw document; geometry never creates a second attempt.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Literal

from pydantic import Field, StrictInt, model_validator

from pipeline.shared.core import DomainError, digest, now

from .task_annotations import AnnotationModel, FrameNote, TaskTemplate, Value


class Execution(AnnotationModel):
    id: str = Field(min_length=1, max_length=160)
    subtaskId: str | None = None
    subtaskName: str | None = None
    outcome: Literal["success", "failure", "unknown"] | None = None
    values: dict[str, Value] = Field(default_factory=dict)
    note: str = Field(default="", max_length=20000)
    archived: bool = False
    provenance: dict = Field(default_factory=dict)


class ReviewStamp(AnnotationModel):
    contentKey: str
    at: float
    actor: str


class Span(AnnotationModel):
    id: str = Field(min_length=1, max_length=160)
    start: StrictInt = Field(ge=0)
    end: StrictInt = Field(gt=0)
    label: Literal["SUBTASK", "NULL", "UNKNOWN", "OUT_OF_TEMPLATE"]
    subtaskId: str | None = None
    attemptId: str | None = None
    note: str = Field(default="", max_length=20000)
    description: str = Field(default="", max_length=20000)
    values: dict[str, Value] = Field(default_factory=dict)
    review: ReviewStamp | None = None
    evidence: list[dict] = Field(default_factory=list, max_length=200)
    risks: list[str] = Field(default_factory=list, max_length=200)
    provenance: dict = Field(default_factory=dict)


class CountEvidence(AnnotationModel):
    reason: str = Field(min_length=1, max_length=20000)
    contentKey: str


class AnnotationDocument(AnnotationModel):
    version: Literal[2]
    templateId: str
    templateSnapshot: TaskTemplate
    values: dict[str, Value] = Field(default_factory=dict)
    attempts: list[Execution] = Field(default_factory=list, max_length=10000)
    segments: list[Span] = Field(default_factory=list, max_length=10000)
    frames: list[FrameNote] = Field(default_factory=list, max_length=10000)
    manualAtoms: list[dict] = Field(default_factory=list, max_length=10000)
    countEvidence: dict[str, CountEvidence] = Field(default_factory=dict)
    provenance: dict = Field(default_factory=dict)
    history: list[dict] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid(self):
        if self.templateId != self.templateSnapshot.id:
            raise ValueError("模板身份与快照不一致")
        ids = [r.id for r in [*self.attempts, *self.segments, *self.frames]]
        if len(set(ids)) != len(ids):
            raise ValueError("尝试、片段或帧观察的稳定 ID 重复")
        steps = {s.id for s in self.templateSnapshot.subtasks}
        executions = {a.id: a for a in self.attempts}
        archived_fields = {}
        for a in self.attempts:
            snapshot = (
                a.provenance.get("archivedTemplateSnapshot") if a.archived else None
            )
            historical = TaskTemplate.model_validate(snapshot) if snapshot else None
            if historical is not None:
                archived_fields[id(a.values)] = {f.id: f for f in historical.fields}
            if (
                a.subtaskId is not None
                and a.subtaskId not in steps
                and not (
                    a.archived
                    and historical is not None
                    and a.subtaskId in {s.id for s in historical.subtasks}
                )
            ):
                raise ValueError(
                    "尝试引用了模板以外的 Subtask ID；归档记录须保留其原模板快照"
                )
        stop = 0
        for s in sorted(self.segments, key=lambda s: (s.start, s.end)):
            if s.start < stop or s.end <= s.start:
                raise ValueError("主轨道区间不得为空或重叠")
            stop = s.end
            a = executions.get(s.attemptId)
            if s.attemptId and (a is None or a.archived):
                raise ValueError("片段关联的尝试不存在或已归档")
            if s.label == "SUBTASK":
                if s.subtaskId not in steps or a is None or a.subtaskId != s.subtaskId:
                    raise ValueError("正式片段必须使用模板 ID 并明确关联同标签尝试")
            elif s.subtaskId is not None:
                raise ValueError("特殊状态不是正式 Subtask")
            if s.label == "NULL" and s.attemptId:
                raise ValueError("NULL 不得关联尝试")
        fields = {f.id: f for f in self.templateSnapshot.fields}
        for scope, values in [
            ("episode", self.values),
            *(("attempt", a.values) for a in self.attempts),
            *(("segment", s.values) for s in self.segments),
            *(("frame", f.values) for f in self.frames),
        ]:
            for key, value in values.items():
                f = archived_fields.get(id(values), fields).get(key)
                if (
                    f is None or value is None
                ):  # removed historical values remain lossless
                    continue
                valid = f.scope == scope
                if f.kind == "rating":
                    valid &= type(value) in (int, float) and (
                        f.minimum if f.minimum is not None else 1
                    ) <= value <= (f.maximum if f.maximum is not None else 5)
                elif f.kind == "number":
                    valid &= type(value) in (int, float)
                elif f.kind == "bool":
                    valid &= type(value) is bool
                elif f.kind in {"text", "textarea"}:
                    valid &= isinstance(value, str)
                elif f.kind == "single":
                    valid &= isinstance(value, str) and value in (f.options or [])
                elif f.kind == "multi":
                    valid &= isinstance(value, list) and all(
                        v in (f.options or []) for v in value
                    )
                if not valid:
                    raise ValueError(f"字段 {f.label} 的值或作用范围错误")
        if any(k not in steps for k in self.countEvidence):
            raise ValueError("计数证据引用了模板以外的 Subtask")
        return self


def migrate_document(raw: dict) -> dict:
    """Idempotent, deterministic v1 -> v2; never infer identity from names."""
    doc = deepcopy(raw)
    if doc.get("version") == 2:
        return doc
    if doc.get("version") != 1:
        raise ValueError("不支持的标注版本")
    result = {
        k: deepcopy(doc[k])
        for k in ("templateId", "templateSnapshot", "values", "frames")
    }
    result.update(
        version=2,
        segments=[],
        attempts=[],
        countEvidence={},
        provenance={"migration": "TaskDocument/v1"},
        history=[{"kind": "legacy-document", "document": doc}],
    )
    for old in doc["attempts"]:
        aid = old["id"]
        outcome = {"成功": "success", "失败": "failure", "无法判断": "unknown"}.get(
            old["values"].get("attempt-outcome")
        )
        result["attempts"].append(
            {
                "id": aid,
                "subtaskId": old["subtaskId"],
                "subtaskName": old["subtaskName"],
                "outcome": outcome,
                "values": deepcopy(old["values"]),
            }
        )
        result["segments"].append(
            {
                "id": "segment-" + digest(aid)[:32],
                "start": old["start"],
                "end": old["end"],
                "label": "SUBTASK",
                "subtaskId": old["subtaskId"],
                "attemptId": aid,
            }
        )
    for old in doc.get("nullSegments", []):
        result["segments"].append({**deepcopy(old), "label": "NULL"})
    result["segments"].sort(key=lambda s: (s["start"], s["end"]))
    return result


def validate_annotation(raw: dict, frames: int) -> dict:
    raw = migrate_document(raw)
    # The legacy standard result field remains available to old consumers but
    # cannot contradict the canonical execution result after an edit.
    outcomes = {"success": "成功", "failure": "失败", "unknown": "无法判断"}
    for a in raw["attempts"]:
        if "attempt-outcome" in a.get("values", {}):
            a["values"]["attempt-outcome"] = outcomes.get(a.get("outcome"))
    d = AnnotationDocument.model_validate(raw)
    if any(s.end > frames for s in d.segments) or any(
        f.frame >= frames for f in d.frames
    ):
        raise DomainError("标注超出来源 Episode 数据帧范围", 422)
    result = d.model_dump(exclude_none=True)
    import json

    if (
        len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode())
        > 8 * 1024 * 1024
    ):
        raise DomainError("单 Episode 标注超过 8 MiB", 422)
    return result


def template_key(doc: dict) -> str:
    return digest(doc["templateSnapshot"])


def content_key(doc: dict) -> str:
    doc = AnnotationDocument.model_validate(doc).model_dump(exclude_none=True)
    return digest(
        {
            k: v
            for k, v in doc.items()
            if k not in {"history", "countEvidence", "provenance"}
        }
        | {
            "segments": [
                {k: v for k, v in s.items() if k != "review"} for s in doc["segments"]
            ]
        }
    )


def span_key(doc: dict, s: dict) -> str:
    a = next((a for a in doc["attempts"] if a["id"] == s.get("attemptId")), None)
    return digest(
        {
            "template": doc["templateSnapshot"],
            "segment": {k: v for k, v in s.items() if k != "review"},
            "attempt": a,
            "values": doc["values"],
            "manualAtoms": doc.get("manualAtoms", []),
            "frames": [f for f in doc["frames"] if s["start"] <= f["frame"] < s["end"]],
        }
    )


def reconcile_review(
    previous: dict | None,
    incoming: dict,
    *,
    confirm: list[tuple[int, int]] = (),
    actor="human",
) -> dict:
    result = AnnotationDocument.model_validate(incoming).model_dump(exclude_none=True)
    prior = {s["id"]: s for s in (previous or {}).get("segments", [])}
    # A range may cover only part of an existing span. Split geometry without
    # inventing executions; this makes a partial human check expressible.
    split, inherited = [], set()
    for original in result["segments"]:
        edges = sorted(
            {original["start"], original["end"]}
            | {
                edge
                for interval in confirm
                for edge in interval
                if original["start"] < edge < original["end"]
            }
        )
        old = prior.get(original["id"], {})
        trusted = old.get("review", {}).get("contentKey") == span_key(result, original)
        if len(edges) > 2:
            result["history"].append(
                {"kind": "confirmation-range-split", "segment": deepcopy(original)}
            )
        for index, (lo, hi) in enumerate(zip(edges, edges[1:])):
            piece = {**deepcopy(original), "start": lo, "end": hi}
            if index:
                piece["id"] = (
                    "confirm-"
                    + digest([original["id"], lo, hi, span_key(result, original)])[:40]
                )
            if trusted:
                inherited.add(piece["id"])
            split.append(piece)
    result["segments"] = split
    for s in result["segments"]:
        s.pop("review", None)
        key = span_key(result, s)
        old = prior.get(s["id"], {})
        if old.get("review", {}).get("contentKey") == key:
            s["review"] = old["review"]
        if s["id"] in inherited and not s.get("review"):
            s["review"] = {"contentKey": key, "actor": actor, "at": now()}
        if any(lo <= s["start"] and s["end"] <= hi for lo, hi in confirm):
            s["review"] = {"contentKey": key, "actor": actor, "at": now()}
    return result


def gaps(doc: dict, count: int) -> list[list[int]]:
    result, stop = [], 0
    for s in sorted(doc["segments"], key=lambda s: s["start"]):
        if s["start"] > stop:
            result.append([stop, s["start"]])
        stop = s["end"]
    if stop < count:
        result.append([stop, count])
    return result


def reviewed(doc: dict, s: dict) -> bool:
    return s.get("review", {}).get("contentKey") == span_key(doc, s)


def completion(doc: dict, count: int) -> dict:
    missing = []
    blank = gaps(doc, count)
    if blank:
        missing.append("仍有未标注空白")
    pending = [s["id"] for s in doc["segments"] if not reviewed(doc, s)]
    if pending:
        missing.append("仍有待人工核对片段")
    active = {s.get("attemptId") for s in doc["segments"]}
    if any(a["id"] in active and not a.get("outcome") for a in doc["attempts"]):
        missing.append("尝试结果未填写")
    if any(
        s["label"] == "OUT_OF_TEMPLATE" and not s.get("description", "").strip()
        for s in doc["segments"]
    ):
        missing.append("模板外活动缺少描述")
    return {
        "eligible": not missing,
        "missing": missing,
        "gaps": blank,
        "pending": pending,
    }


def count_attempts(doc: dict, subtask: str, count: int) -> dict:
    active = {s.get("attemptId") for s in doc["segments"]}
    recorded = len(
        {
            a["id"]
            for a in doc["attempts"]
            if a["id"] in active
            and a.get("subtaskId") == subtask
            and not a.get("archived")
        }
    )
    evidence = doc.get("countEvidence", {}).get(subtask, {})
    explicit = bool(evidence.get("reason", "").strip()) and evidence.get(
        "contentKey"
    ) == content_key(doc)
    sufficient = not gaps(doc, count) and all(
        reviewed(doc, s) and (s["label"] != "UNKNOWN" or s.get("attemptId"))
        for s in doc["segments"]
    )
    return {
        "recorded_count": recorded,
        "complete": explicit or sufficient,
        "basis": evidence["reason"]
        if explicit
        else "全片核对，未知区间均明确关联"
        if sufficient
        else "标注或人工计数依据不足",
    }


def formal_segments(doc: dict) -> list[dict]:
    if doc.get("version") == 1:
        doc = migrate_document(doc)
    names = {s["id"]: s["name"] for s in doc["templateSnapshot"]["subtasks"]}
    return [
        {
            "start_frame": s["start"],
            "end_frame": s["end"],
            "text": names[s["subtaskId"]],
        }
        for s in sorted(doc["segments"], key=lambda s: s["start"])
        if s["label"] == "SUBTASK"
    ]
