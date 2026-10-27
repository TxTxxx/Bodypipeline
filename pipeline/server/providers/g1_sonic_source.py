"""Direct raw-backed LeTools DatasetSource and isolated conversion entry point."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

# JobRunner invokes this file with the isolated LeTools Python from any cwd.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import numpy as np
import pyarrow as pa
from letools import ConversionConfig, VideoEncodingConfig, convert
from letools.model import (
    DatasetMetadata,
    Episode,
    EpisodeDataProfile,
    FrameSequence,
    MediaProfile,
)
from letools.plugins import DatasetSource

from pipeline.server.providers.g1_sonic import (
    SOURCE_DESCRIPTOR,
    G1SONICProvider,
    numeric_features,
)
from pipeline.server.providers.g1_sonic_raw import (
    RAW_FORMAT,
    ROBOT_TYPE,
    SCHEMA,
    index_recording,
    json_object,
    read_image,
    recording_files,
    regular_file,
)
from pipeline.shared.core import DomainError, digest, file_hash


def _stats(values: np.ndarray) -> dict:
    return {
        "min": np.atleast_1d(values.min(axis=0)).tolist(),
        "max": np.atleast_1d(values.max(axis=0)).tolist(),
        "mean": np.atleast_1d(values.mean(axis=0)).tolist(),
        "std": np.atleast_1d(values.std(axis=0)).tolist(),
        "count": [len(values)],
    }


@dataclass(frozen=True)
class G1SONICFrames(FrameSequence):
    path: Path
    source_key: str
    offsets: tuple[int, ...]
    width: int
    height: int
    source_stat: tuple[int, int]
    estimated_size_bytes: int
    encoded_format: str = "jpeg"

    @property
    def frame_count(self) -> int:
        return len(self.offsets)

    def read_batch(self, start: int, stop: int) -> tuple[bytes, ...]:
        if not 0 <= start <= stop <= self.frame_count:
            raise IndexError("Invalid G1 image batch bounds")
        stat = regular_file(self.path).stat()
        if (stat.st_size, stat.st_mtime_ns) != self.source_stat:
            raise DomainError("G1 raw source changed while encoding video")
        result, previous, encoded = [], None, b""
        with self.path.open("rb") as stream:
            for offset in self.offsets[start:stop]:
                if offset != previous:
                    encoded = read_image(stream, offset, self.source_key)
                    previous = offset
                result.append(encoded)
        return tuple(result)


class G1SONICSource(DatasetSource):
    """Expose checked raw packets as Arrow frames and encoded JPEG sequences."""

    def __init__(self, root: Path, profile: dict, task: str, *, raw: bool = False):
        G1SONICProvider().validate_profile(profile)
        if not isinstance(task, str) or not task.strip():
            raise DomainError("G1 conversion requires an explicit task")
        self.root, self.profile = root.absolute(), profile
        if raw:
            paths = (
                sorted(root.glob("episodes/episode_*/packets.raw"))
                if root.is_dir()
                else []
            )
            sources = (
                [recording_files(p) for p in paths]
                if paths
                else [recording_files(root)]
            )
            descriptors = [
                {"raw_path": str(p), "episode": ep, "robot_config": cfg}
                for p, ep, cfg in sources
            ]
        else:
            paths = sorted(
                root.glob(f"episode*/{SOURCE_DESCRIPTOR}"),
                key=lambda p: int(p.parent.name.removeprefix("episode")),
            )
            descriptors = [json_object(p) for p in paths]
            if not descriptors:
                raise DomainError("No prepared G1 raw episodes found")
            for desc in descriptors:
                if desc.get("schema") != RAW_FORMAT or desc.get(
                    "profile_hash"
                ) != digest(profile):
                    raise DomainError("G1 prepared source/Profile binding mismatch")
        self._descriptors, self._offsets, self._profiles = descriptors, {}, {}
        episodes, features, total = [], None, 0
        for index, desc in enumerate(descriptors):
            recording = self._load(desc, check_images=raw)
            n = recording.report["frames"]
            if n < profile.get("minimum_frames", 2):
                raise DomainError(
                    "G1 conversion contains an episode shorter than minimum_frames"
                )
            self._offsets[index] = total
            arrays = self._arrays(recording, index, total)
            schema = numeric_features(profile)
            for key, values in arrays.items():
                schema.setdefault(
                    key,
                    {
                        "dtype": str(values.dtype),
                        "shape": list(values.shape[1:]) or [1],
                        "names": None,
                    },
                )
            videos = {}
            stat = recording.path.stat()
            for camera_id, frames in recording.cameras.items():
                key = f"observation.images.{camera_id}"
                schema[key] = {
                    "dtype": "video",
                    "shape": [3, frames.height, frames.width],
                    "names": ["channels", "height", "width"],
                    "info": {
                        "video.height": frames.height,
                        "video.width": frames.width,
                        "video.channels": 3,
                        "video.fps": profile["output_fps"],
                        "video.codec": "mjpeg",
                        "video.pix_fmt": "yuvj420p",
                        "video.is_depth_map": False,
                        "has_audio": False,
                    },
                }
                videos[key] = G1SONICFrames(
                    recording.path,
                    frames.source_key,
                    tuple(map(int, frames.offsets)),
                    frames.width,
                    frames.height,
                    (stat.st_size, stat.st_mtime_ns),
                    max(1, stat.st_size // len(recording.cameras)),
                )
            if features is not None and features != schema:
                raise DomainError("G1 numeric or camera schema differs across episodes")
            features = schema
            logical = sum(a.nbytes for a in arrays.values())
            self._profiles[index] = EpisodeDataProfile(
                str(recording.path), logical, logical, stat.st_size, n
            )
            episodes.append(
                Episode(
                    index,
                    n,
                    (task,),
                    {k: _stats(v) for k, v in arrays.items()},
                    recording.path,
                    data_end=n,
                    videos=videos,
                )
            )
            total += n
        info = {
            "codebase_version": RAW_FORMAT,
            "robot_type": ROBOT_TYPE,
            "total_episodes": len(episodes),
            "total_frames": total,
            "total_tasks": 1,
            "total_videos": len(episodes) * len(profile["cameras"]),
            "total_chunks": 1,
            "chunks_size": 1000,
            "fps": profile["output_fps"],
            "splits": {"train": f"0:{len(episodes)}"},
            "features": features,
            "data_path": None,
            "video_path": None,
            "g1_sonic": {
                **G1SONICProvider().lineage_metadata(profile),
                "schema": SCHEMA,
            },
        }
        self.metadata = DatasetMetadata(
            RAW_FORMAT,
            profile["output_fps"],
            features,
            ROBOT_TYPE,
            info["splits"],
            total,
            len(episodes),
            {0: task},
            info,
        )
        self.episodes = tuple(episodes)

    def _load(self, descriptor: dict, *, check_images: bool = False):
        return index_recording(
            Path(descriptor["raw_path"]),
            descriptor["episode"],
            self.profile,
            descriptor.get("robot_config"),
            check_images=check_images,
        )

    def _arrays(self, recording, index: int, offset: int) -> dict[str, np.ndarray]:
        length = recording.report["frames"]
        return {
            **recording.arrays,
            "timestamp": (np.arange(length) / self.profile["output_fps"]).astype(
                np.float32
            ),
            "frame_index": np.arange(length, dtype=np.int64),
            "episode_index": np.full(length, index, dtype=np.int64),
            "index": np.arange(offset, offset + length, dtype=np.int64),
            "task_index": np.zeros(length, dtype=np.int64),
        }

    def read_episode(self, episode: Episode) -> pa.Table:
        recording = self._load(self._descriptors[episode.index])
        arrays = self._arrays(recording, episode.index, self._offsets[episode.index])
        columns = {
            key: pa.array(values)
            if values.ndim == 1
            else pa.FixedSizeListArray.from_arrays(
                pa.array(values.reshape(-1)), values.shape[1]
            )
            for key, values in arrays.items()
        }
        return pa.table(columns)

    def data_profile(self, episode: Episode) -> EpisodeDataProfile:
        return self._profiles[episode.index]

    def media_profile(self, episode: Episode, key: str) -> MediaProfile:
        frames = episode.videos[key]
        return MediaProfile(
            str(frames.path), frames.estimated_size_bytes, "frame_sequence", True
        )

    def verify_sources(self) -> None:
        for descriptor in self._descriptors:
            path = regular_file(Path(descriptor["raw_path"]))
            if file_hash(path) != descriptor["episode"]["sha256"]:
                raise DomainError(
                    "G1 source changed during conversion; output will not be published"
                )

    def validate_destination(self, destination: Path) -> None:
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(destination)
        target = destination.resolve()
        roots = [self.root if self.root.is_dir() else self.root.parent]
        roots.extend(Path(d["raw_path"]).parent for d in self._descriptors)
        if any(target.is_relative_to(root.resolve()) for root in roots):
            raise DomainError(
                "G1 output must be outside the source recording directory"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--to", choices=("v2.1", "v3.0"), default="v3.0")
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Read native raw episodes instead of Server-prepared descriptors",
    )
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--video-workers", type=int, default=1)
    parser.add_argument("--video-codec-threads", type=int, default=1)
    args = parser.parse_args()
    if min(args.workers, args.video_workers, args.video_codec_threads) < 1:
        parser.error("worker counts must be positive")
    profile = json_object(args.profile)
    source = G1SONICSource(args.source, profile, args.task, raw=args.raw)
    destination = args.destination.absolute()
    source.validate_destination(destination)
    # Keep publication after source revalidation and semantic metadata writing.
    staging = destination.with_name(f".{destination.name}.g1-sonic-{uuid.uuid4().hex}")
    try:
        result = convert(
            source,
            staging,
            args.to,
            config=ConversionConfig(
                workers=args.workers,
                video_workers=args.video_workers,
                video_encoding=VideoEncodingConfig(
                    codec="libx264",
                    pixel_format="yuv420p",
                    codec_threads=args.video_codec_threads,
                ),
            ),
        )
        G1SONICProvider().patch_dataset_info(staging, profile)
        source.verify_sources()
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    print(
        json.dumps(
            {
                "destination": str(destination),
                "episodes": result.episodes,
                "frames": result.frames,
                "target_format": args.to,
                "source_format": RAW_FORMAT,
            }
        )
    )


if __name__ == "__main__":
    main()
