"""Batch admission policy and explicit, recording-local quality rejections."""

from __future__ import annotations

from typing import Literal

from pipeline.shared.core import DomainError

InvalidEpisodePolicy = Literal["fail", "skip"]


def validate_episode_policy(value: str) -> InvalidEpisodePolicy:
    if value not in ("fail", "skip"):
        raise DomainError("invalid_episode_policy 必须为 fail 或 skip", 400)
    return value


class EpisodeValidationError(DomainError):
    """A provider positively identified invalid data in one recording.

    Only this error is eligible for skipping. Configuration, integrity, tool,
    I/O and unexpected implementation failures remain fatal to the batch.
    """

    def __init__(self, message: str, *, code="invalid_episode", details=None):
        super().__init__(message)
        self.reason_code = code
        self.details = details or {}
