"""Build small, real datasets with videos and different local task indices."""

import json
import sys
from fractions import Fraction
from pathlib import Path

import av
import h5py
import numpy as np
from letools import (
    ConversionConfig,
    HDF5Mapping,
    HDF5NumericField,
    HDF5Source,
    HDF5VideoField,
    convert,
    open_dataset,
    validate_dataset,
)


def create(root, version):
    mapping = HDF5Mapping(
        fps=10,
        numeric_fields=(
            HDF5NumericField("observations/qpos", "observation.state"),
            HDF5NumericField("action", "action"),
        ),
        video_fields=(
            HDF5VideoField(
                "observations/images/front",
                "observation.images.front",
                width=32,
                height=24,
            ),
        ),
        task_key="language_instruction",
        robot_type="test_arm",
    )
    for ordinal in range(2):
        raw = root / f"raw-{ordinal}"
        raw.mkdir(parents=True)
        for episode, length in enumerate((6, 5)):
            state = (
                np.arange(length * 2, dtype=np.float32).reshape(length, 2) * 0.01
                + ordinal
            )
            with h5py.File(raw / f"episode_{episode}.hdf5", "w") as handle:
                handle.create_dataset("observations/qpos", data=state)
                handle.create_dataset("action", data=state + 0.05)
                video = handle.create_dataset(
                    "observations/images/front",
                    shape=(length,),
                    dtype=h5py.vlen_dtype(np.dtype("uint8")),
                )
                codec = av.CodecContext.create("mjpeg", "w")
                codec.width, codec.height = 32, 24
                codec.pix_fmt, codec.time_base = "yuvj420p", Fraction(1, 10)
                for frame in range(length):
                    pixels = np.full(
                        (24, 32, 3),
                        ordinal * 70 + episode * 30 + frame * 2,
                        dtype=np.uint8,
                    )
                    packet = codec.encode(
                        av.VideoFrame.from_ndarray(pixels, format="rgb24")
                    )[0]
                    video[frame] = np.frombuffer(bytes(packet), dtype=np.uint8)
                handle.create_dataset(
                    "language_instruction",
                    data=("pick", "place")[(episode + ordinal) % 2],
                    dtype=h5py.string_dtype("utf-8"),
                )
        convert(
            HDF5Source(raw, mapping),
            root / f"input-{ordinal}",
            version,
            config=ConversionConfig(workers=1, video_workers=1),
        )


def inspect(path):
    source = open_dataset(path)
    assert validate_dataset(path, deep=True).valid
    return {
        "episodes": source.metadata.total_episodes,
        "frames": source.metadata.total_frames,
        "indices": [e.index for e in source.episodes],
        "tasks": [list(e.tasks) for e in source.episodes],
        "rows": [source.read_episode(e).to_pylist() for e in source.episodes],
    }


if __name__ == "__main__":
    if sys.argv[1] == "create":
        create(Path(sys.argv[2]), sys.argv[3])
    else:
        print(json.dumps(inspect(Path(sys.argv[2]))))
