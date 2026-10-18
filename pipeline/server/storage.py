"""Data paths are separate from local service state and pinned per record."""

from __future__ import annotations

import re
from pathlib import Path

from pipeline.shared.core import DomainError, confined, durable_mkdir

ROBOT_LAYOUT = "robot_type_v1"
AREAS = ("incoming", "raw", "work")


def robot_type(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value
    ):
        raise DomainError("robot_type 必须为 1–64 位字母、数字、下划线或连字符", 400)
    return value


def storage_path(settings, row: dict, area: str, relative: str = "") -> Path:
    if area not in AREAS:
        raise ValueError("Unknown data area")
    layout = row.get("data_layout")
    if layout == ROBOT_LAYOUT:
        if settings.data_root is None or not settings.data_root.is_dir():
            raise DomainError("数据根目录不可用，请检查 data_root 和存储挂载", 503)
        base = settings.data_root
        parts = [robot_type(row.get("robot_type")), area, relative]
    elif layout is None:
        # Old records keep their paths until explicitly migrated offline.
        base = settings.root
        parts = [area, relative]
    else:
        raise DomainError("未知数据目录布局", 500)
    return confined(base, "/".join(part for part in parts if part))


def prepare_storage(settings, row: dict) -> None:
    for area in AREAS:
        durable_mkdir(storage_path(settings, row, area))
