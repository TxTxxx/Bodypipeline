#!/usr/bin/env python3
"""Offline copy-and-verify migration into data_root/<robot_type>/.

Stop Server and Viewer, set data_root in the server config, then run this script.
Original data and a snapshot of the records remain available for rollback. Legacy
and migrated records can coexist, so an interrupted migration is safe to rerun.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import uuid
from pathlib import Path

from pipeline.server.ingest import IngestService
from pipeline.server.storage import (
    ROBOT_LAYOUT,
    prepare_storage,
    robot_type,
    storage_path,
)
from pipeline.shared.config import Settings
from pipeline.shared.core import (
    DomainError,
    ProcessLock,
    atomic_json,
    confined,
    digest,
    durable_mkdir,
    file_hash,
    fsync_dir,
    now,
)


def inventory(root: Path, link_root: Path | list[Path] | None = None) -> dict:
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"Not a regular directory: {root}")
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            # Existing alignment views link to immutable raw files. Materialize
            # only these known links, so migrated work never depends on old raw.
            resolved = path.resolve(strict=True)
            allowed = (
                link_root
                if isinstance(link_root, list)
                else ([link_root] if link_root else [])
            )
            approved_root = next(
                (r for r in allowed if resolved.is_relative_to(r)), None
            )
            relative = path.relative_to(root).parts
            staging = path.is_relative_to(root / "aligned") or (
                len(relative) > 3
                and relative[0] == "sources"
                and relative[1].isdigit()
                and relative[2] == "aligned"
            )
            if approved_root is None or not staging or not resolved.is_file():
                raise ValueError(f"Refusing symlink: {path}")
            confined(approved_root, str(resolved.relative_to(approved_root)))
        if path.is_file():
            files[str(path.relative_to(root))] = (path.stat().st_size, file_hash(path))
        elif not path.is_dir():
            raise ValueError(f"Refusing special file: {path}")
    return files


def copy_verified(
    source: Path, target: Path, link_root: Path | list[Path] | None = None
) -> dict:
    expected = inventory(source, link_root)
    if target.exists():
        if inventory(target) != expected:
            raise FileExistsError(f"Destination differs; refusing overwrite: {target}")
    else:
        durable_mkdir(target.parent)
        temporary = target.with_name(f".{target.name}.copy-{uuid.uuid4()}")
        try:
            shutil.copytree(source, temporary)
            if (
                inventory(temporary) != expected
                or inventory(source, link_root) != expected
            ):
                raise ValueError(
                    f"Copy verification failed or source changed: {source}"
                )
            for name in expected:
                with (temporary / name).open("rb") as stream:
                    os.fsync(stream.fileno())
            for directory in sorted(
                (p for p in temporary.rglob("*") if p.is_dir()),
                key=lambda p: len(p.parts),
                reverse=True,
            ):
                fsync_dir(directory)
            fsync_dir(temporary)
            os.rename(temporary, target)
            fsync_dir(target.parent)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return {"files": len(expected), "bytes": sum(size for size, _ in expected.values())}


def migrate(settings) -> dict:
    if settings.data_root is None or not settings.data_root.is_dir():
        raise ValueError("Configure and create data_root before migration")
    lock = ProcessLock(settings.root / "server.lock")
    lock.acquire()
    try:
        ingest = IngestService(settings)
        try:
            viewer = ingest.store.get("viewer", "active")
        except DomainError as exc:
            if exc.code != 404:
                raise
        else:
            if viewer.get("state") in {"READY", "STARTING"}:
                raise ValueError("Stop the Viewer session before migration")
        uploads = ingest.store.all("upload")
        jobs = ingest.store.all("job")
        if any(r["state"] == "COMMITTING" for r in uploads) or any(
            j["state"] == "RUNNING" for j in jobs
        ):
            raise ValueError("Recover interrupted commits/jobs before migration")
        backup = settings.root / "migrations" / f"data-{uuid.uuid4()}"
        atomic_json(
            backup / "before.json",
            {"uploads": uploads, "jobs": jobs, "data_root": str(settings.data_root)},
        )
        plan, changed_uploads, changed_jobs = [], [], []
        by_id = {}
        for row in uploads:
            if row.get("data_layout") == ROBOT_LAYOUT:
                # Validate even already-migrated roots before continuing.
                ingest.manifest(row, fresh=True)
                by_id[row["id"]] = row
                continue
            manifest = ingest.manifest(row, fresh=True)
            updated = {
                **row,
                "robot_type": robot_type(manifest["profile"].get("robot_type")),
                "data_layout": ROBOT_LAYOUT,
            }
            prepare_storage(settings, updated)
            source = ingest.root(row)
            target = (
                ingest.final_dir(updated)
                if row["state"] == "COMMITTED"
                else storage_path(settings, updated, "incoming", row["id"])
            )
            if row["state"] == "COMMITTED":
                for spec in manifest["files"]:
                    path = confined(source / "data", spec["path"])
                    if (
                        path.stat().st_size != spec["size_bytes"]
                        or file_hash(path) != spec["sha256"]
                    ):
                        raise ValueError(
                            f"Committed raw file failed verification: {path}"
                        )
                updated["raw_path"] = str(target / "data")
            plan.append({"source": str(source), "target": str(target)})
            changed_uploads.append(updated)
            by_id[row["id"]] = updated
        for job in jobs:
            if job.get("data_layout") == ROBOT_LAYOUT:
                continue
            upload = by_id[job["upload_id"]]
            updated = {
                **job,
                "robot_type": upload["robot_type"],
                "data_layout": ROBOT_LAYOUT,
            }
            prepare_storage(settings, updated)
            for area in ("work",):
                source = storage_path(settings, job, area, job["id"])
                target = storage_path(settings, updated, area, job["id"])
                if source.exists():
                    item = {"source": str(source), "target": str(target)}
                    if area == "work":
                        item["link_roots"] = [
                            str(
                                ingest.final_dir(
                                    {
                                        k: v
                                        for k, v in by_id[uid].items()
                                        if k != "data_layout"
                                    }
                                )
                                / "data"
                            )
                            for uid in job.get("upload_ids", [job["upload_id"]])
                        ]
                    plan.append(item)
                for field in ("work_path", "dataset_path"):
                    if job.get(field):
                        path = Path(job[field])
                        if path.is_relative_to(source):
                            if not path.exists():
                                raise ValueError(f"Job references missing data: {path}")
                            updated[field] = str(target / path.relative_to(source))
            changed_jobs.append(updated)
        atomic_json(backup / "plan.json", {"copies": plan})
        for item in plan:
            item.update(
                copy_verified(
                    Path(item["source"]),
                    Path(item["target"]),
                    [Path(r) for r in item.get("link_roots", [])],
                )
            )
        # Files are complete before any state record starts referencing them.
        # Each update is atomic; all old data remain readable after a crash.
        for kind, rows in (("upload", changed_uploads), ("job", changed_jobs)):
            for row in rows:
                ingest.store.put(kind, row["id"], row)
        for row in changed_uploads:
            assert digest(ingest.manifest(row, fresh=True)) == row["manifest_hash"]
        result = {
            "completed_at": now(),
            "data_root": str(settings.data_root),
            "backup": str(backup),
            "uploads": len(changed_uploads),
            "jobs": len(changed_jobs),
            "copies": plan,
            "originals_retained": True,
        }
        atomic_json(backup / "result.json", result)
        return result
    finally:
        lock.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            migrate(Settings.load("server", args.config)), ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
