"""Immutable, portable annotation ZIP exports; source media remains external."""

from __future__ import annotations

import copy
import os
import uuid
import zipfile
from pathlib import Path

from pipeline.shared.core import DomainError, canonical, file_hash, fsync_dir, now

from .datasets import dataset_for_job
from .merges import source_snapshot
from .recycle import dataset_lock
from .storage import data_directory


def _portable(value):
    # Only the workspace envelope contains internal paths; user dictionaries
    # and tool arguments may legitimately use these names at any depth.
    return {
        key: copy.deepcopy(item)
        for key, item in value.items()
        if not key.startswith("_")
        and key not in {"dataset_path", "source_path", "storage_dir"}
    }


def write_annotation_bundle(target: Path, snapshot: dict, source: Path) -> None:
    """Write a standalone label snapshot with content hashes, never source bytes."""
    manifest = _portable(copy.deepcopy(snapshot))
    episodes = manifest.pop("episodes", [])
    scheme = manifest.pop("scheme", None)
    source_files = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise DomainError("来源包含符号链接，拒绝导出", 409)
        if not path.is_file():
            continue
        relative = path.relative_to(source).as_posix()
        row = {"path": relative, "bytes": path.stat().st_size}
        if relative.startswith(("meta/", "data/")):
            row["sha256"] = file_hash(path)
        source_files.append(row)
    manifest.update(
        format="bodypipeline-annotations-v1",
        media_bytes_verified=False,
        source_files=source_files,
        includes_source_media=False,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name("." + target.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr("manifest.json", canonical(manifest))
            archive.writestr("scheme.json", canonical(scheme))
            archive.writestr(
                "episodes.jsonl", b"".join(canonical(item) + b"\n" for item in episodes)
            )
            archive.writestr(
                "README.txt",
                "BodyPipeline annotation snapshot\nSource videos and robot data are NOT included.\n"
                "Use the dataset matching manifest.json metadata/Parquet SHA256 hashes and frame mappings.\n"
                "Media lists contain names/sizes only: media byte equality has NOT been verified.\n"
                "Intervals are [start_frame,end_frame); missing/null values are not successful outcomes.\n"
                "source_signature is a local concurrency token, NOT a portable content hash.\n",
            )
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        fsync_dir(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


class ManualAnnotationExports:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store

    def enqueue(self, dataset_id, body):
        from .manual_annotations import ManualAnnotations

        snapshot = ManualAnnotations(self.settings, self.store).export_snapshot(
            dataset_id, body
        )
        job = self.store.get("job", dataset_id)
        source = dataset_for_job(self.settings, job)
        identifier = str(uuid.uuid4())
        output = data_directory(
            self.settings, job, "annotations", "manual-exports/" + identifier
        )
        row = {
            "id": identifier,
            "dataset_id": dataset_id,
            "job_id": dataset_id,
            "dataset_path": str(source),
            "state": "QUEUED",
            "snapshot": snapshot,
            "source_signature": snapshot["source_signature"],
            "created_at": now(),
            "output": str(output / "annotations.zip"),
            "error": None,
        }
        self.store.put("manual_annotation_export", identifier, row)
        return self.status(dataset_id, identifier)

    def _row(self, dataset_id, identifier):
        row = self.store.get("manual_annotation_export", identifier)
        if row["dataset_id"] != dataset_id:
            raise DomainError("导出任务不属于此数据集", 404)
        return row

    def status(self, dataset_id, identifier):
        row = self._row(dataset_id, identifier)
        return {
            key: row.get(key)
            for key in (
                "id",
                "dataset_id",
                "state",
                "created_at",
                "finished_at",
                "error",
            )
        }

    def download(self, dataset_id, identifier):
        row = self._row(dataset_id, identifier)
        if row["state"] != "COMPLETED":
            raise DomainError("标注包尚未生成完成", 409)
        path = Path(row["output"])
        if not path.is_file() or path.is_symlink():
            raise DomainError("标注包不存在", 404)
        return path

    def recover(self):
        for row in self.store.all("manual_annotation_export"):
            if row["state"] == "RUNNING":
                row.update(state="QUEUED", error=None)
                self.store.put("manual_annotation_export", row["id"], row)

    def tick(self):
        pending = [
            r
            for r in self.store.all("manual_annotation_export")
            if r["state"] == "QUEUED"
        ]
        if not pending:
            return False
        row = pending[-1]
        row.update(state="RUNNING", error=None)
        self.store.put("manual_annotation_export", row["id"], row)
        try:
            import hashlib

            source = Path(row["dataset_path"])
            with dataset_lock(
                self.store, hashlib.sha256(str(source).encode()).hexdigest()
            ):
                job = self.store.get("job", row["dataset_id"])
                if dataset_for_job(self.settings, job) != source:
                    raise DomainError("导出来源已变化", 409)
                signature = source_snapshot(source)["signature"]
                if signature != row["source_signature"]:
                    raise DomainError("导出来源已变化，请重新提交", 409)
                write_annotation_bundle(Path(row["output"]), row["snapshot"], source)
                if source_snapshot(source)["signature"] != signature:
                    Path(row["output"]).unlink(missing_ok=True)
                    raise DomainError("导出期间来源发生变化，请重试", 409)
                row.update(state="COMPLETED", finished_at=now())
        except Exception as exc:
            row.update(state="FAILED", error=str(exc), finished_at=now())
        self.store.put("manual_annotation_export", row["id"], row)
        return True
