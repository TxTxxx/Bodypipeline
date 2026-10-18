#!/usr/bin/env python3
"""Offline, one-time legacy export. Runtime services never import SQLite.

Stop Client/Server/Viewer first. Original state and data files are left untouched.
Only switch to the new state directory after every record has been verified.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path

from pipeline.shared.core import ProcessLock, Store, atomic_json, fsync_dir, now


def migrate(root: Path, role: str) -> dict:
    if role not in {"client", "server"}:
        raise ValueError("role 必须是 client 或 server")
    root = root.expanduser().absolute()
    source = root / f"{role}.sqlite"
    target = root / "state"
    if root.resolve() != root or source.is_symlink() or not source.is_file():
        raise ValueError("旧状态文件不存在或路径包含符号链接")
    lock = ProcessLock(root / f"{role}.lock")
    lock.acquire()
    staging = root / f".state-import-{uuid.uuid4()}"
    try:
        if target.exists() or target.is_symlink():
            raise FileExistsError("state 目录已存在；拒绝覆盖或重复导入")
        store = Store(staging)
        counts = {}
        connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        try:
            records = connection.execute(
                "SELECT kind, id, data FROM records ORDER BY rowid"
            )
            for kind, key, data in records:
                value = json.loads(data)
                store.put(kind, key, value)
                if store.get(kind, key) != value:
                    raise ValueError(f"迁移校验失败：{kind}/{key}")
                counts[kind] = counts.get(kind, 0) + 1
        finally:
            connection.close()
        for kind, count in counts.items():
            if len(store.all(kind)) != count:
                raise ValueError(f"迁移数量不一致：{kind}")
        report = {
            "version": 1,
            "source": source.name,
            "migrated_at": now(),
            "counts": counts,
        }
        atomic_json(staging / ".legacy-import.json", report)
        os.rename(staging, target)
        fsync_dir(root)
        return report
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        lock.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--role", choices=["client", "server"], required=True)
    args = parser.parse_args()
    print(json.dumps(migrate(args.root, args.role), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
