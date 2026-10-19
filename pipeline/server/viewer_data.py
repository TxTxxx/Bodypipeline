"""Read-only dataset file service for the always-running Visualizer.

The upstream application reads Hugging Face style ``<repo>/resolve/main/<path>``
URLs. The gateway exposes exactly that shape for every registered dataset, so one
long-lived Visualizer can show any number of datasets at once and "opening a
preview" only has to register a mapping, never start or stop a process.
"""

from __future__ import annotations

import mimetypes
import re
import time
from pathlib import Path
from urllib.parse import unquote

from fastapi import Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from pipeline.shared.core import DomainError, Store

REPO_ORG = "bp"
REPO_PATTERN = re.compile(r"^bp/([A-Za-z0-9_-]{1,128})$")
REGISTRY_KIND = "preview"
_COPY_BUFFER = 1024 * 1024


def repo_id_for(job_id: str) -> str:
    return f"{REPO_ORG}/{job_id}"


class PreviewRegistry:
    """Datasets that currently have a preview link, persisted as JSON files."""

    def __init__(self, store: Store, ttl: float):
        self.store, self.ttl = store, ttl

    def register(self, job_id: str, dataset: Path) -> dict:
        row = {
            "id": job_id,
            "job_id": job_id,
            "repo_id": repo_id_for(job_id),
            "dataset_path": str(dataset),
            "page_path": f"/{repo_id_for(job_id)}/episode_0",
            "expires_at": time.time() + self.ttl,
            "created_at": time.time(),
        }
        try:
            row["created_at"] = self.store.get(REGISTRY_KIND, job_id)["created_at"]
        except DomainError:
            pass
        self.store.put(REGISTRY_KIND, job_id, row)
        return row

    def lookup(self, repo_id: str) -> dict | None:
        match = REPO_PATTERN.fullmatch(repo_id)
        if not match:
            return None
        try:
            row = self.store.get(REGISTRY_KIND, match.group(1))
        except DomainError as exc:
            if exc.code == 404:
                return None
            raise
        if not isinstance(row.get("expires_at"), (int, float)):
            return None
        if row["expires_at"] < time.time():
            return None
        return row

    def active(self) -> list[dict]:
        now = time.time()
        rows = []
        for row in self.store.all(REGISTRY_KIND):
            if isinstance(row.get("expires_at"), (int, float)) and (
                row["expires_at"] >= now
            ):
                rows.append(row)
        return sorted(rows, key=lambda r: r.get("created_at", 0), reverse=True)

    def remove(self, job_id: str) -> None:
        try:
            self.store.delete(REGISTRY_KIND, job_id)
        except DomainError as exc:
            if exc.code != 404:
                raise

    def prune(self) -> int:
        removed = 0
        now = time.time()
        for row in self.store.all(REGISTRY_KIND):
            expires = row.get("expires_at")
            if not isinstance(expires, (int, float)) or expires < now:
                self.remove(row["id"])
                removed += 1
        return removed


def _confined_file(root: Path, relative: str) -> Path | None:
    """Resolve ``relative`` under ``root`` or return None (never raise to clients)."""

    if not relative or "\0" in relative:
        return None
    root = root.resolve(strict=False)
    candidate = (root / relative).resolve(strict=False)
    if not candidate.is_relative_to(root):
        return None
    current = root
    for part in candidate.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            return None
    if not candidate.is_file():
        return None
    return candidate


def _byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    if header is None:
        return None
    if not header.startswith("bytes=") or "," in header:
        raise ValueError("仅支持单个字节范围")
    start_text, end_text = header[6:].split("-", 1)
    if not start_text:
        length = int(end_text)
        if length <= 0:
            raise ValueError("无效的后缀范围")
        return max(0, size - length), size - 1
    start = int(start_text)
    end = min(size - 1, int(end_text)) if end_text else size - 1
    if start < 0 or start >= size or end < start:
        raise ValueError("范围超出文件")
    return start, end


def serve_dataset_file(request: Request, registry: PreviewRegistry, path: str):
    """Serve ``bp/<job>/resolve/main/<file>`` from the registered dataset root."""

    parts = path.split("/", 4)
    if len(parts) != 5 or parts[2] != "resolve" or parts[3] != "main":
        return Response(status_code=404)
    repo_id = f"{parts[0]}/{parts[1]}"
    row = registry.lookup(repo_id)
    if row is None:
        return Response(
            '{"detail":"预览已过期或不存在，请在管理台重新打开"}',
            status_code=404,
            media_type="application/json",
        )
    target = _confined_file(Path(row["dataset_path"]), unquote(parts[4]))
    if target is None:
        return Response(status_code=404)
    size = target.stat().st_size
    try:
        selected = _byte_range(request.headers.get("range"), size)
    except (ValueError, TypeError):
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    if selected is None:
        return FileResponse(
            target,
            media_type=mimetypes.guess_type(target.name)[0]
            or "application/octet-stream",
            headers={"Accept-Ranges": "bytes", "Cache-Control": "no-cache"},
        )
    start, end = selected
    length = end - start + 1 if size else 0

    def body():
        with target.open("rb") as source:
            source.seek(start)
            remaining = length
            while remaining:
                chunk = source.read(min(_COPY_BUFFER, remaining))
                if not chunk:
                    break
                yield chunk
                remaining -= len(chunk)

    return StreamingResponse(
        body() if request.method != "HEAD" else iter(()),
        status_code=206,
        media_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream",
        headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(length),
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-cache",
        },
    )


__all__ = [
    "PreviewRegistry",
    "REPO_ORG",
    "REGISTRY_KIND",
    "repo_id_for",
    "serve_dataset_file",
]
