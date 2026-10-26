"""Translate an ARX execution backend's status into Client job progress.

This process owns only its replay request. It never constructs an SDK, kills a
competing controller, disables motors or shuts down the persistent ROS services.
"""

from __future__ import annotations

import argparse
import importlib
import math
import signal
import sys
import threading
import time
import uuid
from pathlib import Path

REPLAY_SCHEMA = "bodypipeline.arx.replay.v1"


def run_replay(
    backend,
    values,
    fps,
    speed,
    alignment_duration,
    *,
    stop=None,
    emit=print,
    now=time.monotonic,
    wait=None,
):
    stop = stop or threading.Event()
    wait = wait or stop.wait
    owner = "bodypipeline-" + uuid.uuid4().hex
    sent_start = False
    completed = False
    next_frame = 0
    last_phase = None
    frames = len(values)
    try:
        emit("ARX_STATUS 连接回放执行后端，等待双臂反馈", flush=True)
        if stop.is_set():
            return 0
        backend.start_services()
        deadline = now() + 15
        while not stop.is_set():
            status = backend.status()
            if status.get("feedback_fresh"):
                break
            if now() >= deadline:
                raise RuntimeError("等待双臂反馈超时")
            wait(0.1)
        if stop.is_set():
            return 0
        # Mark before sending: a lost response may still have started motion.
        sent_start = True
        backend.request(
            "/start",
            {
                "owner": owner,
                "values": values.tolist(),
                "fps": fps,
                "speed": speed,
                "duration": alignment_duration,
            },
        )
        while not stop.is_set():
            status = backend.request("/heartbeat", {"owner": owner})
            phase, result = status.get("state"), status.get("result")
            if phase != last_phase:
                emit("ARX_STATUS " + str(status.get("message", phase)), flush=True)
                last_phase = phase
            if result in {"failed", "stopped"}:
                raise RuntimeError(status.get("message") or "回放工具停止")
            frame = status.get("frame", 0)
            if type(frame) is not int or not 0 <= frame < frames:
                raise RuntimeError("ARX 回放工具返回了无效帧序号")
            if result == "completed":
                if frame != frames - 1:
                    raise RuntimeError("ARX 工具在末帧前报告完成")
                # Set before emitting the final frame: ReplayService may SIGINT us
                # immediately after it sees completion. Leave the tool holding.
                completed = True
                while next_frame < frames:
                    emit(f"ARX_FRAME {next_frame}", flush=True)
                    next_frame += 1
                return 0
            if phase in {"replaying", "finishing"}:
                # The tool interpolates at its own control rate. Normalize its
                # trajectory progress; reserve the final marker for completion.
                while next_frame <= min(frame, frames - 2):
                    emit(f"ARX_FRAME {next_frame}", flush=True)
                    next_frame += 1
            wait(0.2)
        return 0
    except Exception as exc:
        emit("ARX_ERROR " + str(exc).replace("\n", " "), flush=True)
        return 1
    finally:
        if sent_start and not completed:
            try:
                backend.request("/stop", {"owner": owner})
            except Exception as exc:
                emit(
                    "ARX_ERROR 停止请求未确认，依赖已有工具的心跳超时停止推进目标："
                    + str(exc).replace("\n", " "),
                    flush=True,
                )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool-root", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--speed", type=float, default=1)
    parser.add_argument("--alignment-duration", type=float, default=5)
    parser.add_argument(
        "--backend",
        choices=("installed_tool", "capture_bridge"),
        default="installed_tool",
    )
    parser.add_argument("--bridge-url", default="http://127.0.0.1:8091")
    args = parser.parse_args()
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    try:
        import h5py
        import numpy as np

        with h5py.File(args.dataset, "r") as handle:
            if handle.attrs.get("schema") != REPLAY_SCHEMA:
                raise ValueError("回放准备文件 schema 不匹配")
            values = np.asarray(handle["positions"], dtype=float)
            fps = float(handle.attrs["fps"])
        if (
            values.ndim != 2
            or values.shape[1] != 14
            or not len(values)
            or not np.isfinite(values).all()
        ):
            raise ValueError("回放位置数据无效")
        if (
            not math.isfinite(fps)
            or not 1 <= fps <= 240
            or not 0 < args.speed <= 1
            or not 0 < args.alignment_duration <= 30
        ):
            raise ValueError("回放时间参数无效")
        if args.backend == "capture_bridge":
            if __package__:
                from .arx_bridge_backend import CaptureBridgeBackend
            else:
                from arx_bridge_backend import CaptureBridgeBackend
            backend = CaptureBridgeBackend(args.bridge_url)
        else:
            root = args.tool_root.resolve()
            sys.path.insert(0, str(root))
            backend = importlib.import_module("ros_client")
            if Path(backend.__file__).resolve() != root / "ros_client.py":
                raise ValueError("导入的 ARX 回放工具路径不匹配")
        return run_replay(
            backend, values, fps, args.speed, args.alignment_duration, stop=stop
        )
    except Exception as exc:
        print("ARX_ERROR " + str(exc).replace("\n", " "), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
