"""Episode-isolated annotation heads, audit history, candidates and deliveries.

Only the Server writes annotations. Dataset paths come from its registry. No
operation in this module modifies the source dataset or dispatches model calls.
"""

from __future__ import annotations

import contextlib
import fcntl
from copy import deepcopy
from typing import Literal

from pydantic import Field, StrictInt

from pipeline.shared.core import DomainError, digest, now

from .annotation_contract import (
    AnnotationDocument,
    completion,
    content_key,
    count_attempts,
    formal_segments,
    migrate_document,
    reconcile_review,
    reviewed,
    span_key,
    template_key,
    validate_annotation,
)
from .merges import source_snapshot
from .recycle import dataset_lock
from .subtask_review import SubtaskReview
from .task_annotations import AnnotationModel


class SaveAnnotation(AnnotationModel):
    revision: StrictInt = Field(ge=0)
    source_signature: str = Field(min_length=64, max_length=64)
    document: AnnotationDocument
    confirm: list[tuple[StrictInt, StrictInt]] = Field(
        default_factory=list, max_length=10000
    )
    complete: bool = False
    count_evidence: dict[str, str] = Field(default_factory=dict)
    operation: str = Field(default="edit", max_length=400)
    # Explicitly resolved snapshot change, with every old ID mapped by the user.
    template_mapping: dict[str, str] | None = None


class Selection(AnnotationModel):
    source_signature: str = Field(min_length=64, max_length=64)
    episodes: list[StrictInt] = Field(min_length=1, max_length=100000)
    revisions: dict[str, StrictInt]
    filter: dict = Field(default_factory=dict)
    origins: dict[str, str] = Field(default_factory=dict)
    name: str | None = Field(default=None, max_length=2000)


class BatchConfirm(Selection):
    pass


class ApplyCandidate(AnnotationModel):
    revision: StrictInt = Field(ge=0)
    source_signature: str = Field(min_length=64, max_length=64)
    candidate: str = Field(min_length=1, max_length=160)
    start: StrictInt = Field(ge=0)
    end: StrictInt = Field(gt=0)
    acknowledge_current_revision: bool = False
    shared_attempt_policy: Literal["require-match", "keep-existing"] = "require-match"


class ImportBundle(AnnotationModel):
    bundle: dict


def replace_range(
    current: dict,
    incoming: dict,
    start: int,
    end: int,
    candidate: str,
    shared_attempt_policy="require-match",
) -> dict:
    """Atomic local application. Imported attempts never alias shared old fields."""
    result = deepcopy(current)
    spans = []
    for s in result["segments"]:
        if s["end"] <= start or s["start"] >= end:
            spans.append(s)
            continue
        if s["start"] < start:
            spans.append({**s, "end": start})
        if s["end"] > end:
            spans.append(
                {
                    **s,
                    "id": "tail-" + digest([candidate, s["id"], end])[:32]
                    if s["start"] < start
                    else s["id"],
                    "start": end,
                }
            )
    imported = [
        s for s in incoming["segments"] if s["start"] < end and s["end"] > start
    ]
    mapping = {
        s["attemptId"]: "candidate-" + digest([candidate, s["attemptId"]])[:32]
        for s in imported
        if s.get("attemptId")
    }
    existing = {a["id"]: a for a in result["attempts"]}
    for a in incoming["attempts"]:
        if a["id"] not in mapping:
            continue
        target = mapping[a["id"]]
        old = existing.get(target)
        if old is not None:
            keys = ("subtaskId", "subtaskName", "outcome", "values", "note")
            if (
                any(old.get(k) != a.get(k) for k in keys)
                and shared_attempt_policy != "keep-existing"
            ):
                raise DomainError(
                    "同一候选尝试的共享字段已被修改；请核对全部关联片段并明确保留当前共享信息",
                    409,
                )
            old["archived"] = False
        else:
            result["attempts"].append(
                {
                    **deepcopy(a),
                    "id": target,
                    "provenance": {
                        **a.get("provenance", {}),
                        "candidate": candidate,
                        "originalAttemptId": a["id"],
                    },
                }
            )
    for i, s in enumerate(imported):
        item = {
            **deepcopy(s),
            "id": "candidate-segment-"
            + digest([candidate, s["id"], content_key(current), start, end, i])[:32],
            "start": max(start, s["start"]),
            "end": min(end, s["end"]),
            "provenance": {
                **s.get("provenance", {}),
                "candidate": candidate,
                "originalSegmentId": s["id"],
            },
        }
        if s.get("attemptId"):
            item["attemptId"] = mapping[s["attemptId"]]
        item.pop("review", None)
        spans.append(item)
    result["segments"] = sorted(spans, key=lambda s: s["start"])
    # Frame observations are local; Episode fields and shared old attempt fields stay.
    result["frames"] = [
        f for f in result["frames"] if not start <= f["frame"] < end
    ] + [
        {
            **deepcopy(f),
            "id": "candidate-frame-" + digest([candidate, f["id"], start, end])[:32],
        }
        for f in incoming["frames"]
        if start <= f["frame"] < end
    ]
    active = {s.get("attemptId") for s in result["segments"]}
    for a in result["attempts"]:
        if a["id"] not in active:
            a["archived"] = True
    result["history"].append(
        {
            "kind": "candidate-apply",
            "candidate": candidate,
            "range": [start, end],
            "preservedEpisodeValues": True,
            "importedAttempts": mapping,
        }
    )
    return result


@contextlib.contextmanager
def annotation_lock(store, key, episodes=()):
    # Readers/annotation writers coexist; source-mutating jobs use exclusive
    # dataset_lock. Serialize only heads that actually refer to the same Episode.
    with (
        dataset_lock(store, key, shared=True, wait=True),
        contextlib.ExitStack() as stack,
    ):
        directory = store.path / "annotation-locks"
        directory.mkdir(exist_ok=True)
        for episode in sorted(set(episodes)):
            handle = stack.enter_context(
                (directory / f"{key}-{episode}.lock").open("a")
            )
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield


class AnnotationWorkbench:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.legacy = SubtaskReview(settings, store)

    @staticmethod
    def key(dataset, episode):
        return digest([dataset, episode])

    def context(self, dataset):
        key, path, job = self.legacy.resolve(dataset)
        snapshot = source_snapshot(path)
        data = self.legacy._source(path, snapshot)
        return key, path, job, snapshot, data

    def _head(self, dataset, ep, path, job, snapshot, data):
        item = self.legacy._episode(data, ep)
        try:
            row = self.store.get("annotation_episode", self.key(dataset, ep))
        except DomainError as exc:
            if exc.code != 404:
                raise
            legacy = self.legacy._draft(self.legacy.resolve(dataset)[0], path, snapshot)
            document = self.legacy._document(legacy, item, data, path, job)
            document = migrate_document(document)
            if "manualAtoms" not in document:
                document["manualAtoms"] = deepcopy(
                    legacy.get("atoms", {}).get(str(ep), item.get("atoms", []))
                )
            row = {
                "id": self.key(dataset, ep),
                "dataset": dataset,
                "episode": ep,
                "revision": 0,
                "source_signature": snapshot["signature"],
                "document": validate_annotation(document, item["frames"]),
                "complete": item.get("annotation_complete", False),
            }
        if row["source_signature"] != snapshot["signature"]:
            raise DomainError("来源版本已变化：旧标注保留，禁止套用到新来源", 409)
        return deepcopy(row), item

    def _publish(self, row, doc, *, complete=False, operation="edit"):
        if row["revision"] == 0:
            self.store.put(
                "annotation_episode_history",
                f"{row['id']}-0",
                {**row, "id": f"{row['id']}-0", "episode_id": row["id"]},
            )
        row.update(
            document=doc,
            revision=row["revision"] + 1,
            complete=complete,
            updated_at=now(),
            operation=operation,
            template_signature=template_key(doc),
        )
        self.store.put(
            "annotation_episode_history",
            f"{row['id']}-{row['revision']}",
            {**row, "id": f"{row['id']}-{row['revision']}", "episode_id": row["id"]},
        )
        self.store.put("annotation_episode", row["id"], row)
        return row

    @staticmethod
    def check(row, revision, source):
        if row["revision"] != revision or row["source_signature"] != source:
            raise DomainError(
                "此 Episode 或来源已变化；本地与服务器内容均保留，请对照处理冲突", 409
            )

    def episode(self, dataset, episode):
        key, path, _ = self.legacy.resolve(dataset)
        with annotation_lock(self.store, key, [episode]):
            _, path, job, snapshot, data = self.context(dataset)
            row, item = self._head(dataset, episode, path, job, snapshot, data)
            return {
                **row,
                "frames": item["frames"],
                "fps": data["fps"],
                "status": completion(row["document"], item["frames"]),
                "template_signature": template_key(row["document"]),
            }

    def save(self, dataset, episode, body: SaveAnnotation):
        key, path, _ = self.legacy.resolve(dataset)
        with annotation_lock(self.store, key, [episode]):
            _, path, job, snapshot, data = self.context(dataset)
            row, item = self._head(dataset, episode, path, job, snapshot, data)
            self.check(row, body.revision, body.source_signature)
            doc = validate_annotation(
                body.document.model_dump(exclude_none=True), item["frames"]
            )
            old = row["document"]
            trusted_template_restore = body.operation in {
                "undo",
                "redo",
                "restore",
            } and any(
                h.get("episode_id") == row["id"]
                and content_key(h["document"]) == content_key(doc)
                for h in self.store.all("annotation_episode_history")
            )
            if template_key(doc) != template_key(old) and not trusted_template_restore:
                mapping = body.template_mapping
                used = {
                    a["subtaskId"]
                    for a in old["attempts"]
                    if a.get("subtaskId") and not a.get("archived")
                }
                if mapping is None or not used <= mapping.keys():
                    raise DomainError(
                        "模板快照不同：请查看差异并明确解决全部旧标签对应关系，可继续使用旧快照",
                        409,
                    )
                steps = {s["id"] for s in doc["templateSnapshot"]["subtasks"]}
                if any(mapping[k] not in steps for k in used):
                    raise DomainError("模板映射目标不存在", 422)
                doc["history"].append(
                    {
                        "kind": "template-upgrade",
                        "snapshot": old["templateSnapshot"],
                        "mapping": mapping,
                        "previousDocument": old,
                    }
                )
            for lo, hi in body.confirm:
                if not 0 <= lo < hi <= item["frames"]:
                    raise DomainError("人工确认范围超出 Episode", 422)
            doc = reconcile_review(old, doc, confirm=body.confirm)
            restored = None
            if body.operation in {"undo", "redo", "restore"}:
                histories = [
                    r
                    for r in self.store.all("annotation_episode_history")
                    if r.get("episode_id") == row["id"]
                ]
                # The current head also covers the first edit of an imported
                # confirmed sidecar, before its base is archived on publish.
                histories.append(row)
                submitted = {
                    s["id"]: s
                    for s in body.document.model_dump(exclude_none=True)["segments"]
                }
                for s in doc["segments"]:
                    claimed = (
                        submitted.get(s["id"], {}).get("review", {}).get("contentKey")
                    )
                    expected = span_key(doc, s)
                    if claimed != expected:
                        continue
                    for h in histories:
                        prior = h["document"]
                        old_span = next(
                            (x for x in prior["segments"] if x["id"] == s["id"]), None
                        )
                        if (
                            old_span
                            and old_span.get("review", {}).get("contentKey") == expected
                            and span_key(prior, old_span) == expected
                        ):
                            s["review"] = deepcopy(old_span["review"])
                            break
                restored = next(
                    (
                        h
                        for h in reversed(histories)
                        if content_key(h["document"]) == content_key(doc)
                    ),
                    None,
                )
            # Client cannot forge count evidence by submitting a content hash.
            doc["countEvidence"] = deepcopy(old.get("countEvidence", {}))
            if restored is not None:
                doc["countEvidence"] = deepcopy(
                    restored["document"].get("countEvidence", {})
                )
            for sid, reason in body.count_evidence.items():
                if (
                    sid not in {s["id"] for s in doc["templateSnapshot"]["subtasks"]}
                    or not reason.strip()
                ):
                    raise DomainError("请为模板内的目标 Subtask 填写明确计数依据", 422)
                doc["countEvidence"][sid] = {
                    "reason": reason.strip(),
                    "contentKey": content_key(doc),
                }
            status = completion(doc, item["frames"])
            if body.complete and not status["eligible"]:
                raise DomainError("不能声明完成：" + "；".join(status["missing"]), 422)
            # Metadata-only autosave need not clear an unchanged completed head.
            done = (
                body.complete
                or bool(restored and restored.get("complete") and status["eligible"])
                or (
                    row.get("complete", False)
                    and content_key(old) == content_key(doc)
                    and status["eligible"]
                )
            )
            self._publish(row, doc, complete=done, operation=body.operation)
            return {**row, "status": status, "saved": True}

    def listing(
        self, dataset, subtask=None, count=None, recorded=False, template_signature=None
    ):
        key, path, _ = self.legacy.resolve(dataset)
        with annotation_lock(self.store, key):
            _, path, job, snapshot, data = self.context(dataset)
            rows = []
            for item in data["episodes"]:
                ep = item["episode_index"]
                row, _ = self._head(dataset, ep, path, job, snapshot, data)
                doc = row["document"]
                valid_target = (
                    template_signature is None
                    or template_key(doc) == template_signature
                ) and subtask in {s["id"] for s in doc["templateSnapshot"]["subtasks"]}
                number = (
                    count_attempts(doc, subtask, item["frames"])
                    if valid_target
                    else {
                        "recorded_count": None,
                        "complete": item.get("annotation_complete", False),
                        "basis": "未选择目标标签或该 Episode 使用不同模板",
                    }
                )
                if count is not None and (
                    not valid_target
                    or number["recorded_count"] != count
                    or (not recorded and not number["complete"])
                ):
                    continue
                rows.append(
                    {
                        "episode": ep,
                        "revision": row["revision"],
                        "frames": item["frames"],
                        "complete": row["complete"],
                        "status": completion(doc, item["frames"]),
                        "templateId": doc["templateId"],
                        "templateSignature": template_key(doc),
                        "counts": number,
                    }
                )
            return {
                "dataset": dataset,
                "name": job.get("dataset_name") or dataset,
                "source_signature": snapshot["signature"],
                "episodes": rows,
                "filter": {
                    "subtask": subtask,
                    "count": count,
                    "basis": "recorded" if recorded else "complete",
                },
            }

    def _selection(self, dataset, body):
        _, path, job, snapshot, data = self.context(dataset)
        if snapshot["signature"] != body.source_signature:
            raise DomainError("来源已变化，请重新核对导出清单", 409)
        episodes = []
        for ep in sorted(set(body.episodes)):
            row, item = self._head(dataset, ep, path, job, snapshot, data)
            self.check(row, body.revisions.get(str(ep), -1), body.source_signature)
            episodes.append((row, item))
        if not episodes:
            raise DomainError("导出清单为空，不能自动改成全量", 422)
        return path, job, snapshot, data, episodes

    def bundle(self, dataset, body: Selection):
        key, _, _ = self.legacy.resolve(dataset)
        with annotation_lock(self.store, key, body.episodes):
            _, job, snapshot, data, selected = self._selection(dataset, body)
            return {
                "format": "bodypipeline-annotations",
                "version": 2,
                "mode": "template",
                "created_at": now(),
                "source": {
                    "dataset": dataset,
                    "name": job.get("dataset_name"),
                    "signature": snapshot["signature"],
                },
                "frameBasis": {
                    "kind": "data-frame",
                    "interval": "half-open",
                    "ui": "inclusive-first-last",
                    "fps": data["fps"],
                },
                "selection": {
                    "episodes": [r["episode"] for r, _ in selected],
                    "filter": body.filter,
                    "origins": body.origins,
                },
                "episodes": [
                    {
                        "episode": r["episode"],
                        "revision": r["revision"],
                        "frames": i["frames"],
                        "complete": r["complete"],
                        "status": completion(r["document"], i["frames"]),
                        "document": r["document"],
                    }
                    for r, i in selected
                ],
            }

    def batch_confirm(self, dataset, body: BatchConfirm):
        key, _, _ = self.legacy.resolve(dataset)
        with annotation_lock(self.store, key, body.episodes):
            _, _, _, _, selected = self._selection(dataset, body)
            pending = []
            for row, item in selected:
                doc = row["document"]
                confirmed = reconcile_review(doc, doc, confirm=[(0, item["frames"])])
                status = completion(confirmed, item["frames"])
                if not status["eligible"] or any(
                    (s["label"] in {"UNKNOWN", "OUT_OF_TEMPLATE"} or s.get("risks"))
                    and not reviewed(doc, s)
                    for s in doc["segments"]
                ):
                    raise DomainError(
                        f"Episode {row['episode']} 有空白、缺失信息或待单独检查的特殊区间，请逐条核对",
                        422,
                    )
                pending.append((row, confirmed))
            for row, doc in pending:
                self._publish(row, doc, complete=True, operation="batch-confirm")
            return {"confirmed": [r["episode"] for r, _ in pending]}

    def candidates(self, dataset, episode):
        self.episode(dataset, episode)
        return [
            r
            for r in self.store.all("annotation_candidate")
            if r["dataset"] == dataset and r["episode"] == episode
        ]

    def add_candidate(
        self,
        dataset,
        episode,
        document,
        *,
        run,
        source_signature,
        base_revision,
        provenance=None,
    ):
        head = self.episode(dataset, episode)
        if source_signature != head["source_signature"]:
            raise DomainError("AI 候选的来源身份不匹配", 409)
        doc = validate_annotation(document, head["frames"])
        doc = reconcile_review(None, doc)
        cid = digest([dataset, episode, run, doc, base_revision])
        record = {
            "id": cid,
            "dataset": dataset,
            "episode": episode,
            "source_signature": source_signature,
            "base_revision": base_revision,
            "template_signature": template_key(doc),
            "document": doc,
            "run": run,
            "provenance": provenance or {},
            "created_at": now(),
        }
        self.store.put("annotation_candidate", cid, record)
        return record

    def apply(self, dataset, episode, body: ApplyCandidate):
        key, _, _ = self.legacy.resolve(dataset)
        with annotation_lock(self.store, key, [episode]):
            _, path, job, snapshot, data = self.context(dataset)
            row, item = self._head(dataset, episode, path, job, snapshot, data)
            self.check(row, body.revision, body.source_signature)
            c = self.store.get("annotation_candidate", body.candidate)
            if (
                c["dataset"] != dataset
                or c["episode"] != episode
                or c["source_signature"] != body.source_signature
            ):
                raise DomainError("候选来源身份或 Episode 不匹配", 409)
            if (
                c["base_revision"] != body.revision
                and not body.acknowledge_current_revision
            ):
                raise DomainError(
                    "候选生成后 Episode 已修改，请明确核对当前修订后再采用", 409
                )
            if c["template_signature"] != template_key(c["document"]) or c[
                "template_signature"
            ] != template_key(row["document"]):
                raise DomainError(
                    "模板快照不同，先显式升级或映射；候选及当前草稿均保留", 409
                )
            if not 0 <= body.start < body.end <= item["frames"]:
                raise DomainError("候选应用范围无效", 422)
            doc = replace_range(
                row["document"],
                c["document"],
                body.start,
                body.end,
                c["id"],
                body.shared_attempt_policy,
            )
            doc = reconcile_review(
                row["document"], validate_annotation(doc, item["frames"])
            )
            self._publish(row, doc, operation="candidate-apply")
            return {**row, "status": completion(doc, item["frames"])}

    def history(self, dataset, episode):
        head = self.episode(dataset, episode)
        return [
            r
            for r in self.store.all("annotation_episode_history")
            if r["episode_id"] == head["id"]
        ]

    def export_plan(self, dataset, body: Selection):
        bundle = self.bundle(dataset, body)
        _, path, job, snapshot, data = self.context(dataset)
        vocab, episodes = {}, []
        for item in bundle["episodes"]:
            spans = []
            for s in formal_segments(item["document"]):
                spans.append(
                    [
                        s["start_frame"],
                        s["end_frame"],
                        vocab.setdefault(s["text"], len(vocab)),
                    ]
                )
            episodes.append(
                {
                    "episode_index": item["episode"],
                    "frames": item["frames"],
                    "segments": spans,
                    "task_document": item["document"],
                    "atoms": item["document"].get("manualAtoms", []),
                    "annotation_revision": item["revision"],
                    "annotation_status": item["status"],
                    "annotation_complete": item["complete"],
                }
            )
        total = sum(e["frames"] for e in episodes)
        covered = sum(s[1] - s[0] for e in episodes for s in e["segments"])
        plan = {
            "dataset": {
                "format": __import__("json").loads(
                    (path / "meta/info.json").read_text()
                )["codebase_version"],
                "total_episodes": len(episodes),
                "robot_type": job.get("robot_type"),
            },
            "episodes": episodes,
            "vocabulary": [
                {"subtask": t, "subtask_index": i} for t, i in vocab.items()
            ],
            "unannotated_index": -1,
            "unannotated_text": "未标注 / Unannotated",
            "skipped": [],
            "coverage": {
                "annotated_episodes": sum(bool(e["segments"]) for e in episodes),
                "dataset_episodes": len(episodes),
                "frames": total,
                "labelled_frames": covered,
                "ratio": covered / total if total else 0,
                "labelled_ratio": sum(bool(e["segments"]) for e in episodes)
                / len(episodes),
            },
            "selection": bundle["selection"],
            "annotation_bundle": bundle,
            "review": {
                "source_signature": snapshot["signature"],
                "revisions": body.revisions,
            },
            "subset": True,
        }
        plan["digest"] = digest(plan)
        return job, path, snapshot, plan
