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

    def analysis_context(self, profile: dict) -> dict:
        """Facts a downstream analysis engine needs that the dataset does not carry.

        Argus infers camera mounting from camera names and refuses to assume it.
        When the profile states where each camera sits, that fact belongs here so
        the analysis can qualify its cross-modal checks instead of skipping them.
        Nothing in this mapping may be invented: an absent fact stays absent and
        the engine reports the affected check as not assessed.
        """
        return {}


__all__ = ["RobotProvider", "RobotRuntime"]
