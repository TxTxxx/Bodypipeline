"""Generate a video only after an explicit download POST, and stream it locally."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from pipeline.shared.core import DomainError

from .adapters.registry import backend_for_record


class VideoDownload:
    def __init__(self, capture):
        self.capture = capture
        self.lock = threading.Lock()
        self.process = None
        self.log = None

    def start(self, cid):
        with self.lock, self.capture.lock:
            if self.process is not None:
                raise DomainError("已有视频正在生成或下载，请稍后再试", 409)
            row = self.capture.store.get("capture", cid)
            if row["state"] != "SAVED" or row.get("discard_requested") or row.get("capture_failed"):
                raise DomainError("只有已保存且未删除的片段可以下载视频", 409)
            if row.get("conversion_state") not in {None, "DONE"}:
                raise DomainError("请等待 HDF5 转换完成后下载视频", 409)
            backend = backend_for_record(row)
            if backend.video_source is None:
                raise DomainError("当前数据格式尚未配置视频导出", 409)
            candidates = [Path(row["raw_root"]) / "episode0" / backend.filename]
            candidates += [Path(row[key]) for key in ("published_path", "package_path") if row.get(key)]
            source = next((path for path in candidates if path.is_file()), None)
            if source is None:
                raise DomainError("本地 HDF5 不存在，无法生成视频", 404)
            if source.resolve() != source.absolute():
                raise DomainError("视频源文件不能包含符号链接", 409)
            public = row.get("published_path") or row.get("published_raw_path") or source
            filename = Path(public).stem + ".mp4"
            spec = {"source": str(source), "backend": backend.id,
                    "mode": row["mode"], "profile": row["profile"]}
            log = tempfile.TemporaryFile()
            try:
                process = subprocess.Popen(
                    [sys.executable, "-m", "pipeline.client.video_export", "--spec", json.dumps(spec)],
                    stdout=subprocess.PIPE, stderr=log, start_new_session=True,
                    env={**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
                         "MKL_NUM_THREADS": "1"},
                )
            except Exception:
                log.close()
                raise
            self.process = process
            self.log = log
        try:
            first = process.stdout.read1(64 * 1024)
            if not first:
                process.wait()
                log.seek(0)
                raise DomainError("视频生成失败：" + log.read()[-1500:].decode(errors="replace"), 422)
        except Exception:
            self._finish(process, log)
            raise

        async def content():
            try:
                yield first
                while chunk := await asyncio.to_thread(process.stdout.read1, 256 * 1024):
                    yield chunk
                if process.wait():
                    raise RuntimeError("视频生成失败，下载已中止")
            finally:
                self._finish(process, log)

        return filename, content(), lambda: self._finish(process, log)

    def _finish(self, process, log):
        with self.lock:
            if self.process is not process:
                return
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            process.stdout.close()
            log.close()
            self.process = self.log = None

    def close(self):
        with self.lock:
            process, log = self.process, self.log
        if process is not None:
            self._finish(process, log)
