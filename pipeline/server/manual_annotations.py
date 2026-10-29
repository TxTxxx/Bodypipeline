"""Canonical server-owned annotations, with legacy workbench compatibility."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import uuid
from pathlib import Path

from pydantic import ValidationError

from pipeline.shared.core import DomainError, atomic_json, canonical, now

from .annotation_schemes import (
    SchemeService,
    compatible,
    fail,
    validate_scheme,
    validate_values,
)
from .datasets import dataset_for_job
from .merges import source_snapshot
from .recycle import dataset_lock
from .storage import data_directory

STATE_KIND = "manual_annotation"
SIDECAR = "meta/bodypipeline-custom-annotations.json"


def workspace_key(path):
    return hashlib.sha256(str(path).encode()).hexdigest()


def _document(head, uid):
    return json.loads((Path(head["storage_dir"]) / head["documents"][uid]).read_text())


def _publish(store, head, changes=None):
    """Write records and immutable manifest before exposing one atomic head."""
    head = copy.deepcopy(head)
    directory = Path(head["storage_dir"])
    for uid, record in (changes or {}).items():
        relative = f"episodes/{uid}/{head['revision']}.json"
        atomic_json(directory / relative, record)
        head["documents"][uid] = relative
    head["updated_at"] = now()
    atomic_json(directory / f"revisions/{head['revision']}.json", head)
    store.put(STATE_KIND, head["id"], head)
    return head


def remap_workspace(store, path, mapping, operation_id):
    """Called under the dataset lock after publication, before deleting old data."""
    key = workspace_key(path)
    try:
        head = store.get(STATE_KIND, key)
    except DomainError as exc:
        if exc.code == 404:
            try:
                legacy = store.get("subtask_draft", key)
            except DomainError as missing:
                if missing.code == 404:
                    return
                raise
            if operation_id in legacy.get("applied_operations", []):
                return
            for field in ("episodes", "atoms"):
                legacy[field] = {
                    str(mapping[index]): value
                    for index, value in legacy.get(field, {}).items()
                    if index in mapping
                }
            legacy["revision"] += 1
            legacy["source_signature"] = source_snapshot(Path(path))["signature"]
            legacy.setdefault("applied_operations", []).append(operation_id)
            history_id = f"{key}-{legacy['revision']}"
            store.put(
                "subtask_draft_history",
                history_id,
                {**legacy, "id": history_id, "draft_id": key},
            )
            store.put("subtask_draft", key, legacy)
            return
        raise
    if operation_id in head.get("applied_operations", []):
        return
    changes = {}
    kept, removed = {}, {}
    for uid, index in head["episodes"].items():
        if str(index) in mapping:
            kept[uid] = int(mapping[str(index)])
            record = _document(head, uid)
            record["episode_index"] = kept[uid]
            changes[uid] = record
        else:
            removed[uid] = index
    head.setdefault("archived_episodes", {}).update(removed)
    head["episodes"] = kept
    head["revision"] += 1
    head["source_signature"] = source_snapshot(Path(path))["signature"]
    head.setdefault("applied_operations", []).append(operation_id)
    _publish(store, head, changes)


def archive_workspace(store, path, operation_id):
    key = workspace_key(path)
    try:
        head = store.get(STATE_KIND, key)
    except DomainError as exc:
        if exc.code == 404:
            return
        raise
    if operation_id in head.get("applied_operations", []):
        return
    head.update(archived=True, revision=head["revision"] + 1)
    head.setdefault("applied_operations", []).append(operation_id)
    _publish(store, head)


class ManualAnnotations:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.schemes = SchemeService(settings, store)

    def resolve(self, dataset_id):
        job = self.store.get("job", dataset_id)
        path = dataset_for_job(self.settings, job)
        return workspace_key(path), path, job

    def exists(self, dataset_id):
        key, _, _ = self.resolve(dataset_id)
        try:
            self.store.get(STATE_KIND, key)
            return True
        except DomainError as exc:
            if exc.code == 404:
                return False
            raise

    def _source(self, path, snapshot):
        from .subtask_review import SubtaskReview

        return SubtaskReview(self.settings, self.store)._source(path, snapshot)

    def _ensure(self, dataset_id):
        key, path, job = self.resolve(dataset_id)
        try:
            recycle = self.store.get("recycle", key)
        except DomainError as exc:
            if exc.code != 404:
                raise
        else:
            from .recycle import RecycleBin

            recycle = RecycleBin(self.settings, self.store, None)._recover(recycle)
            if (recycle.get("operation") or {}).get("state") in {"pending", "running"}:
                raise DomainError("数据集正在永久清理，请稍后重试", 409)
        signature = source_snapshot(path)["signature"]
        try:
            head = self.store.get(STATE_KIND, key)
        except DomainError as exc:
            if exc.code != 404:
                raise
            return self._initialize(key, path, job, signature)
        if head.get("archived"):
            raise DomainError("数据集标注已归档", 409)
        if head["source_signature"] != signature:
            raise DomainError("来源数据已变化，不能自动套用旧标注", 409)
        return head, path

    def _initialize(self, key, path, job, signature):
        data = self._source(
            path, {"signature": signature, "bytes": source_snapshot(path)["bytes"]}
        )
        legacy = {}
        try:
            legacy = self.store.get("subtask_draft", key)
        except DomainError as exc:
            if exc.code != 404:
                raise
        if legacy and legacy["source_signature"] != signature:
            raise DomainError("旧草稿来源已变化，请先核对历史草稿", 409)
        imported = {}
        if (path / SIDECAR).exists():
            try:
                imported = json.loads((path / SIDECAR).read_text())
                if not isinstance(imported.get("episodes"), list):
                    raise ValueError("missing episodes")
                if imported.get("scheme"):
                    definition = imported["scheme"]
                    validate_scheme(
                        {"name": definition["name"], "types": definition["types"]}
                    )
            except (ValueError, KeyError, TypeError) as exc:
                raise DomainError("数据集中的自定义标注 sidecar 无效", 409) from exc
        uid = uuid.uuid4().hex
        storage = data_directory(self.settings, job, "annotations", f"manual/{uid}")
        head = {
            "id": key,
            "dataset_uid": uid,
            "dataset_path": str(path),
            "storage_dir": str(storage),
            "source_signature": signature,
            "revision": legacy.get("revision", 0),
            "scheme": imported.get("scheme"),
            "episodes": {},
            "documents": {},
            "archived": False,
            "created_at": now(),
            "applied_operations": [],
        }
        if imported.get("dataset_uid"):
            head["origin_dataset_uid"] = imported["dataset_uid"]
        sources = {e["episode_index"]: e for e in imported.get("episodes", [])}
        if len(sources) != len(imported.get("episodes", [])) or set(sources) - {
            e["episode_index"] for e in data["episodes"]
        }:
            raise DomainError("Sidecar Episode 映射无效", 409)
        changes = {}
        for item in data["episodes"]:
            eid = uuid.uuid4().hex
            index = item["episode_index"]
            timestamps = self._timestamps(item)
            source = sources.get(index, {})
            if source and (
                source.get("frames") != item["frames"]
                or source.get("frame_timestamps") != timestamps
            ):
                raise DomainError("Sidecar 帧时间映射与数据集不一致", 409)
            head["episodes"][eid] = index
            record = {
                "episode_uid": eid,
                "episode_index": index,
                "fps": data["fps"],
                "frames": item["frames"],
                "frame_timestamps": timestamps,
                "cameras": data.get("cameras", []),
                "segments": copy.deepcopy(
                    legacy.get("episodes", {}).get(
                        str(index), source.get("segments", item.get("segments", []))
                    )
                ),
                "atoms": copy.deepcopy(
                    legacy.get("atoms", {}).get(
                        str(index), source.get("atoms", item.get("atoms", []))
                    )
                ),
                "values": copy.deepcopy(source.get("values", {})),
                "records": copy.deepcopy(source.get("records", [])),
                "status": source.get(
                    "status",
                    "draft"
                    if str(index) in legacy.get("episodes", {})
                    else "not_started",
                ),
            }
            if source.get("episode_uid"):
                record["origin_episode_uid"] = source["episode_uid"]
            for segment in record["segments"]:
                segment.setdefault("id", uuid.uuid4().hex)
            self._validate_record(
                head, record, complete=record["status"] == "completed"
            )
            changes[eid] = record
        if source_snapshot(path)["signature"] != signature:
            raise DomainError("读取期间来源变化，请重新加载", 409)
        return _publish(self.store, head, changes), path

    @staticmethod
    def _timestamps(item):
        timestamps = item.get("frame_timestamps")
        if (
            not isinstance(timestamps, list)
            or len(timestamps) != item["frames"]
            or any(
                type(t) not in (float, int) or not math.isfinite(t) or t < 0
                for t in timestamps
            )
            or any(a >= b for a, b in zip(timestamps, timestamps[1:]))
        ):
            raise DomainError("缺少有效的实际帧时间戳，不能使用 FPS 猜测标注时间", 409)
        return timestamps

    def _locked(self, dataset_id):
        key, _, _ = self.resolve(dataset_id)
        return dataset_lock(self.store, key)

    @staticmethod
    def _check(head, body):
        if (
            type(body.get("revision")) is not int
            or body["revision"] != head["revision"]
            or body.get("source_signature") != head["source_signature"]
        ):
            raise DomainError(
                "标注版本或来源已变化，请重新加载（不会覆盖其他窗口）", 409
            )

    def _summary(self, head):
        return {
            "dataset_uid": head["dataset_uid"],
            "revision": head["revision"],
            "source_signature": head["source_signature"],
            "scheme": head["scheme"],
            "episodes": [
                {
                    "episode_uid": uid,
                    "episode_index": index,
                    "status": _document(head, uid)["status"],
                }
                for uid, index in sorted(head["episodes"].items(), key=lambda x: x[1])
            ],
        }

    def summary(self, dataset_id):
        with self._locked(dataset_id):
            head, _ = self._ensure(dataset_id)
            return self._summary(head)

    def bind(self, dataset_id, body):
        if set(body) - {"scheme_id", "version", "revision", "source_signature"}:
            fail("方案绑定含未知字段")
        with self._locked(dataset_id):
            head, _ = self._ensure(dataset_id)
            self._check(head, body)
            scheme = self.schemes.get(body.get("scheme_id"), body.get("version"))
            if scheme["archived"]:
                raise DomainError("不能应用已停用的方案", 409)
            if head["scheme"] and not compatible(head["scheme"], scheme):
                raise DomainError("已有标注不支持破坏性方案升级，请保留原方案", 409)
            head["scheme"] = scheme
            head["revision"] += 1
            _publish(self.store, head)
            return self._summary(head)

    def _episode(self, head, uid):
        if uid not in head["episodes"]:
            raise DomainError("Episode 不存在", 404)
        result = _document(head, uid)
        result["episode_index"] = head["episodes"][uid]
        return {
            **result,
            "dataset_uid": head["dataset_uid"],
            "revision": head["revision"],
            "source_signature": head["source_signature"],
            "scheme": head["scheme"],
        }

    def episode(self, dataset_id, episode_uid):
        with self._locked(dataset_id):
            head, _ = self._ensure(dataset_id)
            return self._episode(head, episode_uid)

    def _validate_record(self, head, record, complete=False):
        from .subtask_review import ManualAtom, Segment, validate_segments

        if not isinstance(record["values"], dict):
            fail("Episode 属性必须为对象")
        if any(
            not isinstance(record[k], list) for k in ("segments", "atoms", "records")
        ):
            fail("语言、区间和事件标注必须为列表")
        if any(not isinstance(segment, dict) for segment in record["segments"]):
            fail("Subtask 条目必须为对象")
        try:
            segments = []
            identifiers = set()
            for segment in record["segments"]:
                parsed = Segment.model_validate(
                    {k: v for k, v in segment.items() if k not in {"id", "values"}}
                ).model_dump()
                sid = segment.get("id") or uuid.uuid4().hex
                self._uuid(sid)
                if sid in identifiers:
                    fail("Subtask ID 重复")
                identifiers.add(sid)
                segments.append(
                    {
                        **parsed,
                        "id": sid,
                        **(
                            {"values": segment["values"]} if "values" in segment else {}
                        ),
                    }
                )
            validate_segments(segments, record["frames"])
            atoms = [
                ManualAtom.model_validate(atom).model_dump() for atom in record["atoms"]
            ]
        except ValidationError as exc:
            fail("语言标注或 Subtask 格式无效：" + str(exc))
        timestamps = record["frame_timestamps"]
        for atom in atoms:
            if atom["timestamp"] > timestamps[-1] or atom["timestamp"] < timestamps[0]:
                fail("标注时间戳必须位于实际帧时间范围内")
            if (
                atom.get("camera") is not None
                and atom["camera"] not in record["cameras"]
            ):
                fail("标注相机不存在")
            if atom["style"] in {"vqa", "interjection", None}:
                atom["timestamp"] = min(
                    timestamps, key=lambda t: abs(t - atom["timestamp"])
                )
        record["segments"], record["atoms"] = segments, atoms
        if (
            not isinstance(record["records"], list)
            or len(record["records"]) > 10000
            or len(segments) > 2000
            or len(atoms) > 10000
        ):
            fail("单个 Episode 标注条目过多")
        scheme = head["scheme"]
        if not scheme:
            if (
                record["values"]
                or record["records"]
                or any(s.get("values") for s in segments)
            ):
                fail("请先绑定标注方案")
            return
        types = {t["id"]: t for t in scheme["types"]}
        ep_types = {tid: t for tid, t in types.items() if t["scope"] == "episode"}
        if (
            not isinstance(record["values"], dict)
            or set(record["values"]) - ep_types.keys()
        ):
            fail("Episode 属性含未知类型")
        for tid, tdef in ep_types.items():
            validate_values(tdef["fields"], record["values"].get(tid, {}), complete)
        spans = {}
        for entry in record["records"]:
            if not isinstance(entry, dict) or set(entry) - {
                "id",
                "type_id",
                "start_frame",
                "end_frame",
                "frame_index",
                "camera",
                "values",
            }:
                fail("自定义记录格式无效")
            if not isinstance(entry.get("type_id"), str):
                fail("自定义记录类型无效")
            tdef = types.get(entry.get("type_id"))
            if not tdef or tdef["scope"] == "episode":
                fail("自定义记录类型无效")
            rid = entry.setdefault("id", uuid.uuid4().hex)
            self._uuid(rid)
            if rid in identifiers:
                fail("标注 ID 重复")
            identifiers.add(rid)
            if (
                entry.get("camera") is not None
                and entry["camera"] not in record["cameras"]
            ):
                fail("标注相机不存在")
            if tdef.get("camera_bound") and not entry.get("camera"):
                fail("此类型必须指定相机")
            if tdef["scope"] == "frame":
                frame = entry.get("frame_index")
                if (
                    type(frame) is not int
                    or not 0 <= frame < record["frames"]
                    or "start_frame" in entry
                    or "end_frame" in entry
                ):
                    fail("单帧索引无效")
            else:
                start, end = entry.get("start_frame"), entry.get("end_frame")
                if (
                    type(start) is not int
                    or type(end) is not int
                    or not 0 <= start < end <= record["frames"]
                    or "frame_index" in entry
                ):
                    fail("区间须满足 0 ≤ 开始帧 < 结束帧 ≤ 总帧数")
                spans.setdefault(tdef["id"], []).append((start, end))
            validate_values(tdef["fields"], entry.get("values", {}), complete)
        for tid, ranges in spans.items():
            ordered = sorted(ranges)
            if not types[tid]["allow_overlap"] and any(
                left[1] > right[0] for left, right in zip(ordered, ordered[1:])
            ):
                fail("此类型不允许区间重叠")
        subtask_type = types.get("subtask")
        for segment in segments:
            if "values" in segment or subtask_type:
                if not subtask_type or subtask_type["scope"] != "interval":
                    fail("Subtask 字段须定义 interval 范围的 subtask 类型")
                validate_values(
                    subtask_type["fields"], segment.get("values", {}), complete
                )

    @staticmethod
    def _uuid(value):
        try:
            if not isinstance(value, str):
                raise ValueError()
            uuid.UUID(value)
        except ValueError:
            fail("标注条目必须使用有效 UUID")

    def _save(self, head, uid, body):
        if set(body) - {
            "revision",
            "source_signature",
            "segments",
            "atoms",
            "values",
            "records",
            "status",
        }:
            fail("保存请求含未知字段")
        self._check(head, body)
        record = _document(head, uid) if uid in head["episodes"] else None
        if record is None:
            raise DomainError("Episode 不存在", 404)
        for key in ("segments", "atoms", "values", "records"):
            if key in body and body[key] is not None:
                record[key] = copy.deepcopy(body[key])
        status = body.get("status", "draft")
        if not isinstance(status, str) or status not in {"draft", "completed"}:
            fail("标注状态无效")
        record["status"] = status
        try:
            if len(canonical(record)) > 4 * 1024 * 1024:
                fail("单个 Episode 标注不能超过 4 MiB")
        except (TypeError, ValueError):
            fail("标注含非法 JSON 值或非有限数值")
        self._validate_record(head, record, complete=status == "completed")
        head["revision"] += 1
        head = _publish(self.store, head, {uid: record})
        return self._episode(head, uid)

    def save(self, dataset_id, episode_uid, body):
        with self._locked(dataset_id):
            head, _ = self._ensure(dataset_id)
            return self._save(head, episode_uid, body)

    @staticmethod
    def _uid(head, index):
        for uid, current in head["episodes"].items():
            if current == index:
                return uid
        raise DomainError("Episode 不存在", 404)

    def legacy_episode(self, dataset_id, index):
        with self._locked(dataset_id):
            return self.legacy_episode_locked(dataset_id, index)

    def legacy_episode_locked(self, dataset_id, index):
        head, _ = self._ensure(dataset_id)
        record = self._episode(head, self._uid(head, index))
        return self._legacy(record)

    @staticmethod
    def _legacy(record):
        return {
            "episode_index": record["episode_index"],
            "fps": record["fps"],
            "frames": record["frames"],
            "frame_timestamps": record["frame_timestamps"],
            "revision": record["revision"],
            "source_signature": record["source_signature"],
            "has_draft": record["status"] != "not_started",
            "segments": [
                {k: s[k] for k in ("start_frame", "end_frame", "text")}
                for s in record["segments"]
            ],
            "atoms": record["atoms"],
        }

    def legacy_save(self, dataset_id, index, body):
        if hasattr(body, "model_dump"):
            body = body.model_dump(exclude_none=True)
        with self._locked(dataset_id):
            return self.legacy_save_locked(dataset_id, index, body)

    def legacy_save_locked(self, dataset_id, index, body):
        if hasattr(body, "model_dump"):
            body = body.model_dump(exclude_none=True)
        head, _ = self._ensure(dataset_id)
        uid = self._uid(head, index)
        record = _document(head, uid)
        if "segments" in body:
            old = {
                (s["start_frame"], s["end_frame"], s["text"]): s
                for s in record["segments"]
            }
            rewritten = []
            used = set()
            for segment in body["segments"]:
                key = (
                    segment["start_frame"],
                    segment["end_frame"],
                    segment["text"],
                )
                existing = old.get(key)
                if existing:
                    used.add(existing["id"])
                rewritten.append(
                    {
                        **segment,
                        **(
                            {
                                "id": existing["id"],
                                **(
                                    {"values": existing["values"]}
                                    if "values" in existing
                                    else {}
                                ),
                            }
                            if existing
                            else {}
                        ),
                    }
                )
            if any(s.get("values") and s["id"] not in used for s in record["segments"]):
                raise DomainError(
                    "旧客户端无法关联带自定义字段的 Subtask，请使用新版编辑器", 409
                )
            body = {**body, "segments": rewritten}
        saved = self._save(head, uid, body)
        return {
            "revision": saved["revision"],
            "segments": self._legacy(saved)["segments"],
            "saved": True,
        }

    def legacy_state(self, dataset_id):
        """Caller already owns dataset_lock, e.g. legacy revision export."""
        head, _ = self._ensure(dataset_id)
        episodes, atoms = {}, {}
        for uid, index in head["episodes"].items():
            record = self._legacy(self._episode(head, uid))
            episodes[str(index)], atoms[str(index)] = (
                record["segments"],
                record["atoms"],
            )
        return {
            "id": head["id"],
            "revision": head["revision"],
            "source_signature": head["source_signature"],
            "episodes": episodes,
            "atoms": atoms,
        }

    def _filtered(self, head, filters):
        if not isinstance(filters, dict) or set(filters) - {
            "status",
            "type_id",
            "field_id",
            "field",
            "value",
            "values",
        }:
            fail("筛选条件含未知字段")
        if filters.get("status") and (
            not isinstance(filters["status"], str)
            or filters["status"] not in {"not_started", "draft", "completed"}
        ):
            fail("筛选状态无效")
        types = {t["id"]: t for t in (head.get("scheme") or {}).get("types", [])}
        if (
            filters.get("type_id")
            and filters["type_id"] != "subtask"
            and filters["type_id"] not in types
        ):
            fail("筛选类型不存在")
        result = []
        trashed = set()
        try:
            recycle = self.store.get("recycle", head["id"])
            trashed = {int(i) for i in recycle.get("entries", {})}
        except DomainError as exc:
            if exc.code != 404:
                raise
        values_filter = filters.get("values", {})
        if isinstance(values_filter, str):
            try:
                values_filter = json.loads(values_filter)
            except ValueError:
                fail("属性筛选无效")
        field_id = filters.get("field_id", filters.get("field"))
        if field_id:
            if (
                not isinstance(field_id, str)
                or field_id.count(".") != 1
                or "value" not in filters
            ):
                fail("属性筛选须指定类型、字段与值")
            tid, fid = field_id.split(".")
            tdef = types.get(tid)
            field = (
                next((f for f in tdef["fields"] if f["id"] == fid), None)
                if tdef
                else None
            )
            if (
                not field
                or tdef["scope"] != "episode"
                or field["dtype"] not in {"single", "multi", "bool"}
            ):
                fail("属性筛选字段不存在或不支持")
            value = filters["value"]
            if field["dtype"] == "bool":
                if value not in ("true", "false", True, False):
                    fail("布尔筛选值无效")
                value = value in ("true", True)
            elif field["dtype"] == "multi":
                value = [value] if isinstance(value, str) else value
            validate_values([field], {fid: value})
            values_filter = {
                **values_filter,
                tid: {**values_filter.get(tid, {}), fid: value},
            }
        for uid, index in sorted(head["episodes"].items(), key=lambda x: x[1]):
            if index in trashed:
                continue
            record = self._episode(head, uid)
            if filters.get("status") and record["status"] != filters["status"]:
                continue
            if filters.get("type_id") and not (
                (filters["type_id"] == "subtask" and record["segments"])
                or any(r["type_id"] == filters["type_id"] for r in record["records"])
            ):
                continue
            if not isinstance(values_filter, dict):
                fail("属性筛选须为对象")
            matches = True
            for tid, expected in values_filter.items():
                if not isinstance(expected, dict):
                    fail("属性筛选须按类型分组")
                for field, value in expected.items():
                    actual = record["values"].get(tid, {}).get(field)
                    if isinstance(actual, list):
                        matches &= (
                            all(v in actual for v in value)
                            if isinstance(value, list)
                            else value in actual
                        )
                    else:
                        matches &= type(actual) is type(value) and actual == value
            if matches:
                result.append(record)
        return result

    def episodes(self, dataset_id, filters=None):
        with self._locked(dataset_id):
            head, _ = self._ensure(dataset_id)
            records = self._filtered(head, filters or {})
            return {**self._summary(head), "episodes": records, "total": len(records)}

    def export_snapshot(self, dataset_id, body):
        if not isinstance(body, dict) or set(body) - {
            "revision",
            "source_signature",
            "include_drafts",
            "include_trash",
            "filters",
        }:
            fail("导出请求含未知字段")
        if (
            type(body.get("include_drafts", False)) is not bool
            or type(body.get("include_trash", False)) is not bool
            or body.get("include_trash", False)
        ):
            fail("导出选项必须为布尔值，且不支持导出回收站数据")
        if not isinstance(body.get("filters", {}), dict):
            fail("导出筛选条件必须为对象")
        with self._locked(dataset_id):
            head, path = self._ensure(dataset_id)
            if "revision" in body or "source_signature" in body:
                self._check(head, body)
            filters = dict(body.get("filters", {}))
            if "status" in filters and filters["status"] not in (
                "not_started",
                "draft",
                "completed",
            ):
                fail("筛选状态无效")
            if not body.get("include_drafts", False):
                filters["status"] = "completed"
            episodes = self._filtered(head, filters)
            return {
                "format": "bodypipeline-annotations-v1",
                "dataset_uid": head["dataset_uid"],
                "revision": head["revision"],
                "source_signature": head["source_signature"],
                "scheme": copy.deepcopy(head["scheme"]),
                "episodes": copy.deepcopy(episodes),
                "source_manifest": {
                    "source_signature_kind": "local-inode-size-mtime",
                    "dataset_uid": head["dataset_uid"],
                },
                "created_at": now(),
                "filters": filters,
                "_source_path": str(path),
            }

    def remap(self, path, mapping, operation_id):
        return remap_workspace(self.store, path, mapping, operation_id)

    def archive(self, path, operation_id):
        return archive_workspace(self.store, path, operation_id)
