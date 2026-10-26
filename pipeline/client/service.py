from __future__ import annotations

import errno
import os
import re
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pipeline.shared.collections import collection_metadata, dataset_name
from pipeline.shared.core import (
    DomainError,
    Store,
    digest,
    file_hash,
    fsync_dir,
    now,
    safe_relative,
)

from .adapters.registry import backend_for_record, get_capture_backend

BUSY = {"STARTING", "RECORDING", "STOPPING", "RECOVERY_REQUIRED"}


class CaptureService:
    def __init__(self, settings, adapter=None):
        self.settings = settings
        self.store = Store(settings.root / "state", legacy_name="client.sqlite")
        self.profile = settings.profile()
        self.backend = get_capture_backend(settings, self.profile)
        self.adapter = adapter or self.backend.adapter(settings, self.profile)
        self.lock = threading.RLock()
        self.deleting = set()
        self.roots = {
            k: Path(v).expanduser().absolute() for k, v in settings.local_roots.items()
        } or {"default": settings.root / "captures"}
        for root in self.roots.values():
            if root.resolve() != root:
                raise ValueError("本地受管根不得含符号链接")
            root.mkdir(parents=True, exist_ok=True)

    def recover(self):
        for row in self.store.all("capture"):
            row.pop("delete_pending", None)
            if row["state"] in BUSY:
                row.update(
                    state="RECOVERY_REQUIRED",
                    error="Client 重启，需确认 recorder 状态后结束；不能重发 start",
                )
                self.save(row)
            if row["state"] in {"REVIEW", "SEALING", "SEALED"}:
                # Preserve files from the former confirmation/transfer workflow.
                row["state"] = "SAVED"
                row["saved_at"] = row.get("stopped_at") or row.get("sealed_at") or now()
            if row["state"] == "SAVED":
                self._saved_path(row)
            # Existing recordings are history, not new recording-start events.
            if row.get("started_at"):
                row.setdefault("upload_trigger_checked", True)
            if row.get("upload_state") == "UPLOADING":
                row.update(upload_state="QUEUED", retry_at=0)
            self.save(row)

    def _saved_path(self, row):
        raw = Path(row["raw_root"])
        candidates = [raw / "episode0" / "episode.hdf5"]
        if row.get("package_path"):
            candidates.append(Path(row["package_path"]))
        raw_episode = raw / "episode0" / "episode.raw"
        candidates.append(raw_episode)
        source = next((path for path in candidates if path.is_file()), raw)
        if not row.get("direct_hdf5") or not source.is_file():
            row["saved_path"] = str(source)
            return
        with self.lock:
            if raw_episode.is_file():
                self._publish_episode(row, raw_episode, ".raw")
            self._publish_episode(row, source, source.suffix)

    def _publish_hdf5(self, row, source):
        self._publish_episode(row, source, ".hdf5")

    def _publish_episode(self, row, source, suffix):
        if suffix not in {".hdf5", ".raw"}:
            raise DomainError("未知采集文件扩展名")
        path_key = "published_raw_path" if suffix == ".raw" else "published_path"
        directory = self._data_path(row["data_path"], create=True)
        allocated = (
            row.get(path_key)
            or row.get("published_path")
            or row.get("published_raw_path")
        )
        if allocated:
            destination = Path(allocated).with_suffix(suffix)
            if destination.parent != directory or not re.fullmatch(
                r"episode[1-9][0-9]*\.(hdf5|raw)", destination.name
            ):
                raise DomainError("已保存采集文件不属于当前会话")
        else:
            # Include discarded recordings so their numbers are not reused.
            names = [path.name for path in directory.iterdir()]
            names.extend(
                Path(item[key]).name
                for item in self.store.all("capture")
                for key in ("published_path", "published_raw_path")
                if item.get(key) and item.get("data_path") == row["data_path"]
            )
            numbers = [
                int(match.group(1))
                for name in names
                if (match := re.fullmatch(r"episode([1-9][0-9]*)\.(?:hdf5|raw)", name))
            ]
            destination = directory / f"episode{max(numbers, default=0) + 1}{suffix}"
            # Persist the allocation before publishing, so recovery uses the same name.
            row[path_key] = str(destination)
            self.save(row)
        if source.resolve() != source or destination.resolve() != destination:
            raise DomainError("拒绝符号链接采集文件")
        if destination.exists():
            if not destination.samefile(source) and file_hash(destination) != file_hash(
                source
            ):
                raise DomainError("拒绝覆盖已有采集文件")
        else:
            try:
                os.link(source, destination)
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                temporary = directory / f".{row['id']}.{uuid.uuid4().hex}.tmp"
                try:
                    with source.open("rb") as src, temporary.open("xb") as dst:
                        shutil.copyfileobj(src, dst)
                        dst.flush()
                        os.fsync(dst.fileno())
                    os.link(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
            fsync_dir(directory)
        row.update(saved_path=str(destination), **{path_key: str(destination)})

    def save(self, row):
        row["updated_at"] = now()
        self.store.put("capture", row["id"], row)
        return row

    @staticmethod
    def _component(value, label):
        value = " ".join(str(value or "").split())
        if (
            not value
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            or "\x00" in value
        ):
            raise DomainError(f"{label}必须是单个文件夹名称", 400)
        return value

    def _capture_root(self):
        configured = self.settings.capture.get("data_root")
        if configured:
            root = Path(configured).expanduser().absolute()
        else:
            root = self.roots.get("arx-data") or next(iter(self.roots.values()))
        if root.resolve() != root:
            raise DomainError("采集数据根目录不能是符号链接", 400)
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _data_path(self, value, *, create=False, root=None):
        path = Path(str(value or "")).expanduser()
        if (
            not path.is_absolute()
            or path.resolve() != path
            or any(part in {".", ".."} for part in path.parts)
        ):
            raise DomainError(
                "数据路径必须是本机的绝对路径，且不能包含符号链接或 ..", 400
            )
        allowed = [
            Path(v).expanduser().absolute().resolve() for v in self.roots.values()
        ]
        if root is not None:
            allowed = [Path(root).expanduser().absolute().resolve()]
        if not any(path == base or path.is_relative_to(base) for base in allowed):
            raise DomainError("数据路径不在允许的采集目录内", 400)
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def create_data_path(self, task_name, collector_name):
        task = self._component(task_name, "任务名称")
        collector = self._component(collector_name, "数采员名称")
        root = self._capture_root()
        name = dataset_name(f"{task}-{collector}")
        date = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
        path = root / task / collector / date
        if path.resolve() != path:
            raise DomainError("数据路径不能包含符号链接", 400)
        path.mkdir(parents=True, exist_ok=True)
        return {
            "path": str(path),
            "task_name": task,
            "collector_name": collector,
            "dataset_name": name,
            "date": date,
        }

    @staticmethod
    def _dataset(body):
        task = dataset_name(body.get("dataset_name") or body.get("instruction"))
        collector = " ".join(str(body.get("collector_name") or "").split())
        return (
            task,
            collector,
            dataset_name(f"{task}-{collector}") if collector else task,
        )

    def browse_data_paths(self, value=""):
        root = self._capture_root()
        current = root if not value else self._data_path(value, root=root)
        current.mkdir(parents=True, exist_ok=True)
        entries = []
        for entry in sorted(current.iterdir(), key=lambda item: item.name.casefold()):
            if (
                entry.is_dir()
                and not entry.is_symlink()
                and not entry.name.startswith(".")
            ):
                entries.append({"name": entry.name, "path": str(entry)})
        return {
            "root": str(root),
            "path": str(current),
            "parent": str(current.parent) if current != root else None,
            "entries": entries,
        }

    def create(self, body, *, external_backend=None, external_profile=None):
        backend = external_backend or self.backend
        profile = external_profile or self.profile
        with self.lock:
            if external_backend is None and not self.settings.capture.get("recording_enabled", True):
                raise DomainError("当前工作台处于纯推理模式，采集已暂停", 409)
            if not backend.raw_format:
                raise DomainError("当前 Client 未启用采集后端", 503)
            key = body["idempotency_key"]
            payload_hash = digest(
                {k: v for k, v in body.items() if k != "idempotency_key"}
            )
            for row in self.store.all("capture"):
                if row["idempotency_key"] == key:
                    if row["request_hash"] != payload_hash:
                        raise DomainError("同一幂等键不能用于不同请求")
                    return row

            # Deployment configuration owns the collection mode. An older open
            # webpage may still send continuous=False after a service update.
            continuous = external_backend is None and bool(
                self.settings.capture.get("continuous", body.get("continuous", False))
            )
            if continuous and (
                backend.id != "arx_ros2"
                or self.settings.capture.get("start_trigger", "arx_buttons")
                != "arx_buttons"
            ):
                raise DomainError("当前采集后端不支持按键连续采集", 400)
            task, collector, dataset = self._dataset(body)
            data_path_value = body.get("data_path")
            if not data_path_value and collector:
                data_path_value = self.create_data_path(task, collector)["path"]
            if data_path_value:
                if not collector and external_backend is None:
                    raise DomainError("请填写数采员名称", 400)
                base = self._data_path(data_path_value, create=True)
                root_id = next(
                    (
                        name
                        for name, path in self.roots.items()
                        if base == path or base.is_relative_to(path)
                    ),
                    "arx-data",
                )
                prefix = ""
            else:
                root_id = body["local_root"]
                if root_id not in self.roots:
                    raise DomainError("未知本地存储根", 400)
                prefix = safe_relative(body.get("local_prefix", ""), True)
                base = self.roots[root_id]
                base.mkdir(parents=True, exist_ok=True)

            metadata = collection_metadata(
                {
                    "dataset_name": dataset,
                    "project_id": self.settings.project_id,
                    "profile": profile,
                    "profile_hash": digest(profile),
                    "capture_mode": self.settings.mode,
                    "raw_format": backend.raw_format,
                },
                "default",
                "",
            )
            if data_path_value:
                raw = (
                    self._capture_root()
                    / ".bodypipeline"
                    / "datasets"
                    / metadata["collection_id"]
                    / "sessions"
                    / str(uuid.uuid4())
                    / "raw"
                )
                # The session ID is the final path component and is persisted below.
                capture_id = raw.parts[-2]
            else:
                capture_id = str(uuid.uuid4())
                raw = (
                    base
                    / "datasets"
                    / metadata["collection_id"]
                    / "sessions"
                    / capture_id
                    / "raw"
                )
            backend.validate_path(raw)
            raw.parent.mkdir(parents=True, exist_ok=True)
            row = {
                **metadata,
                "id": capture_id,
                "idempotency_key": key,
                "request_hash": payload_hash,
                "state": "DRAFT",
                "created_at": now(),
                "instruction": body.get("instruction", ""),
                "dataset_name": dataset,
                "task_name": task,
                "collector_name": collector,
                "data_path": str(base),
                "direct_hdf5": bool(data_path_value),
                "local_root": root_id,
                "local_prefix": prefix,
                "raw_root": str(raw),
                "robot_id": self.settings.robot_id,
                "project_id": self.settings.project_id,
                "profile": profile,
                "profile_hash": digest(profile),
                "mode": self.settings.mode,
                "capture_backend": backend.id,
                "error": None,
                "discard_requested": False,
                "continuous": continuous,
            }
            if continuous:
                row["continuous_group_id"] = capture_id
            return self.save(row)

    def _queue_previous_upload(self, current):
        """A real recording start confirms the immediately preceding episode."""
        if not current.get("started_at") or current.get("upload_trigger_checked"):
            return
        previous = [
            row
            for row in self.store.all("capture")
            if row["id"] != current["id"]
            and row.get("started_at")
            and row.get("stopped_at")
            and row["stopped_at"] <= current["started_at"]
            and all(
                row.get(key) == current.get(key)
                for key in (
                    "robot_id",
                    "project_id",
                    "profile_hash",
                    "mode",
                    "capture_backend",
                )
            )
        ]
        if previous:
            row = max(previous, key=lambda item: (item["stopped_at"], item["id"]))
            if (
                row["state"] == "SAVED"
                and not row.get("capture_failed")
                and not row.get("discard_requested")
                and row.get("upload_state", "NONE") == "NONE"
            ):
                row.update(
                    upload_state="QUEUED",
                    upload_trigger_id=current["id"],
                    upload_requested_at=now(),
                    retry_at=0,
                    remote_target=self.settings.capture.get("upload_target", "default"),
                    remote_prefix=safe_relative(
                        self.settings.capture.get("upload_prefix", ""), True
                    ),
                )
                self._upload_dataset(row)
                self.save(row)
        current["upload_trigger_checked"] = True
        self.save(current)

    def _upload_dataset(self, row):
        # Adopt the new name when an existing local episode is first confirmed.
        # An already sealed manifest keeps its immutable transfer identity.
        if row.get("task_name") or row.get("manifest_hash"):
            return
        task, collector, name = self._dataset(row)
        row.update(task_name=task, collector_name=collector)
        row.update(
            collection_metadata(
                {
                    **row,
                    "dataset_name": name,
                    "capture_mode": row["mode"],
                    "raw_format": backend_for_record(row).raw_format,
                },
                row.get("remote_target", "default"),
                row.get("remote_prefix", ""),
            )
        )

    def describe(self, row):
        """Expose legacy captures in task groups without moving their files."""
        row = {**row, "video_download_supported": backend_for_record(row).video_source is not None}
        if row.get("collection_id"):
            return row
        metadata = collection_metadata(
            {
                "instruction": row["instruction"],
                "project_id": row["project_id"],
                "profile": row["profile"],
                "profile_hash": row["profile_hash"],
                "capture_mode": row["mode"],
                "raw_format": backend_for_record(row).raw_format,
            },
            "default",
            "",
        )
        return {**row, **metadata}

    def start(self, cid):
        with self.lock:
            if getattr(self, "control_modes", None):
                self.control_modes.require("capture")
            if not self.settings.capture.get("recording_enabled", True):
                raise DomainError("当前工作台处于纯推理模式，采集已暂停", 409)
            if getattr(self, "replay", None) and self.replay.row["state"] in {
                "PREPARING",
                "RUNNING",
                "STOPPING",
                "RECOVERY_REQUIRED",
            }:
                raise DomainError("请先结束当前回放")
            row = self.store.get("capture", cid)
            if row["state"] in {"STARTING", "RECORDING"}:
                return row
            if row["state"] != "DRAFT":
                raise DomainError("该会话不能再次启动；请创建新会话")
            if (
                row["profile_hash"] != digest(self.profile)
                or backend_for_record(row).id != self.backend.id
                or row["robot_id"] != self.settings.robot_id
                or row["project_id"] != self.settings.project_id
            ):
                raise DomainError("会话绑定与当前机器人配置不同；请创建新会话")
            if any(r["state"] in BUSY for r in self.store.all("capture")):
                raise DomainError("机器人已有采集或状态未知的会话")
            health = self.adapter.health()
            if not health.get("ready"):
                raise DomainError(f"采集前检查失败：{health}")
            raw = Path(row["raw_root"])
            if raw.resolve() != raw or (raw / "episode0").exists():
                raise DomainError("拒绝符号链接或覆盖已有采集目录")
            raw.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(raw).free < self.settings.min_free_bytes:
                raise DomainError("磁盘剩余容量不足")
            row.update(state="STARTING", start_requested_at=now(), error=None)
            self.save(row)  # Persist before the non-idempotent ROS call.
            try:
                result = (
                    self.adapter.start(
                        cid,
                        raw,
                        row.get("dataset_name") or row["instruction"],
                        **({"continuous": True} if row.get("continuous") else {}),
                    )
                    or {}
                )
                if result.get("pending") is True and result.get("started") is False:
                    row.update(start_pending=True, start_message=result.get("message"))
                else:
                    row.update(
                        state="RECORDING",
                        started_at=result.get("recording_started_at") or now(),
                    )
            except Exception as exc:
                row.update(state="RECOVERY_REQUIRED", error=str(exc))
            self.save(row)
            self._queue_previous_upload(row)
            return row

    def enable_arm(self):
        with self.lock:
            self.sync_continuous()
            active = [row for row in self.store.all("capture") if row["state"] in BUSY]
            if any(
                row["state"] != "STARTING" or not row.get("start_pending")
                for row in active
            ):
                raise DomainError("正在录制或会话状态异常，请先结束当前采集")
            result = self.adapter.enable()
            if active:
                result["capture"] = self.describe(active[0])
            return result

    def reset_arm(self):
        with self.lock:
            reset = getattr(self.adapter, "reset", None)
            if reset is None:
                raise DomainError("当前采集后端不支持机械臂复位", 400)
            result = reset()
            self.sync_continuous()
            self.sync_pending_starts()
            self.sync_recording_stops()
            return result

    def capture_destination(self):
        try:
            return self.store.get("selection", "capture")
        except DomainError as exc:
            if exc.code != 404:
                raise
            return None

    def _matches_destination(self, row, body):
        return (row["dataset_name"] == self._dataset(body)[2]
                and row.get("collector_name", "") == body.get("collector_name", "")
                and row.get("instruction", "") == body.get("instruction", "")
                and row.get("data_path") == body.get("data_path"))

    def select_destination(self, body):
        with self.lock:
            self.sync_continuous()
            path = str(self._data_path(body["data_path"]))
            body = {**body, "data_path": path}
            task, collector, _ = self._dataset(body)
            if not collector:
                raise DomainError("请填写数采员名称", 400)
            rows = self.store.all("capture")
            active = next((r for r in rows if r["state"] in BUSY), None)
            capture = None
            applied = "selected"
            if active:
                selector = getattr(self.adapter, "select_next", None)
                if selector is None:
                    raise DomainError("请结束当前采集后切换文件夹")
                if active["state"] == "RECOVERY_REQUIRED":
                    raise DomainError("请先结束异常采集，再切换文件夹")
                capture = next((r for r in rows if r.get("destination_pending")
                                and r["state"] == "DRAFT" and self._matches_destination(r, body)), None)
                if self._matches_destination(active, body):
                    capture = active
                if capture is None:
                    capture = self.create(body)
                # A draft is the next group's metadata; Bridge switches only at
                # the episode boundary, so current raw files never change roots.
                is_draft = capture["state"] == "DRAFT"
                if is_draft:
                    capture["destination_pending"] = True
                    self.save(capture)
                result = selector(capture["id"], Path(capture["raw_root"]),
                                  capture.get("dataset_name") or capture["instruction"],
                                  continuous=bool(capture.get("continuous")))
                applied = result["applied"]
                self.sync_continuous()
                for previous in self.store.all("capture"):
                    if (previous.get("destination_pending") and previous["state"] == "DRAFT"
                            and previous["id"] != capture["id"]):
                        previous.update(state="DISCARDED", destination_pending=False)
                        self.save(previous)
                capture = self.store.get("capture", capture["id"])
            destination = {"data_path": path, "task_name": task, "collector_name": collector,
                           "instruction": body.get("instruction", ""), "changed_at": now()}
            self.store.put("selection", "capture", destination)
            return {"destination": destination, "applied": applied, "capture": capture}

    def create_and_start(self, body):
        with self.lock:
            self.sync_continuous()
            for row in self.store.all("capture"):
                if row["state"] != "STARTING" or not row.get("start_pending"):
                    continue
                desired = self._dataset(body)[2]
                if (
                    row["dataset_name"] == desired
                    and row.get("instruction", "") == body.get("instruction", "")
                    and row.get("collector_name", "") == body.get("collector_name", "")
                    and (
                        not body.get("data_path")
                        or row.get("data_path") == body["data_path"]
                    )
                ):
                    return row
            waiting = next((r for r in self.store.all("capture")
                            if r["state"] == "STARTING" and r.get("start_pending")), None)
            if waiting and body.get("data_path"):
                return self.select_destination(body)["capture"]
            row = self.start(self.create(body)["id"])
            if body.get("data_path"):
                task, collector, _ = self._dataset(body)
                self.store.put("selection", "capture", {"data_path": body["data_path"],
                    "task_name": task, "collector_name": collector,
                    "instruction": body.get("instruction", ""), "changed_at": now()})
            return row

    def _sync_conversion_status(self, status):
        with self.lock:
            for item in status.get("conversion_sessions", []):
                try:
                    row = self.store.get("capture", item["session_id"])
                except DomainError:
                    continue
                if row["state"] == "DISCARDED" or row["raw_root"] != item["raw_root"]:
                    continue
                before = dict(row)
                for key in ("conversion_state", "conversion_error", "conversion_finished_at"):
                    row[key] = item.get(key)
                if item.get("raw_alignment_report"):
                    row["raw_alignment_report"] = item["raw_alignment_report"]
                if row["state"] == "SAVED" and item.get("conversion_state") == "DONE":
                    self._saved_path(row)
                if row != before:
                    self.save(row)

    def sync_continuous(self):
        """Import completed and active episodes owned by a continuous Bridge run.

        The Bridge advances immediately; Client polling only records its history.
        No start command is issued when synchronizing, including after a restart.
        """
        status = self.adapter.status()
        self._sync_conversion_status(status)
        if not any(row.get("continuous") for row in self.store.all("capture")):
            return
        sessions = status.get("continuous_sessions") or []
        groups = dict.fromkeys(item.get("continuous_group_id") for item in
                               sorted(sessions, key=lambda item: item["created_at"]))
        for group in groups:
            if group:
                self._sync_continuous_group(
                    [item for item in sessions if item.get("continuous_group_id") == group], group
                )

    def _sync_continuous_group(self, sessions, group):
        with self.lock:
            try:
                template = self.store.get("capture", group)
            except DomainError:
                return
            if not template.get("continuous"):
                return
            base = Path(template["raw_root"]).parent.parent
            metadata = {
                key: template[key]
                for key in (
                    "collection_id",
                    "dataset_name",
                    "task_name",
                    "instruction",
                    "collector_name",
                    "data_path",
                    "direct_hdf5",
                    "local_root",
                    "local_prefix",
                    "robot_id",
                    "project_id",
                    "profile",
                    "profile_hash",
                    "mode",
                    "capture_backend",
                )
                if key in template
            }
            if not metadata.get("task_name"):
                task, collector, name = self._dataset(template)
                metadata.update(task_name=task, collector_name=collector)
                metadata.update(
                    collection_metadata(
                        {
                            **template,
                            "dataset_name": name,
                            "capture_mode": template["mode"],
                            "raw_format": backend_for_record(template).raw_format,
                        },
                        "default",
                        "",
                    )
                )
            for item in sessions:
                cid = item["session_id"]
                raw = Path(item["raw_root"])
                if (
                    item.get("continuous_group_id") != group
                    or raw != base / cid / "raw"
                    or raw.resolve() != raw
                ):
                    raise DomainError("连续采集会话不属于当前任务路径")
                before = None
                try:
                    row = self.store.get("capture", cid)
                    before = dict(row)
                except DomainError:
                    row = {
                        **metadata,
                        "id": cid,
                        "idempotency_key": "continuous-" + cid,
                        "request_hash": item["request_hash"],
                        "raw_root": str(raw),
                        "created_at": item["created_at"],
                        "error": None,
                        "discard_requested": False,
                        "continuous": True,
                        "continuous_group_id": group,
                        "state": "STARTING",
                        "start_requested_at": item["created_at"],
                    }
                if row["state"] == "DISCARDED":
                    continue
                for key in ("conversion_state", "conversion_error", "conversion_finished_at"):
                    if key in item:
                        row[key] = item[key]
                phase = item["phase"]
                if phase in {"PREPARING", "STARTING", "RECORDING"} and not row.get(
                    "task_name"
                ):
                    row.update(metadata)
                if phase in {"PREPARING", "STARTING"}:
                    row.update(
                        state="STARTING",
                        start_pending=True,
                        start_message=item.get("message"),
                        error=None,
                        capture_failed=False,
                    )
                elif phase == "RECORDING":
                    row.update(
                        state="RECORDING",
                        start_pending=False,
                        start_message=None,
                        recording_message=item.get("message"),
                        error=None,
                    )
                elif phase == "STOPPING":
                    row.update(
                        state="STOPPING",
                        start_pending=False,
                        recording_message=item.get("message"),
                    )
                elif phase == "STOPPED":
                    if item.get("cancelled") and not item.get("frames"):
                        row.update(
                            state="DISCARDED", start_pending=False, start_message=None,
                            recording_message=None, capture_failed=False, error=None,
                            stopped_at=item.get("stopped_at"),
                        )
                    else:
                        row.update(
                            state="SAVED",
                            start_pending=False,
                            start_message=None,
                            recording_message=None,
                            stopped_at=item.get("stopped_at"),
                            saved_at=item.get("stopped_at"),
                            capture_failed=bool(item.get("fail"))
                            or row.get("capture_failed", False),
                            error=item.get("error")
                            or item.get("continuation_error")
                            or (
                                row.get("error") if row.get("capture_failed") else None
                            ),
                        )
                        self._saved_path(row)
                elif phase == "RECOVERY_REQUIRED":
                    row.update(
                        state="RECOVERY_REQUIRED",
                        start_pending=False,
                        capture_failed=True,
                        error=item.get("error"),
                    )
                else:
                    continue
                if item.get("recording_started_at") is not None:
                    row["started_at"] = item["recording_started_at"]
                row["frames"] = item.get("frames", 0)
                if row != before:
                    self.save(row)
            # Process starts after all stops, even if several short episodes
            # completed between polls. PREPARING has no started_at and cannot
            # confirm the previous episode.
            for item in sorted(
                sessions,
                key=lambda value: value.get("recording_started_at") or float("inf"),
            ):
                self._queue_previous_upload(
                    self.store.get("capture", item["session_id"])
                )

    def sync_recording_stops(self):
        """Turn an adapter-side automatic stop into a Client SAVED session."""
        for previous in self.store.all("capture"):
            if previous["state"] not in {"RECORDING", "STOPPING"} or previous.get(
                "continuous"
            ):
                continue
            status = self.adapter.status()
            if status.get("session_id") not in (None, previous["id"]):
                continue
            if status.get("phase") != "STOPPED":
                if status.get("phase") == "RECOVERY_REQUIRED" or status.get("fail"):
                    with self.lock:
                        row = self.store.get("capture", previous["id"])
                        row.update(
                            state="RECOVERY_REQUIRED",
                            capture_failed=True,
                            error=status.get("error"),
                        )
                        self.save(row)
                elif status.get("phase") == "STOPPING":
                    with self.lock:
                        row = self.store.get("capture", previous["id"])
                        row.update(
                            state="STOPPING", recording_message=status.get("message")
                        )
                        self.save(row)
                elif status.get("phase") == "RECORDING":
                    with self.lock:
                        row = self.store.get("capture", previous["id"])
                        message = status.get("message")
                        if (
                            row["state"] == "RECORDING"
                            and row.get("recording_message") != message
                        ):
                            row["recording_message"] = message
                            self.save(row)
                continue
            with self.lock:
                row = self.store.get("capture", previous["id"])
                if row["state"] in {"RECORDING", "STOPPING"}:
                    self.stop(row["id"])

    def sync_pending_starts(self):
        """Observe an accepted asynchronous start without repeating the command."""
        pending = [
            row
            for row in self.store.all("capture")
            if row["state"] == "STARTING"
            and row.get("start_pending")
            and not row.get("continuous")
        ]
        if not pending:
            return
        status = self.adapter.status()
        for previous in pending:
            if status.get("session_id") != previous["id"]:
                continue
            with self.lock:
                row = self.store.get("capture", previous["id"])
                if row["state"] != "STARTING" or not row.get("start_pending"):
                    continue
                before = dict(row)
                if status.get("fail") or status.get("phase") == "RECOVERY_REQUIRED":
                    row.update(
                        state="RECOVERY_REQUIRED",
                        capture_failed=True,
                        error=status.get("error") or "Recorder 启动失败",
                        start_pending=False,
                    )
                elif status.get("phase") == "STOPPED":
                    # A short episode can finish before the next status poll.
                    self.stop(row["id"])
                    continue
                elif status.get("phase") == "RECORDING":
                    row.update(
                        state="RECORDING",
                        started_at=status.get("recording_started_at") or now(),
                        start_pending=False,
                        start_message=None,
                    )
                elif status.get("phase") == "STOPPING":
                    row.update(
                        state="STOPPING",
                        start_pending=False,
                        recording_message=status.get("message"),
                    )
                elif status.get("phase") in {"PREPARING", "STARTING"}:
                    row["start_message"] = status.get("message")
                if row != before:
                    self.save(row)
                    self._queue_previous_upload(row)

    def stop(self, cid, discard=False, fault=None):
        with self.lock:
            row = self.store.get("capture", cid)
            if row["state"] == "DISCARDED":
                return row
            if fault:
                row.update(capture_failed=True, error=fault)
                self.save(row)
            if discard:
                if row.get("upload_state", "NONE") != "NONE":
                    raise DomainError("该 Episode 已进入上传流程，不能再丢弃")
                row["discard_requested"] = True
                self.save(row)
            if row["state"] in {"DRAFT", "SAVED"}:
                if discard:
                    return self._discard(row)
                return row
            if row["state"] not in BUSY:
                raise DomainError("当前状态不允许停止")
            row["state"] = "STOPPING"
            self.save(row)
            try:
                result = self.adapter.stop(cid)
                if result.get("stopped") is False and result.get("pending") is True:
                    row.update(
                        recording_message=result.get("message"), start_pending=False
                    )
                    return self.save(row)
                if not result.get("stopped"):
                    raise DomainError("Recorder 未确认停止")
                row.update(
                    state="SAVED",
                    **{key: result[key] for key in ("conversion_state", "conversion_error") if key in result},
                    stopped_at=now(),
                    capture_failed=bool(result.get("fail"))
                    or row.get("capture_failed", False),
                    error=fault or result.get("error") or row.get("error"),
                    start_pending=False,
                    start_message=None,
                    recording_message=None,
                )
                if result.get("recording_started_at") is not None:
                    row["started_at"] = result["recording_started_at"]
                row["saved_at"] = row["stopped_at"]
                self._saved_path(row)
                self.save(row)
                self._queue_previous_upload(row)
                return (
                    self._discard(row)
                    if row["discard_requested"] or result.get("cancelled")
                    else row
                )
            except Exception as exc:
                row.update(state="RECOVERY_REQUIRED", error=str(exc))
                return self.save(row)

    def retry_conversion_and_upload(self, cid):
        with self.lock:
            self.sync_continuous()
            row = self.store.get("capture", cid)
            if row["state"] != "SAVED" or row.get("capture_failed") or row.get("discard_requested"):
                raise DomainError("只有已保存且未删除的片段可以重新转换并上传")
            if row.get("conversion_state") != "FAILED":
                raise DomainError("该片段没有失败的转换任务")
            retry = getattr(self.adapter, "retry_conversion", None)
            if retry is None:
                raise DomainError("当前采集后端不支持重新转换")
            # This explicit user action confirms upload; it need not wait for
            # another recording. UploadAgent still waits for conversion DONE.
            row.update(upload_state="QUEUED", retry_at=0, upload_error=None,
                       conversion_retry_upload=True, upload_requested_at=now())
            self.save(row)
            result = retry(cid)
            row.update(conversion_state=result["conversion_state"], conversion_error=None)
            return self.save(row)

    def delete(self, cid):
        with self.lock:
            row = self.store.get("capture", cid)
            if row["state"] == "DISCARDED":
                return row
            if row.get("conversion_state") in {"QUEUED", "RUNNING"}:
                raise DomainError("正在转换，请等待转换结束后删除")
            if row.get("upload_state") == "UPLOADING":
                raise DomainError("正在上传，请等待上传结束后删除")
            if cid in self.deleting:
                raise DomainError("该片段正在移入回收站")
            if row["state"] not in {"DRAFT", "SAVED"}:
                return self.stop(cid, discard=True)
            row.update(discard_requested=True, delete_error=None, delete_pending=True)
            self.save(row)
            self.deleting.add(cid)
        try:
            from .upload_trash import trash_uploaded_copy

            result = trash_uploaded_copy(self.settings, row)
            with self.lock:
                row = self.store.get("capture", cid)
                row.update(upload_trash_receipt=result, deleted_at=now(), delete_pending=False)
                if result:
                    row["remote_trash_path"] = result.get("trash_path")
                return self._discard(row)
        except Exception as exc:
            with self.lock:
                row = self.store.get("capture", cid)
                row.update(delete_error=str(exc), delete_pending=False)
                self.save(row)
            raise DomainError("删除未完成：" + str(exc)) from exc
        finally:
            with self.lock:
                self.deleting.discard(cid)

    def _discard(self, row):
        raw = Path(row["raw_root"])
        trash = raw.parent / "discarded"
        if raw.resolve() != raw or trash.resolve() != trash:
            raise DomainError("拒绝符号链接目录")
        artifacts = [raw.parent / "replay.hdf5", raw.parent / "manifest.json",
                     raw.parent / "ssh-upload"]
        for key in ("published_path", "published_raw_path"):
            if not row.get(key):
                continue
            published = Path(row[key])
            directory = self._data_path(row["data_path"])
            if published.parent != directory or not re.fullmatch(
                r"episode[1-9][0-9]*\.(hdf5|raw)", published.name
            ):
                raise DomainError("已保存采集文件不属于当前会话")
            artifacts.append(published)
        if row.get("package_path"):
            package = Path(row["package_path"])
            if package.parent != raw.parent:
                raise DomainError("历史采集文件不在会话目录内")
            artifacts.append(package)
        artifacts = list(dict.fromkeys(path for path in artifacts if path.exists()))
        if any(path.resolve() != path for path in artifacts):
            raise DomainError("拒绝符号链接文件")
        if raw.exists():
            os.replace(raw, trash)
        if artifacts:
            trash.mkdir(exist_ok=True)
            for path in artifacts:
                try:
                    os.replace(path, trash / path.name)
                except OSError as exc:
                    if exc.errno != errno.EXDEV:
                        raise
                    shutil.move(str(path), str(trash / path.name))
        if trash.exists():
            fsync_dir(trash)
            fsync_dir(trash.parent)
            row["trash_path"] = str(trash)
        row.update(state="DISCARDED")
        return self.save(row)

    def health(self):
        try:
            health = self.adapter.health()
            status = self.adapter.status()
            return {**health, "status": status}
        except Exception as exc:
            return {"ready": False, "mode": self.settings.mode, "detail": str(exc)}
