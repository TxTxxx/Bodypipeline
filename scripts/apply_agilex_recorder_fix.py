#!/usr/bin/env python3
"""Apply the stream-stability fix to an AgileX data_tools checkout.

The patch is intentionally kept outside the legacy ``data_ros`` package so the
Bodypipeline repository remains the source of truth for the robot-side fix.
By default this command is a dry-run. Pass ``--apply`` to modify the checkout.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "patches/agilex-data-tools/0001-recorder-stream-stability.patch"
SOURCE_REL = Path("src/data_tools/src/dataCapture.cpp")
HEADER_REL = Path("src/data_tools/include/blockingDeque.h")
INSTALLED_REL = Path("install/data_tools/lib/data_tools/data_tools_dataCapture")


def run(command: list[str], cwd: Path | None = None, check: bool = True):
    print("+", " ".join(command))
    result = subprocess.run(command, cwd=cwd)
    if check and result.returncode:
        raise RuntimeError(f"命令失败，退出码 {result.returncode}")
    return result


def already_applied(data_ros: Path) -> bool:
    source = data_ros / SOURCE_REL
    header = data_ros / HEADER_REL
    return (
        source.is_file()
        and header.is_file()
        and "waitForStableStreams" in source.read_text()
        and "void clear()" in header.read_text()
    )


def backup(data_ros: Path, backup_root: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = backup_root / f"recorder-stream-stability-{stamp}"
    target.mkdir(parents=True, exist_ok=False)
    for rel in (SOURCE_REL, HEADER_REL, INSTALLED_REL):
        source = data_ros / rel
        if not source.exists():
            continue
        destination = target / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return target


def build(data_ros: Path) -> None:
    command = (
        "set -e; "
        "source /opt/ros/humble/setup.bash; "
        f"cd {shlex_quote(str(data_ros))}; "
        "colcon build --packages-select data_tools --symlink-install "
        "--cmake-args -DCMAKE_BUILD_TYPE=Release"
    )
    run(["bash", "-lc", command])


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-ros",
        type=Path,
        default=Path(os.environ.get("DATA_ROS_ROOT", "/path/to/robot_ws")),
        help="data_ros workspace root (default: /path/to/robot_ws)",
    )
    parser.add_argument(
        "--backup-root",
        type=Path,
        default=None,
        help="backup directory (default: <data_ros>/../bodypipeline/backups)",
    )
    parser.add_argument("--apply", action="store_true", help="apply the patch")
    parser.add_argument("--build", action="store_true", help="run colcon after apply")
    parser.add_argument(
        "--restart-recorder",
        action="store_true",
        help="restart bodypipeline-recorder after a successful build",
    )
    args = parser.parse_args()

    data_ros = args.data_ros.expanduser().resolve()
    if not PATCH.is_file():
        raise SystemExit(f"找不到补丁：{PATCH}")
    if not (data_ros / SOURCE_REL).is_file():
        raise SystemExit(f"不是有效的 data_ros 工作区：{data_ros}")
    if already_applied(data_ros):
        print("补丁已经应用，无需重复修改。")
        return 0

    dry_run = run(
        ["patch", "-p1", "--dry-run", "--forward", "-i", str(PATCH)],
        cwd=data_ros,
        check=False,
    )
    if dry_run.returncode:
        raise SystemExit("补丁无法干净应用；未修改任何文件")
    if not args.apply:
        print("Dry-run 通过。确认后加 --apply；需要编译时再加 --build。")
        return 0

    backup_root = args.backup_root
    if backup_root is None:
        backup_root = data_ros.parent / "bodypipeline/backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    saved = backup(data_ros, backup_root)
    print(f"备份：{saved}")

    result = run(
        ["patch", "-p1", "--forward", "-i", str(PATCH)],
        cwd=data_ros,
        check=False,
    )
    if result.returncode:
        raise SystemExit(f"应用补丁失败；原文件备份在 {saved}")
    if args.build:
        try:
            build(data_ros)
        except RuntimeError:
            print(f"编译失败；源码已修改，旧文件备份在 {saved}", file=sys.stderr)
            return 1
    if args.restart_recorder:
        if not args.build:
            raise SystemExit("--restart-recorder 需要同时指定 --build")
        run(["systemctl", "--user", "restart", "bodypipeline-recorder"])
    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
