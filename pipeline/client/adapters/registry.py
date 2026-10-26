"""Trusted capture bindings, including robot-specific sealing and bridge code."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace

from pipeline.shared.capture_formats import ARX_HDF5_FORMAT, CAPTURE_FILENAME
from pipeline.shared.core import DomainError, digest
from pipeline.shared.hdf5 import AGILEX_HDF5_FORMAT, package_agilex_capture

from .disabled import DisabledCaptureAdapter, unavailable
from .http import HttpCaptureAdapter
from .mock import MockAdapter


@dataclass(frozen=True)
class CaptureBackend:
    id: str
    robot_type: str | None
    modes: tuple[str, ...]
    raw_format: str
    package: Callable
    filename: str = CAPTURE_FILENAME
    shell_path: bool = False
    profile_handshake: bool = False
    video_source: Callable | None = None

    def validate_path(self, raw):
        if self.shell_path and not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(raw)):
            raise DomainError(
                "旧 recorder 的 shell 路径限制：本地根只能包含受控 ASCII 路径字符", 400
            )

    def adapter(self, settings, profile):
        if self.id == "disabled":
            return DisabledCaptureAdapter()
        if settings.mode == "mock":
            return MockAdapter(profile)
        return HttpCaptureAdapter(
            settings,
            expected_profile_hash=digest(profile) if self.profile_handshake else None,
        )


def _arx_package(*args, **kwargs):
    from pipeline.shared.arx import package_arx_capture

    return package_arx_capture(*args, **kwargs)


def _arx_kinesthetic_package(*args, **kwargs):
    from pipeline.shared.arx_kinesthetic import package
    return package(*args, **kwargs)


def _arx_video_source(path, profile):
    from ..video_providers.hdf5 import Hdf5VideoSource

    return Hdf5VideoSource(path, profile, "observations/images/{camera}",
                           [("timestamps/reference_source_ns", 1e-9), ("timestamps/monotonic", 1.0)])


def _arx_kinesthetic_video_source(path, profile):
    from ..video_providers.hdf5 import Hdf5VideoSource

    return Hdf5VideoSource(path, profile, "observation.images.{camera}",
                           [("timestamps/monotonic", 1.0)])


_BACKENDS = {
    "arx_kinesthetic": CaptureBackend("arx_kinesthetic", "arx_x5", ("ros2", "mock"),
                                      "arx_kinesthetic_hdf5_v1", _arx_kinesthetic_package,
                                      video_source=_arx_kinesthetic_video_source),
    "disabled": CaptureBackend("disabled", None, ("mock", "ros2"), "", unavailable),
    "agilex_mock": CaptureBackend(
        "agilex_mock",
        "agilex_aloha",
        ("mock",),
        AGILEX_HDF5_FORMAT,
        package_agilex_capture,
    ),
    "agilex_ros2": CaptureBackend(
        "agilex_ros2",
        "agilex_aloha",
        ("ros2",),
        AGILEX_HDF5_FORMAT,
        package_agilex_capture,
        shell_path=True,
    ),
    "arx_ros2": CaptureBackend(
        "arx_ros2",
        "arx_x5",
        ("ros2",),
        ARX_HDF5_FORMAT,
        _arx_package,
        profile_handshake=True,
        video_source=_arx_video_source,
    ),
}


def get_capture_backend(settings, profile, backend_id=None):
    key = backend_id or settings.capture_backend
    if key is None:
        # Compatibility only for existing AgileX deployments.
        key = {"mock": "agilex_mock", "ros2": "agilex_ros2"}.get(settings.mode)
    backend = _BACKENDS.get(key) if isinstance(key, str) else None
    if backend is None or settings.mode not in backend.modes:
        raise DomainError(f"未知或模式不匹配的采集后端：{key!r}")
    if (
        backend.robot_type is not None
        and profile.get("robot_type") != backend.robot_type
    ):
        raise DomainError("采集后端与机器人 Profile 不匹配，必须显式绑定")
    return backend


def create_capture_bridge(settings):
    backend = get_capture_backend(settings, settings.profile())
    if backend.id == "agilex_ros2":
        from pipeline.client.bridge import create_bridge
    elif backend.id == "arx_ros2":
        from pipeline.client.arx_bridge import create_bridge
    else:
        raise DomainError("该采集后端没有 Bridge 实现")
    return create_bridge(settings)


def backend_for_record(row):
    """Sealing and historical grouping use the saved binding, not today's config."""
    return get_capture_backend(
        SimpleNamespace(mode=row["mode"], capture_backend=row.get("capture_backend")),
        row["profile"],
    )
