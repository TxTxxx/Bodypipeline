"""Persistent, bounded, read-only scoring for registered Viewer datasets."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pipeline.shared.core import DomainError, executable_path

SCORING_VERSION = "doctor-ff947533-vector-shards-v1"


def dataset_signature(root: Path) -> str:
    paths = [root / "meta/info.json", *sorted((root / "data").rglob("*.parquet"))]
    entries = []
    for path in paths:
        if any(p.is_symlink() for p in [path, *path.parents]):
            raise DomainError("评分输入不允许包含符号链接", 400)
        stat = path.stat()
        entries.append(
            (str(path.relative_to(root)), stat.st_ino, stat.st_size, stat.st_mtime_ns)
        )
    return hashlib.sha256(json.dumps([SCORING_VERSION, entries]).encode()).hexdigest()


class EpisodeScoreCache:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="episode-score"
        )
        self.lock = threading.RLock()
        self.pending = {}
        self.stopping = threading.Event()

    def close(self):
        self.stopping.set()
        self.pool.shutdown(wait=True, cancel_futures=True)

    def _identity(self, preview):
        root = Path(preview["dataset_path"])
        key = hashlib.sha256(str(root).encode()).hexdigest()
        return root, key, dataset_signature(root)

    def status(self, preview):
        with self.lock:
            _, key, signature = self._identity(preview)
            try:
                result = self.store.get("episode-score", key)
            except DomainError as exc:
                if exc.code != 404:
                    raise
                result = {}
            if result.get("signature") != signature or (
                result.get("state") in {"QUEUED", "RUNNING"}
                and (key not in self.pending or self.pending[key].done())
            ):
                return {"state": "NONE"}
            return result or {"state": "NONE"}

    def start(self, preview):
        with self.lock:
            root, key, signature = self._identity(preview)
            current = self.status(preview)
            if current["state"] in {"QUEUED", "RUNNING", "COMPLETED"}:
                return current
            if key in self.pending and not self.pending[key].done():
                return {"state": "RUNNING"}
            tool = executable_path(self.settings.letools_bin)
            python = Path(tool).with_name("python") if tool else None
            if python is None or not python.is_file() or not os.access(python, os.X_OK):
                raise DomainError("未找到 LeTools Python，无法运行 Episode 评分", 503)
            result = {
                "state": "QUEUED",
                "signature": signature,
                "created_at": time.time(),
            }
            self.store.put("episode-score", key, result)
            self.pending[key] = self.pool.submit(
                self._run, root, key, signature, str(python)
            )
            return result

    def _run(self, root, key, signature, python):
        result = {"state": "RUNNING", "signature": signature, "created_at": time.time()}
        self.store.put("episode-score", key, result)
        try:
            worker = Path(__file__).with_name("episode_score_worker.py")
            with subprocess.Popen(
                [python, str(worker), str(root)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.settings.tool_environment(),
            ) as process:
                try:
                    deadline = time.monotonic() + self.settings.command_timeout
                    while True:
                        if self.stopping.is_set():
                            raise InterruptedError("Viewer 停止；可重新计算评分")
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Episode 评分超时，可重试")
                        try:
                            output, error = process.communicate(timeout=0.5)
                            break
                        except subprocess.TimeoutExpired:
                            pass
                except BaseException:
                    process.kill()
                    process.communicate()
                    raise
            if process.returncode:
                raise ValueError(error[-1200:] or "评分程序执行失败")
            report = json.loads(output)
            json.dumps(report, allow_nan=False)
            rows = report["episodes"]
            total = report["total_episodes"]
            if len(rows) != total or [r["episode_index"] for r in rows] != list(
                range(total)
            ):
                raise ValueError("评分结果未覆盖全部 Episode")
            if dataset_signature(root) != signature:
                raise ValueError("评分期间数据集发生变化，请重新计算")
            result.update(state="COMPLETED", report=report)
        except Exception as exc:
            result.update(state="FAILED", error=str(exc))
        result["finished_at"] = time.time()
        self.store.put("episode-score", key, result)
