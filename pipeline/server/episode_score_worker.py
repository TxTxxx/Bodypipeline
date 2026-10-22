"""Read-only Doctor scoring in the isolated letools environment.

The pinned Doctor loader flattens vector actions and loses episodes across
Parquet shards. Adapt only that loader, in this subprocess, while retaining
the pinned scoring functions and weights. Never rewrite the input dataset.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

DOCTOR_COMMIT = "ff9475332cec755eb61a03b30273cc12dccddc28"


def score_dataset(root: Path) -> dict:
    import numpy as np
    import pyarrow.parquet as pq
    from lerobot_doctor import score as doctor
    from letools.external import upstream_project

    if upstream_project("lerobot-doctor")["commit"] != DOCTOR_COMMIT:
        raise ValueError("Doctor 版本已变化，需要重新验证评分适配")
    info = json.loads((root / "meta/info.json").read_text())
    total = info.get("total_episodes")
    if type(total) is not int or total < 1:
        raise ValueError("数据集缺少有效的 total_episodes")
    parts: dict[int, list] = {i: [] for i in range(total)}
    frames: dict[int, list] = {i: [] for i in range(total)}
    problems: dict[int, list[str]] = {i: [] for i in range(total)}
    observed_frames = 0
    for path in sorted((root / "data").rglob("*.parquet")):
        if any(p.is_symlink() for p in [path, *path.parents]):
            raise ValueError("评分输入不允许包含符号链接")
        names = pq.read_schema(path).names
        actions = (
            ["action"]
            if "action" in names
            else sorted(name for name in names if name.startswith("action."))
        )
        required = ["episode_index", "frame_index"]
        if not set(required).issubset(names):
            raise ValueError(f"数据分片缺少 Episode 或帧索引：{path.name}")
        table = pq.read_table(path, columns=required + actions)
        ids = table.column("episode_index").to_pylist()
        if any(type(i) is not int or i not in parts for i in ids):
            raise ValueError("数据中的 episode_index 与元数据范围不一致")
        observed_frames += len(ids)
        row_indices = defaultdict(list)
        for position, index in enumerate(ids):
            row_indices[index].append(position)
        for index, positions in sorted(row_indices.items()):
            selected = table.take(positions)
            frames[index].extend(selected.column("frame_index").to_pylist())
            if not actions:
                problems[index].append("缺少 action 动作数据")
                continue
            try:
                columns = []
                for name in actions:
                    values = np.asarray(selected.column(name).to_pylist(), dtype=float)
                    if values.ndim == 1:
                        values = values[:, None]
                    if values.ndim != 2 or values.shape[1] == 0:
                        raise ValueError("动作不是标量或一维向量")
                    columns.append(values)
                parts[index].append(np.concatenate(columns, axis=1))
            except (ValueError, TypeError):
                problems[index].append("动作形状不一致或包含非数值")
    if observed_frames != info.get("total_frames"):
        raise ValueError("实际帧数与 meta/info.json 不一致，评分覆盖无法确认")

    eligible = {}
    rows = []
    for index in range(total):
        indices = frames[index]
        reasons = problems[index]
        if not indices:
            reasons.append("未找到该 Episode 的数据")
        elif any(type(i) is not int for i in indices) or sorted(indices) != list(
            range(len(indices))
        ):
            reasons.append("帧索引不连续或重复")
        if len(indices) < 3:
            reasons.append("少于 3 帧，无法可靠评分")
        actions = None
        if parts[index] and not reasons:
            try:
                actions = np.concatenate(parts[index], axis=0)
                actions = actions[np.argsort(indices)]
                if not np.isfinite(actions).all():
                    reasons.append("动作包含 NaN/Inf，未插值或修改原数据")
            except ValueError:
                reasons.append("不同分片的动作维度不一致")
        row = {
            "episode_index": index,
            "frames": len(indices),
            "status": "UNSCORABLE",
            "overall_score": None,
            "smoothness": None,
            "coverage": None,
            "consistency": None,
            "length_score": None,
            "is_outlier": False,
            "reasons": list(dict.fromkeys(reasons)),
        }
        rows.append(row)
        if actions is not None and not reasons:
            eligible[index] = {"actions": actions}

    dimensions = {item["actions"].shape[1] for item in eligible.values()}
    if len(dimensions) > 1:
        raise ValueError("不同 Episode 的动作维度不一致，无法使用同一评分基准")
    if eligible:
        # The patch is confined to this short-lived process. Short/invalid
        # episodes stay explicit null rows, avoiding upstream score/action zip
        # misalignment and non-finite JSON scores.
        with patch.object(doctor, "_load_episodes", return_value=eligible):
            result = doctor.score_episodes(root)
        for score in result.scores:
            row = rows[score.episode_index]
            row.update(
                status="SCORED",
                overall_score=score.overall_score,
                smoothness=score.smoothness,
                coverage=score.coverage,
                consistency=score.consistency if len(eligible) > 3 else None,
                length_score=score.length_score,
                is_outlier=bool(score.is_outlier),
                reasons=list(score.reasons),
            )
            if len(eligible) <= 3:
                row["reasons"].append(
                    "有效 Episode 不足 4 个；原算法一致性项未评估并按 0 计入总分"
                )
            if score.is_outlier:
                row["reasons"].append(
                    "原算法建议复查：总分低于 30 或显著低于数据集均值"
                )
    values = [r["overall_score"] for r in rows if r["status"] == "SCORED"]
    return {
        "algorithm": "lerobot-doctor.score",
        "doctor_commit": DOCTOR_COMMIT,
        "adapter": "vector-shards-v1",
        "total_episodes": total,
        "scored_episodes": len(values),
        "total_frames": observed_frames,
        "mean_score": sum(values) / len(values) if values else None,
        "drop_threshold": 30,
        "episodes": rows,
    }


if __name__ == "__main__":
    try:
        print(
            json.dumps(
                score_dataset(Path(sys.argv[1])), ensure_ascii=False, allow_nan=False
            )
        )
    except Exception as exc:
        print(f"Episode 评分失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from None
