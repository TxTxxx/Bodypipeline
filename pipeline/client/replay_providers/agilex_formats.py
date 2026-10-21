"""Translate explicitly selected joint arrays for the existing ALOHA publisher.

This is a file-format adapter, not a motion controller. Values and units are
preserved; timestamped raw trajectories use the pipeline's causal-hold convention.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import h5py
import numpy as np

from pipeline.shared.core import DomainError, file_hash
from pipeline.shared.hdf5 import AGILEX_ALIGNED_SCHEMA, AGILEX_HDF5_SCHEMA

ARMS = ("masterLeft", "masterRight")
PUPPET_ARMS = ("puppetLeft", "puppetRight")
REPLAY_SCHEMA = "bodypipeline_agilex_tool_replay_v1"


def source_arms(trajectory_source):
    if trajectory_source not in {"action", "state"}:
        raise ValueError("trajectory_source 必须为 action 或 state")
    return PUPPET_ARMS if trajectory_source == "state" else ARMS


def raw_grid(handle, fps, arms=ARMS):
    times = []
    for arm in arms:
        group = handle[f"streams/arm/jointState/{arm}"]
        values = np.asarray(group["timestamps"], dtype=np.float64)
        if (
            values.ndim != 1
            or not len(values)
            or not np.isfinite(values).all()
            or np.any(np.diff(values) <= 0)
        ):
            raise ValueError(f"{arm} 时间戳无效")
        if group["position"].shape != (len(values), 7):
            raise ValueError(f"{arm} 关节数组应为 N × 7")
        times.append(values)
    start, end = max(t[0] for t in times), min(t[-1] for t in times)
    frames = math.floor((end - start) * fps + 1e-5) + 1
    if frames <= 0 or frames > 10_000_000:
        raise ValueError("左右臂时间范围没有有效公共区间")
    return times, float(start), frames


def describe(handle, output_fps=30, trajectory_source="action"):
    arms = source_arms(trajectory_source)
    label = " / ".join(arms)
    metadata = {"trajectory_source": trajectory_source, "source_arms": list(arms)}
    schema = handle.attrs.get("schema")
    if schema == AGILEX_HDF5_SCHEMA:
        fps = float(output_fps)
        if not math.isfinite(fps) or not 1 <= fps <= 240:
            raise ValueError("回放帧率无效")
        _, start, frames = raw_grid(handle, fps, arms)
        return {
            **metadata,
            "format": "bodypipeline_raw",
            "frames": frames,
            "fps": fps,
            "source_start": start,
            "description": f"平台采集包 · {label}"
            + (" 实测轨迹" if trajectory_source == "state" else " 指令轨迹"),
        }
    if schema == AGILEX_ALIGNED_SCHEMA:
        fps = float(handle.attrs["fps"])
        field = "observation.state" if trajectory_source == "state" else "action"
        shape = handle[field].shape
        if len(shape) != 2 or shape[0] <= 0 or shape[1] != 14:
            raise ValueError(f"{field} 应为 N × 14")
        if not math.isfinite(fps) or not 1 <= fps <= 240:
            raise ValueError("对齐数据缺少有效帧率")
        return {
            **metadata,
            "format": "bodypipeline_aligned",
            "frames": shape[0],
            "fps": fps,
            "description": f"平台对齐数据 · 使用 {field}",
        }
    frames = int(handle["size"][()])
    if frames <= 0:
        raise ValueError("size 必须大于零")
    for arm in arms:
        if handle[f"arm/jointStatePosition/{arm}"].shape != (frames, 7):
            raise ValueError(f"{arm} 应为 {frames} × 7")
    return {
        **metadata,
        "format": "aloha_hdf5",
        "frames": frames,
        "fps": 30,
        "description": f"ALOHA HDF5 · {label}",
    }


def prepare(path: Path, cache: Path, output_fps=30, trajectory_source="action"):
    arms = source_arms(trajectory_source)
    with h5py.File(path, "r") as source:
        info = describe(source, output_fps, trajectory_source)
    if info["format"] == "aloha_hdf5" and trajectory_source == "action":
        return path
    # Store only command arrays, not another copy of camera recordings.
    key = f"{file_hash(path)}-{info['fps']:g}-{trajectory_source}-v2"
    directory = cache / key
    target = directory / "episode.hdf5"
    if target.is_file():
        return target
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / "episode.tmp"
    try:
        with h5py.File(path, "r") as source, h5py.File(temporary, "w") as dest:
            dest.attrs["schema"] = REPLAY_SCHEMA
            dest.attrs["fps"] = info["fps"]
            dest.attrs["source_path"] = str(path)
            dest.attrs["source_format"] = info["format"]
            dest.attrs["trajectory_source"] = trajectory_source
            dest.attrs["source_arms"] = ",".join(arms)
            dest["size"] = info["frames"]
            if info["format"] == "bodypipeline_raw":
                times, start, frames = raw_grid(source, info["fps"], arms)
                grid = start + np.arange(frames) / info["fps"]
                for arm, output_arm, timestamps in zip(arms, ARMS, times, strict=True):
                    indices = np.searchsorted(timestamps, grid, side="right") - 1
                    values = source[f"streams/arm/jointState/{arm}/position"][:]
                    dest[f"arm/jointStatePosition/{output_arm}"] = values[indices]
                dest.attrs["source_start"] = start
                dest.attrs["resampling"] = "uniform grid; causal hold; no value scaling"
            elif info["format"] == "bodypipeline_aligned":
                field = (
                    "observation.state" if trajectory_source == "state" else "action"
                )
                action = source[field][:]
                for index, arm in enumerate(ARMS):
                    dest[f"arm/jointStatePosition/{arm}"] = action[
                        :, index * 7 : (index + 1) * 7
                    ]
            else:
                for arm, output_arm in zip(arms, ARMS, strict=True):
                    dest[f"arm/jointStatePosition/{output_arm}"] = source[
                        f"arm/jointStatePosition/{arm}"
                    ][:]
            dest.flush()
        os.replace(temporary, target)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        raise DomainError(f"回放格式适配失败：{exc}") from exc
    return target
