"""Profile-bound position relabeling; never changes measured observations.

The detector follows the position-only close/hold/release heuristic in the ARX
reference script. Only stable holding plateaus within those phases are selected.
All dimensions, motor endpoints and thresholds come from trusted server config.
NumPy is sufficient; the conversion service needs no additional toolchain.
"""

from __future__ import annotations

import math

import numpy as np

from pipeline.shared.core import DomainError

ALGORITHM = "closing_hold_plateaus_v1"
DEFAULT_PARAMETERS = {
    "median_frames": 5,
    "opening_prominence": 0.06,
    "minimum_closure": 0.06,
    "closing_onset": 0.02,
    "plateau_range": 0.04,
    "plateau_seconds": 0.3,
    "minimum_phase_seconds": 0.4,
    "release_window_seconds": 0.2,
    "release_drop": 0.045,
    "release_confirm_seconds": 0.5,
    "release_confirm_drop": 0.12,
    "release_onset_drop": 0.01,
    "empty_closed_margin": 0.2,
    "stability_range": 0.025,
    "minimum_plateau_seconds": 0.5,
    # Allow slow load-induced drift during a sustained grasp. Local window
    # stability and release detection still reject ramps and opening motion.
    "maximum_level_difference": 0.15,
}


def configuration(profile: dict) -> dict | None:
    conversion = profile.get("conversion", {})
    if not isinstance(conversion, dict):
        raise DomainError("Profile conversion 必须是对象")
    if "gripper_closure" not in conversion:
        return None
    config = conversion["gripper_closure"]
    if not isinstance(config, dict) or config.get("algorithm") != ALGORITHM:
        raise DomainError("Profile 夹爪闭合度算法未注册")
    if (
        not isinstance(config.get("calibration_id"), str)
        or not config["calibration_id"]
    ):
        raise DomainError("Profile 夹爪闭合度配置缺少 calibration_id")
    names = profile.get("joint_names", [])
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise DomainError("Profile 夹爪通道需要 joint_names 字符串列表")
    channels = config.get("channels")
    if not isinstance(channels, list) or not channels:
        raise DomainError("Profile 夹爪闭合度配置缺少 channels")
    seen = set()
    for channel in channels:
        if not isinstance(channel, dict):
            raise DomainError("Profile 夹爪通道必须是对象")
        joint = channel.get("joint")
        if not isinstance(joint, str) or names.count(joint) != 1 or joint in seen:
            raise DomainError("Profile 夹爪通道必须绑定唯一的已声明关节")
        seen.add(joint)
        endpoints = [channel.get("open"), channel.get("closed")]
        if any(
            type(v) not in (int, float)
            or not math.isfinite(v)
            or abs(v) > np.finfo(np.float32).max
            for v in endpoints
        ):
            raise DomainError("Profile 夹爪行程端点必须是有限数值")
        if endpoints[0] == endpoints[1]:
            raise DomainError("Profile 夹爪全开与全闭端点不能相同")
    supplied = config.get("phase_parameters", {})
    if not isinstance(supplied, dict) or supplied.keys() - DEFAULT_PARAMETERS.keys():
        raise DomainError("Profile 夹爪区间识别参数无效")
    params = {**DEFAULT_PARAMETERS, **supplied}
    for key, value in params.items():
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise DomainError(f"Profile 夹爪参数 {key} 必须是正有限数值")
    median = params["median_frames"]
    if type(median) is not int or median % 2 != 1 or median > 101:
        raise DomainError("Profile median_frames 必须为不超过 101 的正奇数")
    fps = profile.get("output_fps")
    if type(fps) is not int or not 1 <= fps <= 240:
        raise DomainError("Profile 夹爪修正需要有效的 output_fps")
    default = config.get("default_percent", 90)
    if (
        type(default) not in (float, int)
        or not math.isfinite(default)
        or not 0 <= default <= 100
    ):
        raise DomainError("Profile 夹爪默认闭合度必须在 0–100 之间")
    return {**config, "phase_parameters": params, "default_percent": default}


def capabilities(profile: dict) -> dict:
    config = configuration(profile)
    if config is None:
        return {}
    return {
        "gripper_closure": {
            "minimum_percent": 0,
            "maximum_percent": 100,
            "default_percent": config["default_percent"],
            "algorithm": config["algorithm"],
            "calibration_id": config["calibration_id"],
            "percentage_definition": "nominal_motor_travel",
        }
    }


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    edges = np.diff(np.r_[False, mask, False].astype(np.int8))
    return list(
        zip(np.flatnonzero(edges == 1).tolist(), np.flatnonzero(edges == -1).tolist())
    )


def _troughs(x: np.ndarray, prominence: float) -> list[int]:
    """Local troughs whose rise to both sides reaches the configured prominence.

    Collapse equal-valued runs before finding extrema, including flat troughs.
    Stop each search at a lower trough, as in topographic prominence. We do not
    need SciPy's general peak width/distance machinery for this detector.
    """
    starts = np.r_[0, np.flatnonzero(np.diff(x) != 0) + 1]
    levels = x[starts]
    candidates = (
        np.flatnonzero((levels[1:-1] < levels[:-2]) & (levels[1:-1] < levels[2:])) + 1
    )
    result = []
    for k in candidates:
        level = levels[k]
        left = k - 1
        while left > 0 and levels[left] >= level and levels[left] < level + prominence:
            left -= 1
        right = k + 1
        while (
            right < len(levels) - 1
            and levels[right] >= level
            and levels[right] < level + prominence
        ):
            right += 1
        if levels[left] >= level + prominence and levels[right] >= level + prominence:
            stop = starts[k + 1] if k + 1 < len(starts) else len(x)
            result.append(int((starts[k] + stop - 1) // 2))
    return result


def holding_mask(
    position: np.ndarray, fps: float, closed: float, params: dict
) -> np.ndarray:
    """Input increases toward closed. Return stable plateau frames only."""
    width = params["median_frames"]
    padded = np.pad(np.asarray(position, dtype=np.float64), width // 2, mode="edge")
    x = np.median(np.lib.stride_tricks.sliding_window_view(padded, width), axis=1)
    n = len(x)
    mask = np.zeros(n, dtype=bool)
    stable_n = max(2, round(params["plateau_seconds"] * fps))
    minimum_n = max(1, round(params["minimum_phase_seconds"] * fps))
    plateau_n = max(1, round(params["minimum_plateau_seconds"] * fps))
    release_n = max(1, round(params["release_window_seconds"] * fps))
    confirm_n = max(release_n, round(params["release_confirm_seconds"] * fps))
    if n < max(stable_n, minimum_n, plateau_n):
        return mask
    empty_closed = closed - params["empty_closed_margin"]
    valleys = _troughs(x, params["opening_prominence"])
    if x[0] < empty_closed:
        valleys = sorted(set([0, *valleys]))
    windows = np.lib.stride_tricks.sliding_window_view(x, stable_n)
    ranges, medians = np.ptp(windows, axis=1), np.median(windows, axis=1)
    for j, valley in enumerate(valleys):
        end = valleys[j + 1] if j + 1 < len(valleys) else n
        hits = np.flatnonzero(x[valley:end] >= x[valley] + params["closing_onset"])
        if not len(hits):
            continue
        start = valley + int(hits[0])
        candidates = np.flatnonzero(
            (ranges[start : max(start, end - stable_n + 1)] <= params["plateau_range"])
            & (
                medians[start : max(start, end - stable_n + 1)]
                >= x[valley] + params["minimum_closure"]
            )
            & (medians[start : max(start, end - stable_n + 1)] < empty_closed)
        )
        if not len(candidates):
            continue
        stable = start + int(candidates[0])
        if np.any(x[start:stable] >= empty_closed):
            continue
        stop = n
        for k in range(stable + stable_n, n):
            if x[k] >= empty_closed:
                stop = k
                break
            confirm_end = min(k + confirm_n + 1, n)
            if (
                k + release_n < n
                and x[k + release_n] - x[k] < -params["release_drop"]
                and np.min(x[k + release_n : confirm_end]) - x[k]
                < -params["release_confirm_drop"]
            ):
                onset = np.flatnonzero(
                    x[k : k + release_n + 1] < x[k] - params["release_onset_drop"]
                )
                stop = k + int(onset[0]) if len(onset) else k
                break
        if stop - start < minimum_n:
            continue
        # Mark all frames covered by stable windows; then reject short/drifting
        # plateaus. Motion outside these plateaus retains its original actions.
        eligible = np.zeros(n, dtype=bool)
        for k in range(stable, min(stop - stable_n + 1, len(ranges))):
            if ranges[k] <= params["stability_range"] and medians[k] < empty_closed:
                eligible[k : k + stable_n] = True
        for a, b in _runs(eligible):
            if (
                b - a >= plateau_n
                and np.ptp(x[a:b]) <= params["maximum_level_difference"]
            ):
                mask[a:b] = True
    return mask


def correct_actions(
    state: np.ndarray, action: np.ndarray, profile: dict, options: dict
) -> tuple[np.ndarray, dict]:
    config = configuration(profile)
    if config is None:
        raise DomainError("所选 Profile 不支持夹爪闭合度修正", 400)
    percent = options["minimum_percent"]
    revised = action.copy()
    any_changed = np.zeros(len(action), dtype=bool)
    channels = {}
    for channel in config["channels"]:
        joint = channel["joint"]
        dimension = profile["joint_names"].index(joint)
        opened, closed = channel["open"], channel["closed"]
        direction = 1 if closed > opened else -1
        target = float(np.float32(opened + percent / 100 * (closed - opened)))
        if not math.isfinite(target):
            raise DomainError("夹爪目标位置超出 float32 范围")
        mask = holding_mask(
            state[:, dimension] * direction,
            profile["output_fps"],
            closed * direction,
            config["phase_parameters"],
        )
        values = revised[:, dimension]
        values[mask] = (
            np.maximum(values[mask], target)
            if direction > 0
            else np.minimum(values[mask], target)
        )
        changed = values != action[:, dimension]
        any_changed |= changed
        channels[joint] = {
            "dimension": dimension,
            "open": opened,
            "closed": closed,
            "target": target,
            "selected_frames": int(mask.sum()),
            "changed_frames": int(changed.sum()),
            "selected_intervals": [
                {"from_frame": a, "to_frame_exclusive": b} for a, b in _runs(mask)
            ],
            "changed_intervals": [
                {"from_frame": a, "to_frame_exclusive": b} for a, b in _runs(changed)
            ],
        }
    return revised, {
        "algorithm": ALGORITHM,
        "calibration_id": config["calibration_id"],
        "minimum_percent": percent,
        "percentage_definition": "nominal_motor_travel; not aperture or force",
        "phase_parameters": config["phase_parameters"],
        "changed_features": ["action"],
        "changed_frames_any_channel": int(any_changed.sum()),
        "channels": channels,
        "warnings": []
        if any_changed.any()
        else ["未找到需要提高闭合度的稳定夹持帧；动作保持原值"],
    }
