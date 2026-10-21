"""Exercise real shared Parquet/MP4 cleanup in the optional letools runtime."""

import copy
import json
import tempfile
from pathlib import Path

import av
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from letools._stats import flatten_stats
from letools.plugins import open_dataset
from letools.validation import validate_dataset

from pipeline.server.recycle_worker import (
    MARKER,
    _exchange,
    _scalar_stats,
    rebuild,
    recover_operation,
    run,
)
from pipeline.shared.core import Store


def fixture(root, gop):
    (root / "meta/episodes/chunk-000").mkdir(parents=True)
    (root / "data/chunk-000").mkdir(parents=True)
    video = root / "videos/observation.images.head/chunk-000/file-000.mp4"
    video.parent.mkdir(parents=True)
    with av.open(str(video), "w") as output:
        stream = output.add_stream("libx264", rate=6)
        stream.width = stream.height = 32
        stream.pix_fmt = "yuv420p"
        stream.options = {
            "crf": "18",
            "bf": "0",
            "g": str(gop),
            "keyint_min": str(gop),
            "sc_threshold": "0",
        }
        for i in range(18):
            pixels = np.full((32, 32, 3), [30, 100, 200][i // 6], dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    rows = []
    for ep in range(3):
        for frame in range(6):
            rows.append(
                {
                    "episode_index": ep,
                    "index": ep * 6 + frame,
                    "frame_index": frame,
                    "timestamp": frame / 6,
                    "task_index": 0,
                    "action": [float(ep * 10 + frame)],
                }
            )
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, root / "data/chunk-000/file-000.parquet")
    features = {
        name: {"dtype": "int64", "shape": [1], "names": None}
        for name in ["episode_index", "index", "frame_index", "task_index"]
    }
    features["timestamp"] = {"dtype": "float32", "shape": [1], "names": None}
    features["action"] = {"dtype": "float32", "shape": [1], "names": ["joint0"]}
    features["observation.images.head"] = {
        "dtype": "video",
        "shape": [32, 32, 3],
        "names": ["height", "width", "channels"],
        "info": {"video.fps": 6, "video.codec": "h264", "video.pix_fmt": "yuv420p"},
    }
    info = {
        "codebase_version": "v3.0",
        "robot_type": "fixture",
        "total_episodes": 3,
        "total_frames": 18,
        "total_tasks": 1,
        "fps": 6,
        "chunks_size": 1000,
        "splits": {"train": "0:3"},
        "features": features,
        "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
        "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
    }
    (root / "meta/info.json").write_text(json.dumps(info))
    (root / "meta/stats.json").write_text("{}")
    pq.write_table(
        pa.Table.from_pylist([{"task_index": 0, "task": "Test."}]),
        root / "meta/tasks.parquet",
    )
    episodes = []
    for ep in range(3):
        stats = {
            name: _scalar_stats(table.slice(ep * 6, 6)[name].to_pylist())
            for name in rows[0]
            if name != "action"
        }
        stats["action"] = _scalar_stats([ep * 10 + i for i in range(6)])
        prefix = "videos/observation.images.head"
        episodes.append(
            {
                "episode_index": ep,
                "length": 6,
                "tasks": ["Test."],
                "data/chunk_index": 0,
                "data/file_index": 0,
                "dataset_from_index": ep * 6,
                "dataset_to_index": ep * 6 + 6,
                "meta/episodes/chunk_index": 0,
                "meta/episodes/file_index": 0,
                f"{prefix}/chunk_index": 0,
                f"{prefix}/file_index": 0,
                f"{prefix}/from_timestamp": ep,
                f"{prefix}/to_timestamp": ep + 1,
                **flatten_stats(stats),
            }
        )
    pq.write_table(
        pa.Table.from_pylist(episodes),
        root / "meta/episodes/chunk-000/file-000.parquet",
    )
    assert validate_dataset(root, deep=True).valid


def assert_kept(root):
    report = validate_dataset(root, deep=True)
    assert report.valid, report.errors
    ds = open_dataset(root)
    assert ds.metadata.total_episodes == 2 and ds.metadata.total_frames == 12
    assert [ds.read_episode(e)["action"].to_pylist() for e in ds.episodes] == [
        [[float(i)] for i in range(6)],
        [[float(i)] for i in range(20, 26)],
    ]
    means = []
    for path in sorted((root / "videos").rglob("*.mp4")):
        with av.open(str(path)) as video:
            means.extend(
                float(frame.to_ndarray(format="rgb24").mean())
                for frame in video.decode(video=0)
            )
    assert len(means) == 12 and max(means[:6]) < 45 and min(means[6:]) > 180, means
    assert ds.episodes[1].stats["episode_index"]["mean"] == [1.0]
    assert ds.episodes[1].stats["index"]["min"] == [6.0]


def scenario(base, gop, legacy=False):
    root = base / "dataset"
    fixture(root, gop)
    if legacy:
        from letools.backends.v21 import LeRobotV21Backend
        from letools.conversion_types import ConversionConfig
        from letools.telemetry import StageRecorder

        converted = base / "dataset21"
        LeRobotV21Backend().write(
            open_dataset(root), converted, ConversionConfig(), StageRecorder()
        )
        root = converted
    store = Store(base / "state")
    key = "fixture"
    op = "a" * 32
    entries = {
        "1": {"episode": 1, "deleted_at": 1},
        "2": {"episode": 2, "deleted_at": 2},
    }
    row = {
        "id": key,
        "dataset_path": str(root),
        "revision": 0,
        "entries": entries,
        "operation": {
            "id": op,
            "state": "pending",
            "episodes": [1],
            "entries_before": copy.deepcopy(entries),
        },
    }
    store.put("recycle", key, row)
    run(store, key, op)
    assert_kept(root)
    result = store.get("recycle", key)
    assert result["operation"]["state"] == "completed"
    assert result["entries"] == {"1": {"episode": 1, "deleted_at": 2}}
    assert not list(base.glob(".*.recycle-*"))
    assert json.loads((root / MARKER).read_text())["deleted_frames"] == 6
    assert (root / "meta/info.json").stat().st_mode & 0o777 == 0o644
    # Remove all remaining episodes. An empty result stays a valid dataset.
    stage = base / "empty"
    rebuild(root, stage, {0, 1}, lambda _: None)
    assert validate_dataset(stage, deep=True).valid
    assert json.loads((stage / "meta/info.json").read_text())["total_frames"] == 0


def recovery(base, publish):
    root = base / "dataset"
    fixture(root, 6)
    store = Store(base / "state")
    op = "b" * 32
    entries = {
        "1": {"episode": 1, "deleted_at": 1},
        "2": {"episode": 2, "deleted_at": 2},
    }
    row = {
        "id": "fixture",
        "dataset_path": str(root),
        "revision": 0,
        "entries": entries,
        "operation": {
            "id": op,
            "state": "running",
            "episodes": [1],
            "entries_before": copy.deepcopy(entries),
        },
    }
    stage = base / f".dataset.recycle-{op}"
    result = rebuild(root, stage, {1}, lambda _: None)
    (stage / MARKER).write_text(json.dumps({**result, "operation": op}))
    if publish:
        _exchange(root, stage)
    result = recover_operation(store, row)
    assert result["operation"]["state"] == ("completed" if publish else "failed")
    assert not stage.exists()
    if publish:
        assert_kept(root)
    else:
        assert open_dataset(root).metadata.total_episodes == 3


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="pipeline-recycle-test-") as temp:
        base = Path(temp)
        for gop in [6, 18]:
            folder = base / f"gop{gop}"
            folder.mkdir()
            scenario(folder, gop)
        legacy = base / "legacy"
        legacy.mkdir()
        scenario(legacy, 6, legacy=True)
        for published in [False, True]:
            folder = base / f"recovery-{published}"
            folder.mkdir()
            recovery(folder, published)
    print(
        "Shared-shard removal, non-keyframe video boundaries, full cleanup, and crash recovery passed."
    )
