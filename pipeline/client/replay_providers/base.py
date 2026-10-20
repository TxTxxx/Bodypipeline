"""Client replay contract, independent of ROS, file format and joint layout."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol, TypedDict


class ReplayInfo(TypedDict, total=False):
    supported: bool
    error: str
    frames: int
    fps: float | None
    format: str
    description: str
    metadata: dict


@dataclass(frozen=True)
class ReplayCapabilities:
    id: str
    version: str
    label: str
    robot_types: tuple[str, ...]
    file_extensions: tuple[str, ...]
    input_description: str
    simulated: bool = False
    progress_timeout: float = 15

    def public(self) -> dict:
        return asdict(self)


class ReplayProvider(Protocol):
    """Providers are selected by trusted local config, never request/file data.

    probe/prepare must not move hardware. Supported probe results include a
    positive frames count, format and description; fps can be absent for
    irregularly timed trajectories. Robot-specific information goes in metadata.
    prepare preserves the input and returns the executable artifact's path.
    command returns argv for a dedicated job process (SDK users supply a runner).
    frame normalizes tool progress to contiguous ZERO-BASED submitted-frame
    indices. All frames submitted means publication complete, not measured arrival.
    error parses fatal tool output. Stop/shutdown terminate only this job's process
    group; drivers and robot emergency-stop functions remain outside this service.
    """

    capabilities: ReplayCapabilities

    def __init__(self, config: dict, profile: dict): ...
    def probe(self, path: Path) -> ReplayInfo: ...
    def prepare(self, path: Path, cache: Path) -> Path: ...
    def command(self, path: Path) -> list[str]: ...
    def frame(self, line: str) -> int | None: ...
    def error(self, line: str) -> str | None: ...
