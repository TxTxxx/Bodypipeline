"""Stable task grouping shared by collectors and servers; no database required."""

from __future__ import annotations

import unicodedata

from .core import DomainError, digest


def dataset_name(value):
    if not isinstance(value, str):
        raise DomainError("数据集名称不能为空", 400)
    value = " ".join(unicodedata.normalize("NFKC", value).split())
    if not 1 <= len(value) <= 2000:
        raise DomainError("数据集名称须为 1–2000 个字符", 400)
    return value


def collection_metadata(manifest, target, prefix):
    name = dataset_name(manifest.get("dataset_name") or manifest.get("instruction"))
    scope = {
        "name": name,
        "project_id": manifest["project_id"],
        "robot_type": manifest["profile"]["robot_type"],
        "profile_hash": manifest["profile_hash"],
        "capture_mode": manifest["capture_mode"],
        "raw_format": manifest["raw_format"],
        "target": target,
        "prefix": prefix,
    }
    return {"collection_id": digest(scope)[:32], "dataset_name": name}


def group_uploads(rows):
    groups = {}
    for row in rows:
        cid = row.get("collection_id")
        if not cid:
            # A damaged legacy manifest remains visible but cannot join another task.
            cid = "unavailable-" + row["id"]
        if cid not in groups:
            groups[cid] = {
                "id": cid,
                "dataset_name": row.get("dataset_name")
                or row.get("instruction")
                or cid,
                "robot_type": row.get("robot_type"),
                "project_id": row.get("project_id"),
                "capture_mode": row.get("capture_mode"),
                "target": row.get("target"),
                "prefix": row.get("prefix", ""),
                "uploads": [],
            }
        groups[cid]["uploads"].append(row)
    for group in groups.values():
        group["uploads"].sort(key=lambda r: (r.get("created_at", 0), r["id"]))
        group["episode_count"] = len(group["uploads"])
        group["ready_count"] = sum(r["state"] == "COMMITTED" for r in group["uploads"])
        group["total_bytes"] = sum(r.get("total_bytes", 0) for r in group["uploads"])
    return list(groups.values())
