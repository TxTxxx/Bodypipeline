"""Read ARX measured trajectories without modifying capture/training data."""

from __future__ import annotations

import math
import os
import uuid
from pathlib import Path

import h5py
import numpy as np

from pipeline.shared.arx import capture_contract_hash, ensure_self_contained
from pipeline.shared.capture_formats import ARX_ALIGNED_SCHEMA, ARX_RAW_SCHEMA
from pipeline.shared.core import DomainError, digest, file_hash, fsync_dir

REPLAY_SCHEMA = "bodypipeline.arx.replay.v1"


def trajectory(path, profile, legacy_fps=None):
    with h5py.File(path, "r") as handle:
        ensure_self_contained(handle)
        declared = handle.attrs.get("robot_type")
        if declared and declared != "arx_x5":
            raise DomainError("回放文件声明的机器人型号不是 arx_x5")
        schema = handle.attrs.get("schema")
        times = None
        if schema == ARX_RAW_SCHEMA:
            if not bool(handle.attrs.get("complete", False)):
                raise DomainError("ARX 采集文件尚未写完")
            if handle.attrs.get("capture_contract_hash") != capture_contract_hash(
                profile
            ):
                raise DomainError("ARX 轨迹与当前机器人数据契约不匹配")
            field = "observations/qpos"
            times = np.asarray(handle["timestamps/monotonic"], dtype=np.float64)
            fps = handle.attrs.get("fps")
            kind = "arx_raw"
        elif schema == ARX_ALIGNED_SCHEMA:
            field, fps, kind = (
                "observation.state",
                handle.attrs.get("fps"),
                "arx_aligned",
            )
        elif schema is None and "observations/qpos" in handle:
            field = "observations/qpos"
            fps = handle.attrs.get("fps", legacy_fps)
            kind = "arx_legacy"
        else:
            raise DomainError("不是支持的 ARX 采集或对齐 HDF5")
        dataset = handle[field]
        if (
            dataset.ndim != 2
            or dataset.shape[1] != 14
            or not 1 <= len(dataset) <= 1_000_000
        ):
            raise DomainError("ARX 回放需要非空 N×14 双臂位置轨迹")
        values = np.asarray(dataset, dtype=np.float64)
        if not np.isfinite(values).all():
            raise DomainError("ARX 回放轨迹包含非有限数值")
        if fps is None:
            raise DomainError(
                "旧 ARX 文件未记录 FPS；请在部署配置中设置 replay.legacy_fps"
            )
        fps = float(fps)
        if not math.isfinite(fps) or not 1 <= fps <= 240:
            raise DomainError("ARX 回放 FPS 必须在 1–240 之间")
        if times is not None:
            if (
                times.shape != (len(values),)
                or not np.isfinite(times).all()
                or np.any(np.diff(times) <= 0)
            ):
                raise DomainError("ARX 回放时间戳必须与位置等长且严格递增")
            relative = times - times[0]
            # The existing tool accepts fixed-rate samples. Preserve both ends;
            # the last hold can extend the source duration by less than one tick.
            count = math.ceil(float(relative[-1]) * fps - 1e-6) + 1
            if not 1 <= count <= 1_000_000:
                raise DomainError("ARX 回放时间跨度无效或过长")
            grid = np.minimum(np.arange(count) / fps, relative[-1])
            positions = np.column_stack(
                [np.interp(grid, relative, values[:, axis]) for axis in range(14)]
            )
            timing = "recorded_monotonic_resampled"
        else:
            positions = values
            timing = "file_fps" if "fps" in handle.attrs else "configured_legacy_fps"
        info = {
            "frames": len(positions),
            "fps": fps,
            "format": kind,
            "description": "ARX 双臂实测轨迹 · 左右各 6 关节与夹爪",
            "metadata": {
                "trajectory_source": "state",
                "source_field": field,
                "source_frames": len(values),
                "timing": timing,
                "joint_names": list(profile.get("joint_names", [])),
            },
        }
        return positions, info


def prepare(path: Path, cache: Path, profile: dict, legacy_fps=None):
    original_hash = file_hash(path)
    values, info = trajectory(path, profile, legacy_fps)
    key = digest(
        {
            "source": original_hash,
            "contract": capture_contract_hash(profile),
            "legacy_fps": legacy_fps,
            "schema": REPLAY_SCHEMA,
        }
    )
    directory = cache / key
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "trajectory.hdf5"
    # Rewrite atomically: a stale/tampered cache must never supply motion values.
    temporary = directory / f".{uuid.uuid4().hex}.tmp"
    try:
        with h5py.File(temporary, "x") as handle:
            handle.attrs.update(
                schema=REPLAY_SCHEMA,
                robot_type="arx_x5",
                fps=info["fps"],
                source_sha256=original_hash,
                source_field=info["metadata"]["source_field"],
            )
            handle.create_dataset("positions", data=values)
        if file_hash(path) != original_hash:
            raise DomainError("回放准备期间原始文件发生变化")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        fsync_dir(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return target
