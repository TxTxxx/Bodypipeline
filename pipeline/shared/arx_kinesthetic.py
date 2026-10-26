"""Closed ARX kinesthetic episodes; preserve the next-measured-position labels."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

import h5py
import numpy as np

from .core import DomainError, digest, fsync_dir

FORMAT = "arx_kinesthetic_hdf5_v1"
SCHEMA = "bodypipeline.arx.kinesthetic.v1"


def validate(handle, profile, *, session_id=None):
    from .arx import ensure_self_contained

    ensure_self_contained(handle)
    if profile.get("robot_type") != "arx_x5" or profile.get("action_semantics", {}).get("representation") != "kinesthetic_next_measured_position":
        raise DomainError("纠偏 Profile 与 ARX 拖动标签不匹配")
    if handle.attrs.get("schema") != SCHEMA or handle.attrs.get("raw_format") != FORMAT or not handle.attrs.get("complete"):
        raise DomainError("纠偏 HDF5 尚未完整保存或格式不匹配")
    if handle.attrs.get("profile_hash") != digest(profile):
        raise DomainError("纠偏 HDF5 Profile 摘要不匹配")
    if session_id and handle.attrs.get("session_id") != session_id:
        raise DomainError("纠偏文件不属于当前 Episode")
    times = np.asarray(handle["timestamps/monotonic"])
    n = len(times)
    if n < 1 or times.ndim != 1 or not np.isfinite(times).all() or np.any(np.diff(times) <= 0):
        raise DomainError("纠偏时间轴无效")
    for field in ("observation.state", "action"):
        values = np.asarray(handle[field])
        if values.shape != (n, 14) or not np.isfinite(values).all():
            raise DomainError("纠偏关节数据维度或数值无效")
    if handle.attrs.get("timebase") != "recorded_monotonic":
        raise DomainError("纠偏文件必须声明实测时间轴")
    valid = np.asarray(handle["action_valid_mask"], dtype=bool)
    dt = np.asarray(handle["action_dt"])
    if valid.shape != (n,) or dt.shape != (n,) or valid[-1] or not np.isfinite(dt).all():
        raise DomainError("纠偏未来动作掩码或时间间隔无效")
    if not np.allclose(dt[:-1], np.diff(times)) or dt[-1] != 0:
        raise DomainError("纠偏动作时间间隔与实测时钟不一致")
    for camera in profile["cameras"]:
        if len(handle["observation.images." + camera["id"]]) != n:
            raise DomainError("纠偏图像与动作帧数不一致")
    return {"frames": n, "joint_dimensions": 14}


def package(raw: Path, output: Path, profile: dict, *, metadata=None):
    source = raw / "episode0/episode.hdf5"
    if source.is_symlink() or not source.is_file():
        raise DomainError("纠偏 Episode 文件不存在")
    with h5py.File(source, "r") as handle:
        report = validate(handle, profile, session_id=(metadata or {}).get("session_id"))
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        shutil.copyfile(source, temporary)
        with h5py.File(temporary, "r+") as handle:
            if "metadata" in handle:
                del handle["metadata"]
            handle.create_dataset("metadata", data=json.dumps({**(metadata or {}), "schema": SCHEMA,
                                                               "raw_format": FORMAT, "source": "dagger"}),
                                  dtype=h5py.string_dtype())
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        temporary.replace(output)
        fsync_dir(output.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return {**report, "size_bytes": output.stat().st_size}
