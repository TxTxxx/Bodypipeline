"""CPU-only, read-only exact-frame decoding in the isolated media toolchain."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def describe(root, index):
    from letools import open_dataset

    source = open_dataset(root)
    episode = next((e for e in source.episodes if e.index == index), None)
    if episode is None:
        raise ValueError("Episode 不存在")
    table = source.read_episode(episode)
    fps = float(source.metadata.info["fps"])
    timestamps = (
        table["timestamp"].to_pylist()
        if "timestamp" in table.column_names
        else [i / fps for i in range(episode.length)]
    )
    first = timestamps[0]
    times = [float(t - first) for t in timestamps]
    if any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("数据时间戳必须严格递增")
    cameras = []
    for key, video in episode.videos.items():
        path = Path(video.path).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError("媒体路径无效")
        cameras.append(
            {
                "key": key,
                "path": str(path.relative_to(root.resolve())),
                "start": float(video.start),
                "end": float(video.end),
            }
        )
    return {
        "episode": index,
        "frames": episode.length,
        "fps": fps,
        "timestamps": times,
        "duration": episode.length / fps,
        "cameras": cameras,
    }


def decode(root, index, frame, camera, output):
    import av

    info = describe(root, index)
    if not 0 <= frame < info["frames"]:
        raise ValueError("数据帧越界")
    video = next((v for v in info["cameras"] if v["key"] == camera), None)
    if video is None:
        raise ValueError("相机不存在")
    target = video["start"] + info["timestamps"][frame]
    best, distance, pts = None, float("inf"), None
    with av.open(str(root / video["path"])) as container:
        stream = container.streams.video[0]
        stream.codec_context.thread_count = 1
        container.seek(
            max(0, int(target / float(stream.time_base))), stream=stream, backward=True
        )
        for image in container.decode(stream):
            if image.pts is None:
                continue
            time = float(image.pts * image.time_base)
            if time < video["start"] - 1e-6:
                continue
            if time >= video["end"] - 1e-6:
                break
            delta = abs(time - target)
            if delta < distance:
                best, distance, pts = image.to_image(), delta, time
            if time >= target:
                break
    if best is None or distance > 0.51 / info["fps"]:
        raise ValueError("此数据帧没有可对齐的视频画面，不能显示伪精确帧")
    best.save(output, format="PNG")
    return {
        "frame": frame,
        "data_timestamp": info["timestamps"][frame],
        "media_pts": pts,
        "camera": camera,
    }


if __name__ == "__main__":
    root, index = Path(sys.argv[2]), int(sys.argv[3])
    if sys.argv[1] == "describe":
        result = describe(root, index)
    else:
        result = decode(root, index, int(sys.argv[4]), sys.argv[5], Path(sys.argv[6]))
    print(json.dumps(result, ensure_ascii=False))
