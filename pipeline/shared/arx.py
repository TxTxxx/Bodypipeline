"""ARX direct-teaching storage contract; no controller or ROS dependencies."""

from __future__ import annotations

import io
import json
import math
import os
import shutil
from pathlib import Path

import h5py
import numpy as np
from PIL import Image

from .capture_formats import ARX_HDF5_FORMAT, ARX_RAW_SCHEMA, CAPTURE_FILENAME
from .core import DomainError, digest, fsync_dir

CAMERAS = ("head", "left_wrist", "right_wrist")
SOURCES = (*CAMERAS, "left_arm", "right_arm")
DIMENSIONS = 14


def capture_contract_hash(profile):
    """Approval evidence can advance without changing recorded data semantics."""
    return digest(
        {
            key: value
            for key, value in profile.items()
            if key
            not in {"semantics_approved", "clock_verified", "verification", "notes"}
        }
    )


def validate_profile(profile):
    if (
        profile.get("robot_type") != "arx_x5"
        or profile.get("control_mode") != "teleoperation"
    ):
        raise DomainError("ARX Provider 仅接受 arx_x5 的示教 Profile")
    if [c.get("id") for c in profile.get("cameras", [])] != list(CAMERAS):
        raise DomainError("当前 ARX 采集脚本需要 head / left_wrist / right_wrist")
    if (
        profile.get("action_semantics", {}).get("representation")
        != "measured_joint_position_as_demonstration_target"
    ):
        raise DomainError("ARX action 必须明确声明为直接示教实测位置目标")
    if len(profile.get("joint_names", [])) != DIMENSIONS:
        raise DomainError("ARX 双臂需要 14 维 joint_names")
    if (
        type(profile.get("output_fps")) is not int
        or not 1 <= profile["output_fps"] <= 240
    ):
        raise DomainError("ARX output_fps 无效")
    for key in ("max_frame_skew_ms", "max_sensor_skew_ms"):
        value = profile.get(key)
        if (
            not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 < value <= 1000
        ):
            raise DomainError(f"ARX {key} 无效")
    if type(profile.get("minimum_frames")) is not int or profile["minimum_frames"] < 1:
        raise DomainError("ARX minimum_frames 无效")


def jpeg_size(data):
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "JPEG" or image.mode != "RGB":
                raise ValueError("requires RGB JPEG")
            size = image.size
            image.load()
            return size
    except Exception as exc:
        raise DomainError("ARX 相机必须提供完整 RGB JPEG") from exc


def ensure_self_contained(group, seen=None):
    seen = set() if seen is None else seen
    address = h5py.h5o.get_info(group.id).addr
    if address in seen:
        raise DomainError("HDF5 不允许循环或别名链接")
    seen.add(address)
    for key in group:
        if not isinstance(group.get(key, getlink=True), h5py.HardLink):
            raise DomainError("HDF5 不允许外部或软链接")
        item = group[key]
        if isinstance(item, h5py.Group):
            ensure_self_contained(item, seen)
        elif item.is_virtual or item.external:
            raise DomainError("HDF5 必须为自包含数据")


def validate_capture(handle, profile, *, require_profile_hash=True):
    validate_profile(profile)
    ensure_self_contained(handle)
    if (
        handle.attrs.get("schema") != ARX_RAW_SCHEMA
        or handle.attrs.get("raw_format") != ARX_HDF5_FORMAT
    ):
        raise DomainError("ARX HDF5 schema 不匹配；旧文件需显式迁移，不能猜测时间戳")
    if not bool(handle.attrs.get("complete", False)):
        raise DomainError("ARX writer 尚未确认完整结束")
    if handle.attrs.get("capture_contract_hash") != capture_contract_hash(profile) or (
        require_profile_hash and handle.attrs.get("profile_hash") != digest(profile)
    ):
        raise DomainError("ARX HDF5 Profile 摘要不匹配")
    if handle.attrs.get("fps") != profile["output_fps"]:
        raise DomainError("ARX HDF5 采样频率与 Profile 不一致")
    times = np.asarray(handle["timestamps/monotonic"], dtype=np.float64)
    n = len(times)
    if (
        times.ndim != 1
        or n < profile["minimum_frames"]
        or not np.isfinite(times).all()
        or np.any(np.diff(times) <= 0)
    ):
        raise DomainError("ARX 帧时间戳无效或有效帧数不足")
    for key in (
        "observations/qpos",
        "observations/qvel",
        "observations/effort",
        "observations/eef",
        "action",
        "action_eef",
    ):
        dataset = handle[key]
        if dataset.shape != (n, DIMENSIONS):
            raise DomainError(f"ARX 字段维度或帧数不匹配：{key}")
        for start in range(0, n, 1024):
            if not np.isfinite(dataset[start : start + 1024]).all():
                raise DomainError(f"ARX 字段包含非有限数值：{key}")
    for start in range(0, n, 1024):
        for action, state in (
            ("action", "observations/qpos"),
            ("action_eef", "observations/eef"),
        ):
            if not np.array_equal(
                handle[action][start : start + 1024],
                handle[state][start : start + 1024],
            ):
                raise DomainError("ARX action 与直接示教实测轨迹契约不一致")
    source_times = []
    raw_first = handle.attrs.get("recording_mode") == "raw_first"
    for name in SOURCES:
        values = np.asarray(handle[f"timestamps/source/{name}"], dtype=np.float64)
        if (
            values.shape != (n,)
            or not np.isfinite(values).all()
            or np.any(values <= 0)
            or np.any(np.diff(values) < 0 if raw_first else np.diff(values) <= 0)
        ):
            raise DomainError(f"ARX 传感器时间戳无效或重复：{name}")
        source_times.append(values)
    cameras = {}
    for name in CAMERAS:
        dataset = handle[f"observations/images/{name}"]
        if dataset.ndim not in (1, 2) or len(dataset) != n:
            raise DomainError(f"ARX 相机帧数不一致：{name}")
        size = None
        for row in dataset:
            current = jpeg_size(np.asarray(row, dtype=np.uint8).tobytes())
            if size is not None and size != current:
                raise DomainError(f"ARX 相机分辨率发生变化：{name}")
            size = current
        cameras[name] = {"frames": n, "width": size[0], "height": size[1]}
    report = {
        "frames": n,
        "timestamps": times,
        "source_times": np.stack(source_times),
        "cameras": cameras,
    }
    if raw_first:
        if handle.attrs.get("alignment_clock") != "source":
            raise DomainError("raw_first 文件未声明源时间轴")
        reference = np.asarray(handle["timestamps/reference_source_ns"])
        exact = np.asarray(handle["timestamps/source_ns"])
        received = np.asarray(handle["timestamps/received_monotonic_ns"])
        sample_ids = np.asarray(handle["source_sample_id"])
        if (
            reference.dtype.kind != "i"
            or reference.shape != (n,)
            or np.any(reference <= 0)
            or np.any(np.diff(reference) <= 0)
        ):
            raise DomainError("raw_first 参考相机时间戳无效")
        for key, values in (
            ("source_ns", exact),
            ("received_monotonic_ns", received),
            ("source_sample_id", sample_ids),
        ):
            if (
                values.dtype.kind != "i"
                or values.shape != (n, len(SOURCES))
                or np.any(values <= 0)
                or np.any(np.diff(values, axis=0) < 0)
            ):
                raise DomainError(f"raw_first 原始消息时间戳或索引无效：{key}")
        if not np.array_equal(exact[:, 0], reference):
            raise DomainError("raw_first 源时间轴与参考相机不一致")
        if not np.allclose(
            exact.astype(np.float64) / 1e9,
            np.stack(source_times, axis=1),
            rtol=0,
            atol=1e-6,
        ):
            raise DomainError("raw_first 精确时间戳与兼容字段不一致")
        report["alignment_timestamps"] = (reference - reference[0]).astype(
            np.float64
        ) / 1e9
        report["sensor_skew_ms"] = np.ptp(exact, axis=1).astype(np.float64) / 1e6
    return report


class ArxWriter:
    """Incremental single-file recorder, retaining the upstream ACT field names."""

    def __init__(self, path: Path, profile: dict, session_id: str, instruction: str):
        validate_profile(profile)
        self.path = path
        self.profile = profile
        self.count = 0
        self.last_time = None
        self.sizes = {}
        self.handle = h5py.File(path, "x")
        h = self.handle
        h.attrs.update(
            schema=ARX_RAW_SCHEMA,
            raw_format=ARX_HDF5_FORMAT,
            complete=False,
            fps=profile["output_fps"],
            profile_hash=digest(profile),
            capture_contract_hash=capture_contract_hash(profile),
            session_id=session_id,
            task=instruction,
            sim=False,
        )
        for key in (
            "observations/qpos",
            "observations/qvel",
            "observations/effort",
            "observations/eef",
            "action",
            "action_eef",
        ):
            h.create_dataset(
                key,
                shape=(0, DIMENSIONS),
                maxshape=(None, DIMENSIONS),
                dtype="float32",
                chunks=True,
            )
        for name in CAMERAS:
            h.create_dataset(
                f"observations/images/{name}",
                shape=(0,),
                maxshape=(None,),
                dtype=h5py.vlen_dtype(np.dtype("uint8")),
                chunks=True,
            )
        for key in (
            "timestamps/monotonic",
            *(f"timestamps/source/{name}" for name in SOURCES),
        ):
            h.create_dataset(
                key, shape=(0,), maxshape=(None,), dtype="float64", chunks=True
            )

    def append(self, sample):
        timestamp = float(sample["timestamp"])
        if not math.isfinite(timestamp) or (
            self.last_time is not None and timestamp <= self.last_time
        ):
            raise DomainError("ARX 录制时间戳必须严格递增")
        values = {}
        for name in ("qpos", "qvel", "effort", "eef"):
            array = np.asarray(sample[name], dtype=np.float32)
            if array.shape != (DIMENSIONS,) or not np.isfinite(array).all():
                raise DomainError(f"ARX 观测字段必须为 14 维有限数值：{name}")
            values[f"observations/{name}"] = array
        values["action"] = values["observations/qpos"].copy()
        values["action_eef"] = values["observations/eef"].copy()
        values["timestamps/monotonic"] = timestamp
        for name in SOURCES:
            stamp = float(sample["source_timestamps"][name])
            if not math.isfinite(stamp) or stamp <= 0:
                raise DomainError(f"ARX 传感器缺少有效时间戳：{name}")
            values[f"timestamps/source/{name}"] = stamp
        for name in CAMERAS:
            data = sample["images"][name]
            size = jpeg_size(data)
            if name in self.sizes and self.sizes[name] != size:
                raise DomainError(f"ARX 相机分辨率发生变化：{name}")
            self.sizes[name] = size
            values[f"observations/images/{name}"] = np.frombuffer(data, dtype=np.uint8)
        for key, value in values.items():
            ds = self.handle[key]
            ds.resize(self.count + 1, axis=0)
            ds[self.count] = value
        self.count += 1
        self.last_time = timestamp

    def close(self, *, complete):
        if self.handle is None:
            return
        handle, self.handle = self.handle, None
        try:
            handle.attrs["complete"] = bool(complete and self.count)
            handle.attrs["frame_count"] = self.count
            handle.flush()
        finally:
            handle.close()
        with self.path.open("rb") as stream:
            os.fsync(stream.fileno())
        fsync_dir(self.path.parent)


def package_arx_capture(raw, output, profile, *, metadata=None):
    source = raw / "episode0" / CAPTURE_FILENAME
    if source.resolve() != source.absolute() or not source.is_file():
        raise DomainError("ARX 原始包缺失或包含符号链接")
    with h5py.File(source, "r") as handle:
        report = validate_capture(handle, profile)
        if (metadata or {}).get("session_id") != handle.attrs.get("session_id"):
            raise DomainError("ARX 采集文件不属于当前会话")
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        # Copy the closed recorder file; never mutate the original capture.
        with source.open("rb") as src, temporary.open("xb") as dst:
            shutil.copyfileobj(src, dst)
        with h5py.File(temporary, "r+") as handle:
            if "metadata" in handle:
                del handle["metadata"]
            handle.create_dataset(
                "metadata",
                data=json.dumps(
                    {
                        **(metadata or {}),
                        "schema": ARX_RAW_SCHEMA,
                        "raw_format": ARX_HDF5_FORMAT,
                    }
                ),
                dtype=h5py.string_dtype(),
            )
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        fsync_dir(output.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "schema": ARX_RAW_SCHEMA,
        "size_bytes": output.stat().st_size,
        "frames": report["frames"],
        "cameras": report["cameras"],
        "joint_dimensions": DIMENSIONS,
    }
