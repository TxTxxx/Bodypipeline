"""HDF5 capture packaging shared by the Client and Server.

The v1 AgileX package keeps the recorder's timestamped streams intact. The
Server performs the uniform-grid alignment and emits a second, Letools-native
HDF5 file. No uploaded raw directory is extracted or reconstructed.
"""

from __future__ import annotations

import io
import json
import math
import os
from pathlib import Path
from typing import Any

import h5py
import numpy as np
from PIL import Image

from pipeline.shared.core import DomainError, fsync_dir, now

AGILEX_HDF5_FORMAT = "agilex_joint_camera_hdf5_v1"
AGILEX_HDF5_SCHEMA = "bodypipeline.agilex.raw_hdf5.v1"
AGILEX_ALIGNED_SCHEMA = "bodypipeline.agilex.aligned_hdf5.v1"
AGILEX_HDF5_FILENAME = "episode.hdf5"
LEGACY_AGILEX_RAW_FORMAT = "agilex_timestamped_directory_v1"

_CAMERA_GROUP = "streams/camera"
_JOINT_GROUP = "streams/arm/jointState"
_UINT8_VLEN = h5py.vlen_dtype(np.dtype("uint8"))


def _timestamped_files(path: Path, suffix: str) -> tuple[list[float], list[Path]]:
    files = list(path.glob(f"*{suffix}"))
    rows: list[tuple[float, Path]] = []
    for file in files:
        try:
            timestamp = float(file.stem)
        except ValueError as exc:
            raise DomainError(f"无效时间戳文件名：{file.name}") from exc
        if not math.isfinite(timestamp) or file.is_symlink():
            raise DomainError("采集流包含非法时间戳或符号链接")
        rows.append((timestamp, file))
    rows.sort(key=lambda item: item[0])
    if not rows:
        raise DomainError(f"采集流为空：{path}")
    timestamps = [row[0] for row in rows]
    if len(set(timestamps)) != len(timestamps):
        raise DomainError(f"采集流存在重复时间戳：{path}")
    return timestamps, [row[1] for row in rows]


def _image_payload(path: Path) -> tuple[bytes, tuple[int, int]]:
    data = path.read_bytes()
    try:
        with Image.open(io.BytesIO(data)) as image:
            size = image.size
            image.verify()
    except Exception as exc:
        raise DomainError(f"相机图像损坏：{path.name}") from exc
    return data, size


def _joint_positions(path: Path, dimensions_per_role: int) -> list[float]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        values = payload["position"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise DomainError(f"关节 JSON 无法读取：{path.name}") from exc
    if not isinstance(values, list) or len(values) != dimensions_per_role:
        raise DomainError(f"关节维度不匹配：{path.name} 需要 {dimensions_per_role} 维")
    result = [float(value) for value in values]
    if not all(math.isfinite(value) for value in result):
        raise DomainError(f"关节包含非有限值：{path.name}")
    return result


def _profile_dimensions(profile: dict) -> int:
    cameras = [
        item.get("id") for item in profile.get("cameras", []) if isinstance(item, dict)
    ]
    if cameras != ["left", "front", "right"]:
        raise DomainError("AgileX HDF5 打包需要 left / front / right 三路相机")
    names = profile.get("joint_names")
    if isinstance(names, list) and names:
        if len(names) % 2:
            raise DomainError("AgileX joint_names 必须由左右两组等长关节组成")
        return len(names) // 2
    # The legacy draft profile deliberately leaves semantic names unapproved.
    # AgileX still has a fixed 7-DoF-per-arm recorder contract.
    return 7


def package_agilex_capture(
    raw: Path,
    output: Path,
    profile: dict,
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Package one raw AgileX directory into a self-contained HDF5 file.

    Numeric samples are stored with their original timestamps. JPEGs remain
    encoded inside variable-length HDF5 datasets, so the Server can align and
    convert them without reading loose files from the uploaded payload.
    """

    raw = raw.resolve()
    episode = raw / "episode0"
    if not episode.is_dir() or episode.is_symlink():
        raise DomainError("原始采集缺少 episode0 目录")
    dimensions_per_role = _profile_dimensions(profile)
    roles = list(profile["state_streams"]) + list(profile["action_streams"])
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    if temporary.exists():
        temporary.unlink()

    package_meta = {
        "schema": AGILEX_HDF5_SCHEMA,
        "raw_format": AGILEX_HDF5_FORMAT,
        "robot_type": profile.get("robot_type"),
        "profile_id": profile.get("id"),
        "instruction": (metadata or {}).get("instruction", ""),
        "created_at": now(),
        **{
            key: value
            for key, value in (metadata or {}).items()
            if key != "instruction"
        },
    }
    camera_stats: dict[str, dict[str, Any]] = {}
    joint_stats: dict[str, dict[str, Any]] = {}
    try:
        with h5py.File(temporary, "w") as handle:
            handle.attrs["schema"] = AGILEX_HDF5_SCHEMA
            handle.attrs["raw_format"] = AGILEX_HDF5_FORMAT
            handle.attrs["robot_type"] = str(profile.get("robot_type", ""))
            handle.create_dataset(
                "metadata",
                data=json.dumps(package_meta, ensure_ascii=False),
                dtype=h5py.string_dtype(encoding="utf-8"),
            )
            for camera in profile["cameras"]:
                camera_id = camera["id"]
                timestamps, paths = _timestamped_files(
                    episode / "camera/color" / camera_id, ".jpg"
                )
                group = handle.create_group(f"{_CAMERA_GROUP}/{camera_id}")
                group.attrs["encoded_format"] = "jpeg"
                group.create_dataset(
                    "timestamps",
                    data=np.asarray(timestamps, dtype=np.float64),
                    compression="gzip",
                    shuffle=True,
                )
                encoded = group.create_dataset(
                    "jpeg", shape=(len(paths),), dtype=_UINT8_VLEN
                )
                size = None
                for index, path in enumerate(paths):
                    data, image_size = _image_payload(path)
                    if size is None:
                        size = image_size
                    elif image_size != size:
                        raise DomainError(
                            f"相机 {camera_id} 分辨率不一致：{size} vs {image_size}"
                        )
                    encoded[index] = np.frombuffer(data, dtype=np.uint8)
                if size is None:
                    raise DomainError(f"相机 {camera_id} 没有图像")
                group.attrs["width"], group.attrs["height"] = size
                camera_stats[camera_id] = {
                    "frames": len(paths),
                    "width": size[0],
                    "height": size[1],
                }
            for role in roles:
                timestamps, paths = _timestamped_files(
                    episode / "arm/jointState" / role, ".json"
                )
                positions = np.asarray(
                    [_joint_positions(path, dimensions_per_role) for path in paths],
                    dtype=np.float32,
                )
                group = handle.create_group(f"{_JOINT_GROUP}/{role}")
                group.create_dataset(
                    "timestamps",
                    data=np.asarray(timestamps, dtype=np.float64),
                    compression="gzip",
                    shuffle=True,
                )
                group.create_dataset(
                    "position",
                    data=positions,
                    compression="gzip",
                    shuffle=True,
                )
                joint_stats[role] = {
                    "frames": len(paths),
                    "dimensions": dimensions_per_role,
                }
            handle.flush()
        with temporary.open("rb") as file:
            os.fsync(file.fileno())
        os.replace(temporary, output)
        fsync_dir(output.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    return {
        "schema": AGILEX_HDF5_SCHEMA,
        "path": output.name,
        "size_bytes": output.stat().st_size,
        "cameras": camera_stats,
        "joint_streams": joint_stats,
        "joint_dimensions": dimensions_per_role * 2,
    }


def read_package_metadata(path: Path) -> dict[str, Any]:
    """Read and validate the small metadata record from one raw package."""

    try:
        with h5py.File(path, "r") as handle:
            if handle.attrs.get("schema") != AGILEX_HDF5_SCHEMA:
                raise DomainError("HDF5 不是受支持的 AgileX 原始采集包")
            value = handle["metadata"][()]
            if isinstance(value, bytes):
                value = value.decode("utf-8")
            result = json.loads(str(value))
    except DomainError:
        raise
    except (OSError, KeyError, ValueError, h5py.H5Error) as exc:
        raise DomainError(f"HDF5 采集包无法读取：{path.name}") from exc
    if not isinstance(result, dict) or result.get("raw_format") != AGILEX_HDF5_FORMAT:
        raise DomainError("HDF5 采集包元数据无效")
    return result


def is_hdf5_path(path: Path) -> bool:
    return path.suffix.lower() in {".hdf5", ".h5"}


def find_hdf5_capture(raw: Path) -> Path | None:
    if raw.is_file() and is_hdf5_path(raw):
        return raw
    if raw.is_dir():
        candidates = sorted(
            item
            for item in raw.iterdir()
            if item.is_file() and not item.is_symlink() and is_hdf5_path(item)
        )
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise DomainError("单个采集包中只允许一个 HDF5 文件")
    return None


__all__ = [
    "AGILEX_ALIGNED_SCHEMA",
    "AGILEX_HDF5_FILENAME",
    "AGILEX_HDF5_FORMAT",
    "AGILEX_HDF5_SCHEMA",
    "LEGACY_AGILEX_RAW_FORMAT",
    "find_hdf5_capture",
    "is_hdf5_path",
    "package_agilex_capture",
    "read_package_metadata",
]
