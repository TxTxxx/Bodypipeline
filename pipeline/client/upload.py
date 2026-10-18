from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

import httpx

from pipeline.shared.core import DomainError, digest, file_hash


class UploadAgent:
    def __init__(self, service):
        self.service = service
        self.settings = service.settings

    def process_once(self):
        for row in self.service.store.all("capture"):
            if (
                row["state"] != "SEALED"
                or row["upload_state"] not in {"QUEUED", "UPLOADING", "RETRY"}
                or row.get("retry_at", 0) > time.time()
            ):
                continue
            self.upload(row)
            return True
        return False

    def upload(self, row):
        root = Path(row["raw_root"])
        package = Path(row["package_path"]) if row.get("package_path") else None
        upload_root = package.parent if package is not None else root
        manifest_path = root.parent / "manifest.json"
        row.update(upload_state="UPLOADING", upload_error=None)
        self.service.save(row)
        try:
            manifest = json.loads(manifest_path.read_text())
            if digest(manifest) != row["manifest_hash"]:
                raise DomainError("本地 Manifest 被修改，禁止上传")
            with httpx.Client(
                base_url=self.settings.server_url,
                headers={"Authorization": f"Bearer {self.settings.device_token}"},
                timeout=120,
                trust_env=False,
            ) as http:

                def call(method, path, **kw):
                    r = http.request(method, path, **kw)
                    if r.status_code >= 400:
                        raise DomainError(f"服务器 {r.status_code}: {r.text[:400]}")
                    return r.json()

                info = call(
                    "POST",
                    "/device/uploads",
                    json={
                        "manifest": manifest,
                        "target": row["remote_target"],
                        "prefix": row["remote_prefix"],
                    },
                )
                uid = info["id"]
                row["upload_id"] = uid
                status = call("GET", f"/device/uploads/{uid}")
                if status["manifest_hash"] != row["manifest_hash"]:
                    raise DomainError("远端清单摘要不匹配")
                done = sum(status["offsets"])
                for i, spec in enumerate(manifest["files"]):
                    path = upload_root / spec["path"]
                    if path.is_symlink() or file_hash(path) != spec["sha256"]:
                        raise DomainError(f"封存后文件发生变化：{spec['path']}")
                    offset = status["offsets"][i]
                    with path.open("rb") as f:
                        f.seek(offset)
                        empty_pending = (
                            spec["size_bytes"] == 0 and status["state"] != "COMMITTED"
                        )
                        while offset < spec["size_bytes"] or empty_pending:
                            empty_pending = False
                            data = f.read(
                                min(self.settings.chunk_bytes, status["chunk_bytes"])
                            )
                            result = call(
                                "PUT",
                                f"/device/uploads/{uid}/files/{i}",
                                params={"offset": offset},
                                content=data,
                                headers={
                                    "X-Chunk-SHA256": hashlib.sha256(data).hexdigest()
                                },
                            )
                            done += result["offset"] - offset
                            offset = result["offset"]
                            row["uploaded_bytes"] = done
                            self.service.save(row)
                try:
                    receipt = call("POST", f"/device/uploads/{uid}/complete")
                except DomainError as exc:
                    import re

                    match = re.search(r"file_index=(\d+)", str(exc))
                    if match:
                        # A torn chunk may have survived a host crash. Restart only that
                        # immutable source file; next iteration resumes all other files.
                        call(
                            "POST",
                            f"/device/uploads/{uid}/files/{int(match.group(1))}/reset",
                        )
                    raise
                if (
                    receipt["state"] != "COMMITTED"
                    or receipt["manifest_hash"] != row["manifest_hash"]
                ):
                    raise DomainError("没有有效持久化回执")
                prune_error = None
                if package is not None and root.exists():
                    try:
                        if root.resolve() != root or root.is_symlink():
                            raise OSError("raw directory is a symlink")
                        shutil.rmtree(root)
                    except OSError as exc:
                        prune_error = str(exc)
                row.update(
                    upload_state="ACKED",
                    receipt=receipt,
                    uploaded_bytes=row["total_bytes"],
                    attempts=0,
                    retry_at=0,
                    raw_pruned_at=time.time()
                    if package is not None and not prune_error
                    else None,
                    raw_prune_error=prune_error,
                )
        except Exception as exc:
            attempts = row.get("attempts", 0) + 1
            row.update(
                upload_state="RETRY",
                upload_error=str(exc),
                attempts=attempts,
                retry_at=time.time() + min(300, 2 ** min(attempts, 8)),
            )
        self.service.save(row)
