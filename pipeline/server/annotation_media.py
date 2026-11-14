"""One lazy, bounded CPU decoder per app, shared by exact-frame fallback reads.

Browser-native video is the interactive path. This compatibility path reuses
the isolated LeTools interpreter, metadata and open decoders. Disconnected
requests leave the queue before dispatch; duplicate reads hit the same cache.
"""

from __future__ import annotations

import asyncio
import json
import select
import subprocess
from pathlib import Path

from pipeline.shared.core import DomainError, atomic_json, executable_path


class AnnotationMedia:
    def __init__(self, settings):
        self.settings = settings
        self.lock = asyncio.Lock()
        self.process = None

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        try:
            process.stdin.close()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait()
        finally:
            process.stdout.close()

    def _exchange(self, payload):
        if self.process is None or self.process.poll() is not None:
            self.close()
            tool = executable_path(self.settings.letools_bin)
            python = Path(tool).with_name("python") if tool else None
            if python is None or not python.is_file():
                raise DomainError("帧定位需要 LeTools 媒体环境", 503)
            self.process = subprocess.Popen(
                [
                    str(python),
                    str(Path(__file__).with_name("annotation_media_worker.py")),
                    "serve",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                env=self.settings.tool_environment(),
            )
        process = self.process
        try:
            process.stdin.write(json.dumps(payload) + "\n")
            process.stdin.flush()
            readable, _, _ = select.select(
                [process.stdout], [], [], self.settings.command_timeout
            )
            if not readable:
                raise TimeoutError("精确帧解码超时")
            value = json.loads(process.stdout.readline())
        except (OSError, ValueError, TimeoutError) as exc:
            self.close()
            raise DomainError("精确帧解码器不可用，请重试", 503) from exc
        if "error" in value:
            raise DomainError("无法对齐视频帧：" + value["error"], 422)
        return value["result"]

    def _read(self, payload, cache):
        if cache is None:
            return self._exchange(payload)
        meta = cache.with_suffix(".json")
        if cache.is_file() and meta.is_file():
            return json.loads(meta.read_text())
        temporary = cache.with_suffix(".partial")
        try:
            result = self._exchange({**payload, "output": str(temporary)})
            temporary.replace(cache)
            atomic_json(meta, result)
            return result
        finally:
            temporary.unlink(missing_ok=True)

    async def read(self, request, payload, cache=None):
        # A canceled native seek must not leave a waiting subprocess behind.
        # There is one dispatcher, with no process per camera or per pointermove.
        while True:
            if await request.is_disconnected():
                raise DomainError("取帧请求已取消", 499)
            try:
                await asyncio.wait_for(self.lock.acquire(), timeout=0.05)
                break
            except asyncio.TimeoutError:
                continue
        try:
            if await request.is_disconnected():
                raise DomainError("取帧请求已取消", 499)
            task = asyncio.create_task(asyncio.to_thread(self._read, payload, cache))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Drain the dispatched response before releasing the protocol
                # lock, so cancellation cannot misattribute the next reply.
                await task
                raise
        finally:
            self.lock.release()
