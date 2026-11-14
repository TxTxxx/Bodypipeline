"""Three-camera numbered v3 videos with B frames, shared files and camera offsets.

Run in the isolated LeTools Python: .../python this.py <new-fixture-directory>.
Only the supplied new directory is written. No model or robot is involved.
"""

from __future__ import annotations

import json
import sys
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from PIL import Image, ImageDraw, ImageFont

FPS, FRAMES, EPISODES = 30, 120, 3
KEYS = tuple(f"observation.images.camera_{i}" for i in range(3))
OFFSETS = (0, 7, 19)
WIDTH, HEIGHT = 480, 270


def picture(episode, camera, frame):
    image = Image.new("RGB", (WIDTH, HEIGHT), (25 + camera * 35, 45, 75))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 42
    )
    draw.text((18, 20), f"EP {episode} / CAM {camera}", fill="white")
    draw.text((18, 80), f"FRAME {frame:03d}", font=font, fill="white")
    draw.rectangle((18, 158, 18 + frame * 3, 186), fill=(90, 210, 165))
    # Machine-readable pixels supplement (never replace) the visible burn-in.
    code = frame | (episode << 8)
    for bit in range(12):
        color = "white" if code & (1 << bit) else "black"
        draw.rectangle((18 + bit * 28, 220, 40 + bit * 28, 244), fill=color)
    draw.text((18, 252), "SYNTHETIC / CPU / SHARED VIDEO", fill="white")
    return image


def create(root):
    root.mkdir(parents=True, exist_ok=False)
    dataset = root / "dataset"
    for directory in ("meta/episodes/chunk-000", "data/chunk-000"):
        (dataset / directory).mkdir(parents=True)
    features = {
        k: {"dtype": "int64", "shape": [1], "names": None}
        for k in ("index", "episode_index", "frame_index", "task_index")
    }
    features["timestamp"] = {"dtype": "float32", "shape": [1], "names": None}
    for k in ("observation.state", "action"):
        features[k] = {"dtype": "float32", "shape": [2], "names": ["j0", "j1"]}
    for k in KEYS:
        features[k] = {"dtype": "video", "shape": [HEIGHT, WIDTH, 3], "names": None}
    info = {
        "codebase_version": "v3.0",
        "robot_type": "synthetic_video",
        "total_episodes": EPISODES,
        "total_frames": FRAMES * EPISODES,
        "total_tasks": 1,
        "fps": FPS,
        "features": features,
        "splits": {"train": f"0:{EPISODES}"},
        "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
        "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
    }
    (dataset / "meta/info.json").write_text(json.dumps(info))
    (dataset / "meta/stats.json").write_text("{}")
    pq.write_table(
        pa.table({"task_index": [0], "task": ["numbered video"]}),
        dataset / "meta/tasks.parquet",
    )
    rows, episodes = [], []
    for ep in range(EPISODES):
        for f in range(FRAMES):
            rows.append(
                {
                    "index": ep * FRAMES + f,
                    "episode_index": ep,
                    "frame_index": f,
                    "task_index": 0,
                    # Deliberately non-zero data time base, reset per Episode.
                    "timestamp": 5.0 + ep + f / FPS,
                    "observation.state": [f / 100, ep],
                    "action": [f / 100, ep],
                }
            )
        row = {
            "episode_index": ep,
            "tasks": ["numbered video"],
            "length": FRAMES,
            "data/chunk_index": 0,
            "data/file_index": 0,
            "dataset_from_index": ep * FRAMES,
            "dataset_to_index": (ep + 1) * FRAMES,
        }
        for key, offset in zip(KEYS, OFFSETS, strict=True):
            row.update(
                {
                    f"videos/{key}/chunk_index": 0,
                    f"videos/{key}/file_index": 0,
                    f"videos/{key}/from_timestamp": (offset + ep * FRAMES) / FPS,
                    f"videos/{key}/to_timestamp": (offset + (ep + 1) * FRAMES) / FPS,
                }
            )
        episodes.append(row)
    pq.write_table(
        pa.Table.from_pylist(rows), dataset / "data/chunk-000/file-000.parquet"
    )
    pq.write_table(
        pa.Table.from_pylist(episodes),
        dataset / "meta/episodes/chunk-000/file-000.parquet",
    )
    for camera, (key, offset) in enumerate(zip(KEYS, OFFSETS, strict=True)):
        file = dataset / f"videos/{key}/chunk-000/file-000.mp4"
        file.parent.mkdir(parents=True)
        with av.open(str(file), "w") as container:
            stream = container.add_stream("libx264", rate=FPS)
            stream.width, stream.height, stream.pix_fmt = WIDTH, HEIGHT, "yuv420p"
            stream.options = {"crf": "18", "preset": "fast", "g": "60", "bf": "2"}
            stream.thread_count = 1
            for index in range(offset + EPISODES * FRAMES):
                n = max(0, index - offset)
                image = picture(n // FRAMES, camera, n % FRAMES)
                frame = av.VideoFrame.from_ndarray(np.asarray(image), format="rgb24")
                frame.pts, frame.time_base = index, Fraction(1, FPS)
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
    return dataset


if __name__ == "__main__":
    print(create(Path(sys.argv[1])))
