"""ARX trajectory preparation and dispatch to a configured execution backend."""

from __future__ import annotations

import math
import re
from pathlib import Path

from pipeline.shared.core import DomainError

from . import arx_formats
from .arx_bridge_backend import bridge_address
from .base import ReplayCapabilities


class ArxReplayProvider:
    capabilities = ReplayCapabilities(
        id="arx_tool",
        version="1",
        label="ARX X5 双臂轨迹回放",
        robot_types=("arx_x5",),
        file_extensions=(".hdf5", ".h5"),
        input_description="已保留的 ARX 采集与对齐 HDF5，使用实测双臂位置及夹爪",
        progress_timeout=90,
    )

    def __init__(self, config, profile):
        self.config, self.profile = config, profile
        self.tool_root = Path(config.get("tool_root", "/opt/arx-data-studio"))
        self.python = Path(
            config.get(
                "python", "/opt/ROS2_AC-one_Play/tools/.collect_venv/bin/python"
            )
        )
        self.speed = float(config.get("speed", 1))
        self.alignment_duration = float(config.get("alignment_duration", 5))
        self.legacy_fps = config.get("legacy_fps")
        self.backend = config.get("backend", "installed_tool")
        if self.backend not in {"installed_tool", "capture_bridge"}:
            raise ValueError("未知 ARX 回放 backend")
        self.bridge_url = config.get("bridge_url", "http://127.0.0.1:8091")
        if self.backend == "capture_bridge":
            bridge_address(self.bridge_url)
        for value, upper, label in (
            (self.speed, 1, "speed"),
            (self.alignment_duration, 30, "alignment_duration"),
        ):
            if not math.isfinite(value) or not 0 < value <= upper:
                raise ValueError(f"ARX replay.{label} 必须在 (0, {upper}] 内")

    def probe(self, path):
        try:
            _, info = arx_formats.trajectory(path, self.profile, self.legacy_fps)
            return {"supported": True, "error": "", **info}
        except (OSError, KeyError, ValueError, TypeError, DomainError) as exc:
            return {"supported": False, "frames": 0, "error": str(exc)}

    def prepare(self, path, cache):
        return arx_formats.prepare(path, cache, self.profile, self.legacy_fps)

    def command(self, path):
        if (
            self.backend == "installed_tool"
            and not (self.tool_root / "ros_client.py").is_file()
        ):
            raise DomainError(f"找不到已安装的 ARX 回放工具：{self.tool_root}", 503)
        if not self.python.is_file():
            raise DomainError(f"找不到 ARX 回放 Python 环境：{self.python}", 503)
        command = [
            "bash",
            str(Path(__file__).with_name("run_arx.sh")),
            str(self.python),
            "--tool-root",
            str(self.tool_root),
            "--dataset",
            str(path),
            "--speed",
            str(self.speed),
            "--alignment-duration",
            str(self.alignment_duration),
        ]
        if self.backend == "capture_bridge":
            command += ["--backend", "capture_bridge", "--bridge-url", self.bridge_url]
        return command

    @staticmethod
    def frame(line):
        match = re.fullmatch(r"ARX_FRAME (\d+)", line.strip())
        return int(match[1]) if match else None

    @staticmethod
    def error(line):
        if line.startswith("ARX_ERROR "):
            return line[len("ARX_ERROR ") :]
        if "Traceback" in line:
            return "ARX 回放工具异常，详见日志"
        return None
