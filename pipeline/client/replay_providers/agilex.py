"""Reuse the installed ALOHA publisher without changing its control semantics."""

import os
import re
from pathlib import Path

import h5py

from pipeline.shared.core import DomainError

from . import agilex_formats as replay_formats
from .base import ReplayCapabilities


class AgileXReplayProvider:
    """The same command and frame protocol as ALOHA-Data-Tools.desktop."""

    capabilities = ReplayCapabilities(
        id="agilex_tool",
        version="1",
        label="AgileX ALOHA 回放工具",
        robot_types=("agilex_aloha",),
        file_extensions=(".hdf5", ".h5"),
        input_description="平台原始包、对齐 HDF5 和 ALOHA HDF5",
    )

    def __init__(self, config, profile=None):
        self.config = config
        self.output_fps = (profile or {}).get("output_fps", 30)
        self.trajectory_source = config.get("trajectory_source", "action")
        replay_formats.source_arms(self.trajectory_source)
        self.launcher = Path(
            config.get(
                "launcher", "/home/agilex/Data_Conversion_app/scripts/run_data_tool.sh"
            )
        )

    def probe(self, path):
        try:
            with h5py.File(path, "r") as handle:
                robot_type = handle.attrs.get("robot_type")
                if robot_type and robot_type not in self.capabilities.robot_types:
                    raise ValueError(
                        f"轨迹 robot_type={robot_type!r} 与 AgileX 回放不匹配"
                    )
                info = replay_formats.describe(
                    handle, self.output_fps, self.trajectory_source
                )
                return {
                    "supported": True,
                    "error": "",
                    **info,
                    "metadata": {
                        "trajectory_source": info["trajectory_source"],
                        "source_arms": info["source_arms"],
                    },
                }
        except (OSError, KeyError, ValueError, TypeError) as exc:
            return {"supported": False, "frames": 0, "error": str(exc)}

    def command(self, path):
        with h5py.File(path, "r") as handle:
            if handle.attrs.get("schema") == replay_formats.REPLAY_SCHEMA:
                robot_home = self.config.get("robot_home", "/home/agilex")
                tool = Path(
                    self.config.get(
                        "tool_script",
                        f"{robot_home}/data_ros/src/data_tools/scripts/data_publish.py",
                    )
                )
                if not tool.is_file():
                    raise DomainError(f"找不到原回放脚本：{tool}", 503)
                return [
                    "bash",
                    str(Path(__file__).with_name("run_agilex.sh")),
                    robot_home,
                    "--tool",
                    str(tool),
                    "--dataset",
                    str(path),
                    "--fps",
                    str(handle.attrs["fps"]),
                ]
        if not self.launcher.is_file() or not os.access(self.launcher, os.X_OK):
            raise DomainError(f"找不到可执行的回放工具：{self.launcher}", 503)
        return [str(self.launcher), "data_publish.py", "--datasetDir", str(path)]

    def prepare(self, path, cache):
        return replay_formats.prepare(
            path, cache, self.output_fps, self.trajectory_source
        )

    @staticmethod
    def frame(line):
        match = re.fullmatch(r"frame:\s*(\d+)", line.strip())
        return int(match[1]) if match else None

    @staticmethod
    def error(line):
        if "Traceback" in line or "Exception in thread" in line:
            return "回放工具发生异常，详见日志"
        return None
