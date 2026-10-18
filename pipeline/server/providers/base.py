"""Provider interfaces and the trusted runtime identity passed between services."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pipeline.shared.runtime import RobotRuntime


class RobotProvider(Protocol):
    """Robot-specific conversion boundary.

    Providers are selected only from Server code. Profiles and manifests cannot
    import modules or name arbitrary Python callables.
    """

    id: str

    def validate_profile(self, profile: dict) -> None: ...

    def profile_blockers(self, profile: dict) -> list[dict]: ...

    def prepare(self, raw: Path, dest: Path, profile: dict) -> dict: ...

    def quality(
        self, aligned: Path, profile: dict, synthetic: bool = False
    ) -> dict: ...

    def conversion_args(
        self,
        *,
        executable: str,
        source: Path,
        destination: Path,
        profile: dict,
        job: dict,
    ) -> list[str]: ...

    def patch_dataset_info(
        self,
        dataset: Path,
        profile: dict,
        *,
        synthetic: bool = False,
    ) -> None: ...

    def lineage_metadata(self, profile: dict) -> dict: ...


__all__ = ["RobotProvider", "RobotRuntime"]
