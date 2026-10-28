"""Read frame-aligned subtasks in the isolated LeTools environment (read only)."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path


def read_subtasks(root: Path) -> dict:
    import pyarrow.parquet as pq

    info = json.loads((root / "meta/info.json").read_text())
    fps = info.get("fps")
    if (
        info.get("codebase_version") not in {"v2.1", "v3.0"}
        or type(fps) not in (int, float)
        or not math.isfinite(fps)
        or fps <= 0
    ):
        raise ValueError("仅支持具有有效 FPS 的 LeRobot v2.1 / v3.0 数据集")
    vocabulary = {}
    vocab_path = root / "meta/subtasks.parquet"
    if vocab_path.is_file():
        # Pandas index metadata is intentionally not needed: Arrow exposes both
        # physical columns even when `subtask` is the pandas index.
        for row in pq.read_table(vocab_path).to_pylist():
            index, text = row["subtask_index"], row["subtask"]
            if type(index) is not int or not isinstance(text, str) or not text.strip():
                raise ValueError("Subtask 词表格式无效")
            if index in vocabulary:
                raise ValueError("Subtask 词表编号重复")
            vocabulary[index] = text
    episodes = {}
    for shard in sorted((root / "data").rglob("*.parquet")):
        reader = pq.ParquetFile(shard)
        columns = ["episode_index", "frame_index"]
        labelled = "subtask_index" in reader.schema_arrow.names
        if labelled:
            columns.append("subtask_index")
        for batch in reader.iter_batches(batch_size=65536, columns=columns):
            data = batch.to_pydict()
            labels = data.get("subtask_index", [-1] * batch.num_rows)
            for ep, frame, label in zip(data["episode_index"], data["frame_index"], labels):
                if any(type(v) is not int for v in (ep, frame, label)) or ep < 0:
                    raise ValueError("Episode、帧或标签编号无效")
                item = episodes.setdefault(ep, {"episode_index": ep, "frames": 0, "segments": []})
                if frame != item["frames"]:
                    raise ValueError("Episode 帧序不连续或重复，无法安全编辑")
                item["frames"] += 1
                if label == -1:
                    continue
                if label < 0 or label not in vocabulary:
                    raise ValueError(f"Subtask 编号 {label} 没有有效词表项")
                text = vocabulary[label]
                segments = item["segments"]
                if segments and segments[-1]["end_frame"] == frame and segments[-1]["text"] == text:
                    segments[-1]["end_frame"] += 1
                else:
                    segments.append({"start_frame": frame, "end_frame": frame + 1, "text": text})
                    if len(segments) > 2000:
                        raise ValueError("单个 Episode 超过 2000 段，暂不支持编辑")
    if len(episodes) != info.get("total_episodes") or sum(e["frames"] for e in episodes.values()) != info.get("total_frames"):
        raise ValueError("实际 Episode / 帧数与数据集声明不一致")
    return {"fps": fps, "episodes": [episodes[i] for i in sorted(episodes)]}


if __name__ == "__main__":
    print(json.dumps(read_subtasks(Path(sys.argv[1])), ensure_ascii=False))
