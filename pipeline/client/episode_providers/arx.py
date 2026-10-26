"""ARX Enactive bundles -> explicit kinesthetic HDF5, without controller calls."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import zipfile

import h5py
import numpy as np

from pipeline.shared.arx_kinesthetic import FORMAT, SCHEMA, validate
from pipeline.shared.core import DomainError, digest, fsync_dir


class ArxKinestheticEpisodes:
    backend_id = "arx_kinesthetic"
    robot_type = "arx_x5"

    def __init__(self, profile):
        if profile.get("robot_type") != self.robot_type:
            raise DomainError("纠偏 Provider 与机器人 Profile 不匹配")
        if [camera["id"] for camera in profile["cameras"]] != ["head", "left_wrist", "right_wrist"]:
            raise DomainError("ARX 纠偏 Provider 需要三路既有相机")
        self.base_profile = profile

    def profile(self, episode):
        profile = deepcopy(self.base_profile)
        profile.update(id=str(profile.get("id", "arx")) + "-dagger", minimum_frames=1,
                       output_fps=int(episode["fps"]))
        profile["action_semantics"] = {**profile.get("action_semantics", {}),
                                       "representation": "kinesthetic_next_measured_position",
                                       "label_source": "kinesthetic_next_measured_position",
                                       "timebase": "recorded_monotonic", "target_offset": "next_recorded_frame"}
        if episode.get("robot_model"):
            if episode["robot_model"] not in {"x5_2023", "x5_2025"}:
                raise DomainError("ARX 纠偏机械臂版本未知")
            profile["robot_model"] = episode["robot_model"]
        return profile

    def materialize(self, bundle: Path, target: Path, profile: dict, episode: dict, capture_id: str):
        with zipfile.ZipFile(bundle) as archive:
            metadata = json.loads(archive.read("metadata.json"))
            marker = json.loads(archive.read("segment.json"))
            if metadata.get("schema") != "enactive_arx_kinesthetic_dagger_v1" or metadata.get("label_source") != "kinesthetic_next_measured_position":
                raise DomainError("纠偏 bundle 格式或动作标签不匹配")
            if not marker.get("complete") or not marker.get("keep"):
                raise DomainError("未完成或已丢弃的纠偏禁止上传")
            if metadata["instruction"] != episode["instruction"]:
                raise DomainError("纠偏数据的任务指令与 Episode 不一致")
            if metadata.get("checkpoint") != episode.get("checkpoint"):
                raise DomainError("纠偏 raw 的权重与 Episode 不一致")
            rows = [json.loads(line) for line in archive.read("frames.jsonl").decode().splitlines() if line.strip()]
            times = np.asarray([row["captured_at"] for row in rows], dtype=np.float64)
            values = np.asarray([row["state"] for row in rows], dtype=np.float32)
            fps = float(metadata["fps"])
            if not np.isfinite(fps) or not fps.is_integer() or not 1 <= fps <= 240 or fps != profile["output_fps"]:
                raise DomainError("纠偏采样率无效或与 Profile 不匹配")
            if len(rows) != marker["frames"] or len(rows) < 2 or values.shape != (len(rows), 14):
                raise DomainError("纠偏需要至少两帧 14 维反馈")
            if not np.isfinite(times).all() or not np.isfinite(values).all() or np.any(np.diff(times) <= 0):
                raise DomainError("纠偏时间戳必须有限且严格递增")
            # This is the original capture package, not a training resampler.
            # Retain every real frame and its real clock, including sensor gaps.
            # The unavailable final future action and long-gap labels are masked.
            count = len(rows)
            indices = np.arange(count)
            actions = np.concatenate([values[1:], values[-1:]], axis=0)
            action_dt = np.concatenate([np.diff(times), [0.0]])
            action_valid = (action_dt > 0) & (action_dt <= 0.25)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp.hdf5")
            try:
                with h5py.File(temporary, "w") as handle:
                    handle.attrs.update(schema=SCHEMA, raw_format=FORMAT, complete=False, robot_type=self.robot_type,
                                        fps=int(fps), session_id=capture_id, profile_hash=digest(profile),
                                        task=metadata["instruction"], label_source=metadata["label_source"],
                                        timebase="recorded_monotonic", action_target_offset="next_recorded_frame",
                                        nominal_fps=int(fps), max_gap_s=float(np.max(np.diff(times))),
                                        recorded_fps=(count - 1) / float(times[-1] - times[0]),
                                        source_episode_id=episode["id"], source_bundle_sha256=episode["bundle_sha256"],
                                        checkpoint=episode["checkpoint"], source_raw_format="enactive_arx_kinesthetic_zip_v1")
                    handle.create_dataset("observation.state", data=values, compression="gzip")
                    handle.create_dataset("action", data=actions, compression="gzip")
                    handle.create_dataset("action_dt", data=action_dt)
                    handle.create_dataset("action_valid_mask", data=action_valid)
                    handle.create_dataset("timestamps/monotonic", data=times)
                    handle.create_dataset("source_frame_index", data=indices)
                    handle.create_dataset("source/state", data=values, compression="gzip")
                    handle.create_dataset("source/captured_at", data=times)
                    handle.create_dataset("source/frames_json", data=json.dumps(rows), dtype=h5py.string_dtype())
                    handle.create_dataset("source/session_json", data=json.dumps(metadata), dtype=h5py.string_dtype())
                    for view, camera in (("base", "head"), ("left_wrist", "left_wrist"), ("right_wrist", "right_wrist")):
                        dataset = handle.create_dataset("observation.images." + camera, (count,), dtype=h5py.vlen_dtype(np.dtype("uint8")))
                        dataset.attrs["encoded_format"] = "png"
                        for index, source in enumerate(indices):
                            name = rows[int(source)]["images"][view]
                            if Path(name).name != name:
                                raise DomainError("纠偏图像路径无效")
                            dataset[index] = np.frombuffer(archive.read("images/" + name), dtype=np.uint8)
                    handle.attrs["complete"] = True
                    report = validate(handle, profile, session_id=capture_id)
                with temporary.open("rb") as stream:
                    os.fsync(stream.fileno())
                temporary.replace(target)
                fsync_dir(target.parent)
            finally:
                temporary.unlink(missing_ok=True)
        return {**report, "source_frames": len(rows), "duration_s": float(times[-1] - times[0]),
                "max_gap_s": float(np.max(np.diff(times))), "invalid_action_labels": int(np.count_nonzero(~action_valid)),
                "size_bytes": target.stat().st_size}
