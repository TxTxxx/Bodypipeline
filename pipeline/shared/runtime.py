"""Trusted Server-side robot runtime definitions.

A runtime binds an authenticated robot identity to one Server-owned Profile and
one statically registered conversion provider. Client manifests never select
code paths directly; they can only declare data that must match this binding.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RobotRuntime:
    id: str
    profile: dict = field(repr=False)
    profile_hash: str
    provider: str
    raw_formats: tuple[str, ...]
    accepted_capture_profile_hashes: tuple[str, ...] = ()
    label: str | None = None

    @property
    def robot_type(self) -> str:
        return self.profile.get("robot_type", "")

    def accepts_profile_hash(self, profile_hash: str) -> bool:
        return profile_hash == self.profile_hash or (
            profile_hash in self.accepted_capture_profile_hashes
        )

    def public(self) -> dict:
        return {
            "id": self.id,
            "robot_type": self.robot_type,
            "provider": self.provider,
            "raw_formats": list(self.raw_formats),
            "profile_hash": self.profile_hash,
            "profile_id": self.profile.get("id"),
            "label": self.label or self.robot_type,
        }
