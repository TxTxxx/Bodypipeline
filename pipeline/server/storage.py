"""Data paths are separate from local service state and pinned per record."""

from __future__ import annotations

import re
import stat
from pathlib import Path

from pipeline.shared.core import DomainError, confined, durable_mkdir

ROBOT_LAYOUT = "robot_type_v1"
AREAS = ("incoming", "raw", "work", "analyses")


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
        data_directory(settings, row, area)


def share_data_path(path: Path) -> None:
    """Grant a+rwX on a data entry without following alignment symlinks.

    Do not change the process umask or atomic_json: service state and credentials
    must keep their private permissions. Skip chmod when access is already granted
    so shared directories owned by another user remain usable.
    """
    entry = path.lstat()
    if stat.S_ISLNK(entry.st_mode):
        return
    if not (stat.S_ISDIR(entry.st_mode) or stat.S_ISREG(entry.st_mode)):
        raise DomainError(f"共享数据包含非普通文件或目录：{path}", 500)
    mode = stat.S_IMODE(entry.st_mode)
    access = 0o777 if stat.S_ISDIR(entry.st_mode) or mode & 0o111 else 0o666
    if mode & access != access:
        path.chmod(mode | access, follow_symlinks=False)


def share_data_tree(root: Path) -> None:
    """Normalize externally generated data, including atomic JSON replacements."""
    share_data_path(root)
    if root.is_symlink() or not root.is_dir():
        return
    # Path.walk is unavailable on Python 3.10. Explicit recursion also ensures
    # alignment links never lead chmod into imported source data.
    for path in root.iterdir():
        share_data_tree(path)


def data_directory(
    settings, row: dict, area: str, relative: str = "", *, exist_ok: bool = True
) -> Path:
    """Create shared data directories and make their managed parents accessible."""
    path = storage_path(settings, row, area, relative)
    if exist_ok:
        durable_mkdir(path)
    else:
        path.mkdir(parents=True, exist_ok=False)
    base = (
        settings.data_root if row.get("data_layout") == ROBOT_LAYOUT else settings.root
    )
    for directory in reversed([path, *path.parents]):
        if not directory.is_relative_to(base):
            continue
        if directory == settings.root:
            # Legacy data shares a parent with private service state. Only permit
            # traversal through this root; never expose its other contents.
            mode = stat.S_IMODE(directory.stat().st_mode)
            if mode & 0o111 != 0o111:
                directory.chmod(mode | 0o111, follow_symlinks=False)
        else:
            share_data_path(directory)
    return path
