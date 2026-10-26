"""Background packaging and resumable upload; recorded source files are retained."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

import httpx

from pipeline.shared.core import DomainError, atomic_json, digest, file_hash

from .adapters.registry import backend_for_record


class UploadAgent:
    def __init__(self, service):
        self.service = service
        self.settings = service.settings

    def update(self, cid, **changes):
        # Merge into the latest record so status polling cannot lose progress,
        # and transfer progress cannot overwrite recorder state.
        with self.service.lock:
            row = self.service.store.get("capture", cid)
            row.update(changes)
            return self.service.save(row)

    def process_once(self):
        with self.service.lock:
            rows = sorted(
                self.service.store.all("capture"), key=lambda row: row["created_at"]
            )
            row = next(
                (
                    row
                    for row in rows
                    if row["state"] == "SAVED"
                    and row.get("conversion_state") in {None, "DONE"}
                    and not row.get("capture_failed")
                    and not row.get("discard_requested")
                    and row.get("upload_state") in {"QUEUED", "RETRY"}
                    and row.get("retry_at", 0) <= time.time()
                ),
                None,
            )
            if row is None:
                return False
            row = self.update(row["id"], upload_state="UPLOADING", upload_error=None)
        self.upload(row)
        return True

    def package(self, row):
        root = Path(row["raw_root"])
        if root.resolve() != root:
            raise DomainError("原始采集目录不能包含符号链接")
        manifest_path = root.parent / "manifest.json"
        if row.get("manifest_hash"):
            manifest = json.loads(manifest_path.read_text())
            if digest(manifest) != row["manifest_hash"]:
                raise DomainError("本地 Manifest 被修改，禁止上传")
            return manifest
        backend = backend_for_record(row)
        package = root.parent / backend.filename
        metadata = {
            "session_id": row["id"],
            "robot_id": row["robot_id"],
            "project_id": row["project_id"],
            "instruction": row["instruction"],
            "dataset_name": row["dataset_name"],
            "task_name": row.get("task_name", row["dataset_name"]),
            "collector_name": row.get("collector_name", ""),
            "started_at": row["started_at"],
            "stopped_at": row.get("stopped_at"),
            "capture_mode": row["mode"],
            "profile_hash": row["profile_hash"],
        }
        stats = backend.package(root, package, row["profile"], metadata=metadata)
        manifest = {
            **metadata,
            "manifest_version": 1,
            "raw_format": backend.raw_format,
            "control_mode": "teleoperation",
            "profile": row["profile"],
            "complete": True,
            "files": [
                {
                    "path": backend.filename,
                    "size_bytes": stats["size_bytes"],
                    "sha256": file_hash(package),
                }
            ],
        }
        atomic_json(manifest_path, manifest)
        self.update(
            row["id"],
            package_path=str(package),
            manifest_hash=digest(manifest),
            total_bytes=stats["size_bytes"],
            uploaded_bytes=0,
        )
        return manifest

    def upload(self, row):
        cid = row["id"]
        try:
            manifest = self.package(row)
            manifest_hash = digest(manifest)
            root = Path(row["raw_root"]).parent
            if self.settings.capture.get("upload_ssh"):
                from .ssh_upload import upload_package

                receipt = upload_package(
                    self.settings,
                    row,
                    manifest,
                    lambda done: self.update(cid, uploaded_bytes=done),
                )
                self.update(
                    cid,
                    upload_id=cid,
                    upload_state="ACKED",
                    receipt=receipt,
                    remote_path=receipt["remote_path"],
                    attempts=0,
                    uploaded_bytes=sum(spec["size_bytes"] for spec in manifest["files"]),
                    retry_at=0,
                    uploaded_at=time.time(),
                    upload_error=None,
                )
                return
            with httpx.Client(
                base_url=self.settings.server_url,
                headers={"Authorization": f"Bearer {self.settings.device_token}"},
                timeout=120,
                trust_env=False,
            ) as http:

                def call(method, path, **kwargs):
                    response = http.request(method, path, **kwargs)
                    if response.status_code >= 400:
                        raise DomainError(
                            f"服务器 {response.status_code}: {response.text[:400]}"
                        )
                    return response.json()

                info = call(
                    "POST",
                    "/device/uploads",
                    json={
                        "manifest": manifest,
                        "target": row.get("remote_target", "default"),
                        "prefix": row.get("remote_prefix", ""),
                    },
                )
                uid = info["id"]
                self.update(cid, upload_id=uid)
                status = call("GET", f"/device/uploads/{uid}")
                if status["manifest_hash"] != manifest_hash:
                    raise DomainError("远端清单摘要不匹配")
                done = sum(status["offsets"])
                self.update(cid, uploaded_bytes=done)
                for index, spec in enumerate(manifest["files"]):
                    path = root / spec["path"]
                    if path.resolve() != path or file_hash(path) != spec["sha256"]:
                        raise DomainError(f"封存后文件发生变化：{spec['path']}")
                    offset = status["offsets"][index]
                    if not 0 <= offset <= spec["size_bytes"]:
                        raise DomainError("远端续传偏移无效")
                    with path.open("rb") as stream:
                        stream.seek(offset)
                        while offset < spec["size_bytes"]:
                            data = stream.read(
                                min(self.settings.chunk_bytes, status["chunk_bytes"])
                            )
                            result = call(
                                "PUT",
                                f"/device/uploads/{uid}/files/{index}",
                                params={"offset": offset},
                                content=data,
                                headers={
                                    "X-Chunk-SHA256": hashlib.sha256(data).hexdigest()
                                },
                            )
                            if not offset < result["offset"] <= spec["size_bytes"]:
                                raise DomainError("远端上传偏移未前进")
                            done += result["offset"] - offset
                            offset = result["offset"]
                            stream.seek(offset)
                            self.update(cid, uploaded_bytes=done)
                try:
                    receipt = call("POST", f"/device/uploads/{uid}/complete")
                except DomainError as exc:
                    match = re.search(r"file_index=(\d+)", str(exc))
                    if match:
                        call(
                            "POST",
                            f"/device/uploads/{uid}/files/{int(match.group(1))}/reset",
                        )
                    raise
                if (
                    receipt["state"] != "COMMITTED"
                    or receipt["manifest_hash"] != manifest_hash
                ):
                    raise DomainError("没有有效持久化回执")
                self.update(
                    cid,
                    upload_state="ACKED",
                    receipt=receipt,
                    attempts=0,
                    uploaded_bytes=sum(
                        spec["size_bytes"] for spec in manifest["files"]
                    ),
                    retry_at=0,
                    uploaded_at=time.time(),
                    upload_error=None,
                )
        except Exception as exc:
            attempts = row.get("attempts", 0) + 1
            self.update(
                cid,
                upload_state="RETRY",
                upload_error=str(exc),
                attempts=attempts,
                retry_at=time.time() + min(300, 2 ** min(attempts, 8)),
            )
