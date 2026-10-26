"""Synchronize external corrections into the existing saved/uploaded Episode flow."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
from pathlib import Path
import re
import shutil
import threading
import time
from zoneinfo import ZoneInfo

import httpx

from pipeline.shared.core import DomainError, fsync_dir, now
from .adapters.registry import get_capture_backend


def episode_provider(name, profile):
    if name == "enactive_arx_kinesthetic_v1":
        from .episode_providers.arx import ArxKinestheticEpisodes
        return ArxKinestheticEpisodes(profile)
    raise DomainError("未知的纠偏 Episode Provider，必须通过可信配置显式绑定")


class DaggerEpisodeService:
    def __init__(self, settings, capture, inference):
        self.settings, self.capture, self.inference = settings, capture, inference
        config = settings.capture.get("inference", {})
        name = config.get("episode_provider")
        self.provider = episode_provider(name, capture.profile) if name else None
        self.url = str(inference.urls.get("dagger", "")).rstrip("/")
        self.enabled = self.provider is not None and bool(self.url)
        self.http = httpx.Client(timeout=5, trust_env=False)
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dagger-episode-import")
        self.future = None
        self.error = ""
        for row in self.capture.store.all("dagger_episode"):
            if row.get("import_state") == "IMPORTING":
                row.update(import_state="QUEUED", retry_at=0)
                self.capture.store.put("dagger_episode", row["id"], row)

    def update(self, episode_id, **changes):
        with self.capture.lock:
            row = self.capture.store.get("dagger_episode", episode_id)
            row.update(changes, updated_at=now())
            self.capture.store.put("dagger_episode", episode_id, row)
            return row

    def tick(self):
        if not self.enabled:
            return
        try:
            response = self.http.get(self.url + "/dagger/episodes")
            response.raise_for_status()
            episodes = response.json()["episodes"]
            with self.capture.lock:
                for source in episodes:
                    episode_id = source["id"]
                    if not re.fullmatch(r"[a-f0-9]{32}", episode_id):
                        raise ValueError("纠偏 Episode 标识无效")
                    try:
                        row = self.capture.store.get("dagger_episode", episode_id)
                    except DomainError as error:
                        if error.code != 404:
                            raise
                        row = {"id": episode_id, "created_at": now(), "import_state": "QUEUED"}
                    if row.get("source") != source:
                        row.update(source=source, updated_at=now())
                        self.capture.store.put("dagger_episode", episode_id, row)
                rows = self.capture.store.all("dagger_episode")
            self.error = ""
            if self.future is None or self.future.done():
                deletion = next((row for row in rows if row.get("delete_requested") and not row.get("deleted_at")
                                 and row.get("delete_retry_at", 0) <= time.time()), None)
                if deletion:
                    self.future = self.pool.submit(self._delete, deletion["id"])
                    return
                candidate = next((row for row in sorted(rows, key=lambda item: item["source"].get("started_at", 0))
                                  if row["source"]["state"] == "SAVED" and row.get("import_state") in {"QUEUED", "RETRY"}
                                  and not row.get("delete_requested") and not row.get("deleted_at")
                                  and row.get("retry_at", 0) <= time.time()), None)
                if candidate:
                    self.update(candidate["id"], import_state="IMPORTING", import_error=None)
                    self.future = self.pool.submit(self._import, candidate["id"])
        except Exception as error:
            self.error = str(error)

    def _destination(self, source):
        checkpoint = source.get("checkpoint")
        path = Path(checkpoint or "")
        if not checkpoint or not path.is_absolute() or not path.name or ".." in path.parts:
            raise DomainError("纠偏缺少实际推理权重路径，保留原始数据，不能归入其他权重")
        name = "__".join(re.sub(r"[^\w.-]", "_", part) for part in (path.parent.name, path.name) if part)
        date = datetime.fromtimestamp(source["started_at"], ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
        directory = self.capture._capture_root() / "DAgger" / name / date
        self.capture._data_path(directory, create=True)
        return "DAgger-" + name, "", directory

    def _import(self, episode_id):
        try:
            item = self.capture.store.get("dagger_episode", episode_id)
            self._check_import_allowed(episode_id)
            source = item["source"]
            if item.get("capture_id"):
                capture = self.capture.store.get("capture", item["capture_id"])
                profile = capture["profile"]
                if (capture.get("conversion_state") == "DONE" and capture.get("published_path")
                        and Path(capture["published_path"]).is_file()):
                    self.update(episode_id, import_state="DONE", import_error=None)
                    return
            else:
                profile = self.provider.profile(source)
                backend = get_capture_backend(self.settings, profile, self.provider.backend_id)
                task, collector, directory = self._destination(source)
                body = {"idempotency_key": "dagger-" + episode_id, "dataset_name": task,
                        "collector_name": collector, "instruction": source["instruction"], "data_path": str(directory),
                        "local_root": next(iter(self.capture.roots)), "local_prefix": "", "continuous": False}
                capture = self.capture.create(body, external_backend=backend, external_profile=profile)
                self.update(episode_id, capture_id=capture["id"])
            raw = Path(capture["raw_root"])
            raw.mkdir(parents=True, exist_ok=True)
            bundle = raw / "episode0/episode.raw"
            bundle.parent.mkdir(parents=True, exist_ok=True)
            temporary = bundle.with_suffix(".raw.part")
            if not bundle.is_file():
                route = f"/dagger/episodes/{episode_id}/bundle"
                if source.get("bundle_url") != route or source.get("bundle_bytes", 0) > self.settings.max_upload_bytes:
                    raise DomainError("纠偏 bundle 地址或文件大小无效")
                if shutil.disk_usage(raw).free - 2 * source["bundle_bytes"] < self.settings.min_free_bytes:
                    raise DomainError("本地磁盘空间不足，原始纠偏数据保留，等待重试保存")
                self.update(episode_id, download_bytes=0, download_total=source["bundle_bytes"])
                hasher, total = hashlib.sha256(), 0
                with self.http.stream("GET", self.url + route, timeout=60) as response:
                    response.raise_for_status()
                    with temporary.open("wb") as stream:
                        for chunk in response.iter_bytes(1024 * 1024):
                            self._check_import_allowed(episode_id)
                            total += len(chunk)
                            if total > source["bundle_bytes"]:
                                raise DomainError("纠偏 bundle 超过声明大小")
                            stream.write(chunk)
                            hasher.update(chunk)
                            self.update(episode_id, download_bytes=total)
                        stream.flush()
                        import os
                        os.fsync(stream.fileno())
                if total != source["bundle_bytes"] or hasher.hexdigest() != source["bundle_sha256"]:
                    raise DomainError("纠偏 bundle 传输校验失败")
                temporary.replace(bundle)
                fsync_dir(bundle.parent)
            elif bundle.stat().st_size != source["bundle_bytes"] or self._file_hash(bundle) != source["bundle_sha256"]:
                raise DomainError("已保存的 raw 校验失败，禁止转换和覆盖")
            self._check_import_allowed(episode_id)
            with self.capture.lock:
                self._check_import_allowed(episode_id)
                row = self.capture.store.get("capture", capture["id"])
                row.update(source="dagger", source_episode_id=episode_id, source_session_id=source["session_id"],
                           checkpoint=source["checkpoint"], started_at=source["started_at"], stopped_at=source.get("stopped_at"),
                           saved_at=row.get("saved_at") or now(), upload_trigger_checked=True,
                           source_raw_format="enactive_arx_kinesthetic_zip_v1", conversion_state="QUEUED", conversion_error=None,
                           state="SAVED", upload_state="QUEUED", frames=source["frames"], duration_s=source.get("duration_s", 0))
                self.capture._publish_episode(row, bundle, ".raw")
                self.capture.save(row)
            # Same raw-first lifecycle as the workbench. UploadAgent waits for
            # conversion DONE; a failed conversion always leaves the raw intact.
            with self.capture.lock:
                row = self.capture.store.get("capture", capture["id"])
                row.update(conversion_state="RUNNING")
                self.capture.save(row)
            stats = self.provider.materialize(bundle, raw / "episode0/episode.hdf5", profile, source, capture["id"])
            with self.capture.lock:
                self._check_import_allowed(episode_id)
                row = self.capture.store.get("capture", capture["id"])
                row.update(source="dagger", source_episode_id=episode_id, source_session_id=source["session_id"],
                           started_at=source["started_at"], stopped_at=source.get("stopped_at"), saved_at=now(),
                           upload_trigger_checked=True, capture_stats=stats, conversion_state="DONE",
                           conversion_error=None, conversion_finished_at=now())
                self.capture._publish_hdf5(row, raw / "episode0/episode.hdf5")
                row.update(state="SAVED", upload_state="QUEUED", retry_at=0, upload_error=None,
                           frames=source["frames"], duration_s=source.get("duration_s", 0), error=None)
                self.capture.save(row)
            self.update(episode_id, import_state="DONE", import_error=None)
        except Exception as error:
            item = self.capture.store.get("dagger_episode", episode_id)
            if item.get("delete_requested"):
                self.update(episode_id, import_state="CANCELLED", import_error=None)
                return
            if item.get("capture_id"):
                with self.capture.lock:
                    row = self.capture.store.get("capture", item["capture_id"])
                    if row.get("published_raw_path"):
                        row.update(conversion_state="FAILED", conversion_error=str(error))
                        self.capture.save(row)
            attempts = item.get("attempts", 0) + 1
            self.update(episode_id, import_state="RETRY", import_error=str(error), attempts=attempts,
                        retry_at=time.time() + min(300, 2 ** min(attempts, 8)))

    @staticmethod
    def _file_hash(path):
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def rows(self):
        result = []
        with self.capture.lock:
            for item in self.capture.store.all("dagger_episode"):
                row = {**item["source"], **{key: value for key, value in item.items() if key != "source"}}
                if item.get("capture_id"):
                    capture = self.capture.store.get("capture", item["capture_id"])
                    row["video_download_supported"] = (capture["state"] == "SAVED"
                                                        and self.capture.describe(capture)["video_download_supported"])
                    for key in ("saved_path", "published_path", "published_raw_path", "conversion_state", "conversion_error", "checkpoint", "upload_state", "uploaded_bytes", "total_bytes", "remote_path",
                                "upload_error", "uploaded_at", "task_name", "collector_name", "remote_trash_path"):
                        if key in capture:
                            row[key] = capture[key]
                    if capture["state"] == "DISCARDED":
                        row["state"] = "DISCARDED"
                if item.get("deleted_at"):
                    row["state"] = "DISCARDED"
                elif item.get("delete_requested"):
                    # Native recycling and remote recycling are one workflow;
                    # report completion only when all three copies acknowledge.
                    row.pop("deleted_at", None)
                    row["deleted"] = False
                result.append(row)
        return sorted(result, key=lambda item: item.get("started_at", 0), reverse=True)

    def retry(self, episode_id):
        item = self.capture.store.get("dagger_episode", episode_id)
        if item.get("delete_requested") or item.get("deleted_at"):
            raise DomainError("该 Episode 已请求删除，不能重新上传", 409)
        if item["source"]["state"] == "ERROR":
            response = self.http.post(self.url + f"/dagger/episodes/{episode_id}/retry", json={})
            response.raise_for_status()
            return
        if item.get("capture_id"):
            with self.capture.lock:
                capture = self.capture.store.get("capture", item["capture_id"])
                if capture.get("upload_state") == "RETRY":
                    capture.update(upload_state="QUEUED", retry_at=0, upload_error=None)
                    self.capture.save(capture)
                    return
        if item.get("import_state") != "RETRY":
            raise DomainError("该 Episode 没有需要重试的保存或上传", 409)
        self.update(episode_id, import_state="QUEUED", retry_at=0, import_error=None)

    def _check_import_allowed(self, episode_id):
        with self.capture.lock:
            row = self.capture.store.get("dagger_episode", episode_id)
            if row.get("delete_requested") or row.get("deleted_at"):
                raise DomainError("Episode 已请求删除，已停止整理和上传", 409)

    def delete(self, episode_id):
        with self.capture.lock:
            item = self.capture.store.get("dagger_episode", episode_id)
            if item.get("deleted_at"):
                return {"ok": True, "deleted": True, "episode_id": episode_id}
            if item["source"]["state"] == "RECORDING":
                raise DomainError("请先保留或丢弃当前纠偏，再删除 Episode", 409)
            if item.get("capture_id"):
                row = self.capture.store.get("capture", item["capture_id"])
                row.update(discard_requested=True)
                self.capture.save(row)
            self.update(episode_id, delete_requested=True, delete_pending=True, delete_error=None,
                        delete_state="QUEUED", delete_retry_at=0)
        return {"ok": True, "delete_pending": True, "episode_id": episode_id,
                "message": "已请求删除；本地、原始纠偏及已上传副本将移入回收站"}

    def _delete(self, episode_id):
        try:
            item = self.capture.store.get("dagger_episode", episode_id)
            if item.get("capture_id"):
                row = self.capture.store.get("capture", item["capture_id"])
                if row.get("upload_state") == "UPLOADING":
                    self.update(episode_id, delete_state="WAITING_UPLOAD", delete_pending=True,
                                delete_retry_at=time.time() + 1)
                    return
                self.capture.delete(item["capture_id"])
            response = self.http.post(self.url + f"/dagger/episodes/{episode_id}/delete", json={}, timeout=15)
            response.raise_for_status()
            receipt = response.json()
            if receipt.get("deleted") is not True or receipt.get("episode_id") != episode_id:
                raise DomainError("原始纠偏未确认删除")
            self.update(episode_id, delete_state="DONE", delete_pending=False, deleted_at=now(), delete_error=None,
                        import_state="DELETED", native_trash_path=receipt.get("trash_path"))
        except Exception as error:
            item = self.capture.store.get("dagger_episode", episode_id)
            attempts = item.get("delete_attempts", 0) + 1
            self.update(episode_id, delete_state="RETRY", delete_pending=False, delete_error=str(error),
                        delete_attempts=attempts, delete_retry_at=time.time() + min(60, 2 ** min(attempts, 6)))

    def close(self):
        self.pool.shutdown(wait=True)
        self.http.close()
