"""Timestamp-aware conversion for the grus ARX direct-teaching recorder."""

from __future__ import annotations

import io
import json
import math
import os
from pathlib import Path

import h5py
import numpy as np
from PIL import Image

from pipeline.shared.core import DomainError, digest
from pipeline.shared.hdf5 import AGILEX_ALIGNED_SCHEMA

from ..conversion_options import normalize_options
from ..episode_validation import EpisodeValidationError
from ..quality import validate_robot_policy
from .aloha_hdf5 import AlohaHDF5Provider
from .raw_contract import RawFileContract

RAW_FORMAT = "arx_direct_teaching_hdf5_v1"
RAW_SCHEMA = "bodypipeline.arx.direct_teaching.raw.v1"
CAMERAS = ("head", "left_wrist", "right_wrist")
SOURCES = (*CAMERAS, "left_arm", "right_arm")


def capture_contract_hash(profile: dict) -> str:
    return digest(
        {
            k: v
            for k, v in profile.items()
            if k
            not in {"semantics_approved", "clock_verified", "verification", "notes"}
        }
    )


def _self_contained(group, seen=None):
    seen = set() if seen is None else seen
    address = h5py.h5o.get_info(group.id).addr
    if address in seen:
        raise EpisodeValidationError("ARX HDF5 包含循环或别名链接")
    seen.add(address)
    if isinstance(group, h5py.Dataset):
        if group.is_virtual or group.external:
            raise EpisodeValidationError("ARX HDF5 必须自包含")
        return
    for key in group:
        if not isinstance(group.get(key, getlink=True), h5py.HardLink):
            raise EpisodeValidationError("ARX HDF5 不允许外部或软链接")
        _self_contained(group[key], seen)


def _read(path: Path, profile: dict, *, exact_profile=False) -> dict:
    try:
        with h5py.File(path, "r") as h:
            _self_contained(h)
            if (
                h.attrs.get("schema") != RAW_SCHEMA
                or h.attrs.get("raw_format") != RAW_FORMAT
                or h.attrs.get("complete") != np.bool_(True)
            ):
                raise EpisodeValidationError("ARX HDF5 schema、格式或完成标记无效")
            if h.attrs.get("capture_contract_hash") != capture_contract_hash(profile):
                raise EpisodeValidationError("ARX HDF5 采集契约与可信 Profile 不一致")
            if exact_profile and h.attrs.get("profile_hash") != digest(profile):
                raise EpisodeValidationError("ARX HDF5 Profile 摘要不一致")
            if h.attrs.get("fps") != profile["output_fps"]:
                raise EpisodeValidationError("ARX HDF5 FPS 与 Profile 不一致")
            times = np.asarray(h["timestamps/monotonic"], dtype=np.float64)
            if (
                times.ndim != 1
                or len(times) < profile["minimum_frames"]
                or not np.isfinite(times).all()
                or np.any(np.diff(times) <= 0)
            ):
                raise EpisodeValidationError("ARX 帧时间戳无效或帧数不足")
            n = len(times)
            if h.attrs.get("frame_count") != n:
                raise EpisodeValidationError("ARX frame_count 与实际帧数不一致")
            arrays = {}
            for key in (
                "observations/qpos",
                "observations/qvel",
                "observations/effort",
                "observations/eef",
                "action",
                "action_eef",
            ):
                value = np.asarray(h[key], dtype=np.float32)
                if value.shape != (n, 14) or not np.isfinite(value).all():
                    raise EpisodeValidationError(f"ARX 字段维度或数值无效：{key}")
                arrays[key] = value
            for action, state in (
                ("action", "observations/qpos"),
                ("action_eef", "observations/eef"),
            ):
                if not np.array_equal(arrays[action], arrays[state]):
                    raise EpisodeValidationError("ARX action 必须是同帧实测轨迹副本")
            raw_first = h.attrs.get("recording_mode") == "raw_first"
            source = np.stack(
                [
                    np.asarray(h[f"timestamps/source/{s}"], dtype=np.float64)
                    for s in SOURCES
                ],
                axis=1,
            )
            if (
                source.shape != (n, 5)
                or not np.isfinite(source).all()
                or np.any(source <= 0)
                or np.any(np.diff(source, axis=0) < (0 if raw_first else 1e-12))
            ):
                raise EpisodeValidationError("ARX 传感器源时间戳无效")
            skew_ms = np.ptp(source, axis=1) * 1000
            if raw_first:
                if h.attrs.get("alignment_clock") != "source":
                    raise EpisodeValidationError("ARX raw_first 未声明源时间轴")
                reference = np.asarray(h["timestamps/reference_source_ns"])
                if (
                    reference.shape != (n,)
                    or reference.dtype.kind != "i"
                    or np.any(reference <= 0)
                    or np.any(np.diff(reference) <= 0)
                ):
                    raise EpisodeValidationError("ARX 参考相机源时间戳无效")
                exact = None
                for key in (
                    "timestamps/source_ns",
                    "timestamps/received_monotonic_ns",
                    "source_sample_id",
                ):
                    value = np.asarray(h[key])
                    if (
                        value.shape != (n, 5)
                        or value.dtype.kind != "i"
                        or np.any(value <= 0)
                        or np.any(np.diff(value, axis=0) < 0)
                    ):
                        raise EpisodeValidationError(
                            f"ARX 原始时间戳或消息索引无效：{key}"
                        )
                    if key == "timestamps/source_ns":
                        exact = value
                if not np.array_equal(exact[:, 0], reference) or not np.allclose(
                    exact.astype(np.float64) / 1e9, source, rtol=0, atol=1e-6
                ):
                    raise EpisodeValidationError("ARX 精确时间戳与参考或兼容字段不一致")
                times = (reference - reference[0]).astype(np.float64) / 1e9
                skew_ms = np.ptp(exact, axis=1).astype(np.float64) / 1e6
            else:
                times = times - times[0]
            cameras = {}
            for name in CAMERAS:
                ds = h[f"observations/images/{name}"]
                if ds.ndim not in (1, 2) or len(ds) != n:
                    raise EpisodeValidationError(f"ARX 相机帧数不匹配：{name}")
                size = None
                for frame in ds:
                    with Image.open(
                        io.BytesIO(np.asarray(frame, dtype=np.uint8).tobytes())
                    ) as image:
                        image.load()
                        if image.format != "JPEG" or image.mode != "RGB":
                            raise EpisodeValidationError(
                                f"ARX 相机不是 RGB JPEG：{name}"
                            )
                        if size is not None and size != image.size:
                            raise EpisodeValidationError(f"ARX 相机分辨率变化：{name}")
                        size = image.size
                cameras[name] = size
            metadata = json.loads(h["metadata"][()])
            if not isinstance(metadata, dict) or (
                metadata.get("schema") != RAW_SCHEMA
                or metadata.get("raw_format") != RAW_FORMAT
                or metadata.get("session_id") != h.attrs.get("session_id")
                or metadata.get("profile_hash") != h.attrs.get("profile_hash")
            ):
                raise EpisodeValidationError("ARX 文件元数据与采集属性不一致")
    except DomainError:
        raise
    except (KeyError, ValueError, TypeError, OSError) as exc:
        raise EpisodeValidationError(
            f"ARX 原始文件或图像无法读取：{path.name}"
        ) from exc
    return {
        "frames": n,
        "dimensions": 14,
        "times": times,
        "skew_ms": skew_ms,
        "cameras": cameras,
        "state": arrays["observations/qpos"],
        "action": arrays["action"],
        "metadata": metadata,
        "clock": "source" if raw_first else "monotonic",
    }


class ARXDirectTeachingProvider(AlohaHDF5Provider):
    id = "arx_direct_teaching"
    source_format = RAW_FORMAT
    raw_formats = (RAW_FORMAT,)

    def raw_file_contract(self, raw_format: str) -> RawFileContract:
        if raw_format not in self.raw_formats:
            raise DomainError("ARX Provider 不接受该原始格式", 400)
        return RawFileContract(single_file=True, filename="episode.hdf5")

    def validate_layout(self, profile: dict) -> None:
        validate_robot_policy(profile, expected_dimensions=14)
        if (
            profile.get("robot_type") != "ARX_X5"
            or profile.get("control_mode") != "teleoperation"
        ):
            raise DomainError("ARX Provider 需要 ARX_X5 遥操作 Profile")
        if [c.get("id") for c in profile.get("cameras", [])] != list(CAMERAS):
            raise DomainError("ARX 需要 head / left_wrist / right_wrist 三路相机")
        if (
            profile.get("action_semantics", {}).get("representation")
            != "measured_joint_position_as_demonstration_target"
        ):
            raise DomainError("ARX Profile 必须声明同帧实测位置作为示教目标")
        if (
            type(profile.get("output_fps")) is not int
            or not 1 <= profile["output_fps"] <= 240
        ):
            raise DomainError("ARX output_fps 无效")
        if (
            type(profile.get("minimum_frames")) is not int
            or profile["minimum_frames"] < 1
        ):
            raise DomainError("ARX minimum_frames 无效")
        for key in ("max_frame_skew_ms", "max_sensor_skew_ms"):
            value = profile.get(key)
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 < value <= 1000
            ):
                raise DomainError(f"ARX {key} 无效")

    def conversion_capabilities(self, profile: dict) -> dict:
        # Gripper units/calibration are unverified for this capture contract.
        return {}

    def episode_metadata(self, path: Path, profile: dict) -> dict:
        self.validate_profile(profile)
        report = _read(path, profile)
        return {
            "frames": report["frames"],
            "dimensions": report["dimensions"],
            "cameras": {k: list(v) for k, v in report["cameras"].items()},
            "clock": report["clock"],
            "max_sensor_skew_ms": float(report["skew_ms"].max()),
            "max_frame_gap_ms": float(np.diff(report["times"]).max(initial=0) * 1000),
            "metadata": report["metadata"],
        }

    def validate_upload(self, raw: Path, manifest: dict, profile: dict) -> None:
        self.validate_profile(profile)
        try:
            report = _read(
                raw / "episode.hdf5", manifest["profile"], exact_profile=True
            )
        except EpisodeValidationError as exc:
            raise DomainError(str(exc), 422) from exc
        for key in (
            "session_id",
            "robot_id",
            "project_id",
            "profile_hash",
            "dataset_name",
            "task_name",
            "collector_name",
            "instruction",
        ):
            if report["metadata"].get(key) != manifest.get(key):
                raise DomainError(f"ARX 文件元数据与 Manifest 不一致：{key}", 422)

    def prepare(
        self, raw: Path, dest: Path, profile: dict, *, options: dict | None = None
    ) -> dict:
        self.validate_profile(profile)
        normalize_options(options, self.conversion_capabilities(profile))
        package = raw if raw.is_file() else raw / "episode.hdf5"
        report = _read(package, profile)
        times, fps = report["times"], profile["output_fps"]
        count = int(math.floor(times[-1] * fps + 1e-7)) + 1
        if count < profile["minimum_frames"]:
            raise EpisodeValidationError("ARX 时间轴不足以生成最低帧数")
        # Never fill long acquisition gaps with an unbounded number of copies.
        if np.max(np.diff(times), initial=0) > 2 * profile["max_frame_skew_ms"] / 1000:
            raise EpisodeValidationError(
                "ARX 采集间隙超过帧对齐容差", code="temporal_gap"
            )
        grid = np.arange(count, dtype=np.float64) / fps
        right = np.clip(np.searchsorted(times, grid), 0, len(times) - 1)
        left = np.maximum(right - 1, 0)
        indices = np.where(
            abs(times[left] - grid) <= abs(times[right] - grid), left, right
        )
        frame_skew = abs(times[indices] - grid) * 1000
        if frame_skew.max() > profile["max_frame_skew_ms"]:
            raise EpisodeValidationError(
                "ARX 帧对齐误差超过 Profile 容差", code="temporal_skew"
            )
        if report["skew_ms"].max() > profile["max_sensor_skew_ms"]:
            raise EpisodeValidationError(
                "ARX 传感器时间差超过 Profile 容差", code="sensor_skew"
            )
        aligned = dest.with_suffix(".hdf5")
        aligned.parent.mkdir(parents=True, exist_ok=True)
        temporary = aligned.with_name(f".{aligned.name}.{os.getpid()}.tmp")
        try:
            with h5py.File(temporary, "x") as output, h5py.File(package, "r") as source:
                output.attrs.update(
                    schema=AGILEX_ALIGNED_SCHEMA,
                    fps=fps,
                    frame_count=count,
                    source_format=RAW_FORMAT,
                )
                output.create_dataset(
                    "observation.state",
                    data=report["state"][indices],
                    compression="gzip",
                )
                output.create_dataset(
                    "action", data=report["action"][indices], compression="gzip"
                )
                output.create_dataset("timestamp", data=grid)
                output.create_dataset("source_frame_index", data=indices)
                for name, (width, height) in report["cameras"].items():
                    ds = output.create_dataset(
                        f"observation.images.{name}",
                        shape=(count,),
                        dtype=h5py.vlen_dtype(np.dtype("uint8")),
                    )
                    ds.attrs.update(width=width, height=height, encoded_format="jpeg")
                    for i, source_index in enumerate(indices):
                        ds[i] = source[f"observations/images/{name}"][int(source_index)]
            os.replace(temporary, aligned)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return {
            "fps": fps,
            "frames": count,
            "source_frames": report["frames"],
            "method": "bounded nearest complete snapshot on recorded timestamps",
            "alignment_clock": report["clock"],
            "max_frame_skew_ms": float(frame_skew.max()),
            "max_sensor_skew_ms": float(report["skew_ms"].max()),
            "reused_output_frames": count - len(np.unique(indices)),
            "streams": {
                "observation.state": {"dimensions": 14},
                "action": {"dimensions": 14},
                **{
                    f"observation.images.{k}": {"dimensions": [h, w, 3]}
                    for k, (w, h) in report["cameras"].items()
                },
            },
            "errors": [],
            "aligned_path": str(aligned),
            "aligned_kind": "hdf5",
            "source_package": package.name,
        }
