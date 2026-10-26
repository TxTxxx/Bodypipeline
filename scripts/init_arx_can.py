#!/usr/bin/python3 -I
"""Root-owned deployment helper: initialize exactly one fixed ARX CAN adapter.

Install as /usr/local/libexec/bodypipeline-can-init, owned by root, mode 0755.
The Client account is authorised for only can1, can3 and can6 through sudoers.
This helper neither starts robot controllers nor publishes robot commands.
"""

import fcntl
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

INTERFACES = ("can1", "can3", "can6")
IP = "/usr/bin/ip"
SLCAND = "/usr/bin/slcand"


def link_state(name):
    result = subprocess.run(
        [IP, "-o", "link", "show", name], capture_output=True, text=True, timeout=3
    )
    return result.returncode == 0, bool(re.search(r"<[^>]*\bUP\b", result.stdout))


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=5)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or "CAN 初始化命令失败")


def initialize(name):
    if name not in INTERFACES:
        raise ValueError("只允许 can1、can3、can6")
    device = Path("/dev/arx" + name)
    if not device.exists():
        raise RuntimeError(f"找不到 {device}，请检查 USB/CAN 连接和设备供电")
    real = device.resolve()
    if not re.fullmatch(r"/dev/tty(?:ACM|USB)[0-9]+", str(real)) or not stat.S_ISCHR(real.stat().st_mode):
        raise RuntimeError(f"{device} 没有映射到有效的串口 CAN 设备")
    with open("/run/lock/bodypipeline-" + name + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        exists, up = link_state(name)
        if up:
            return
        if not exists:
            command([SLCAND, "-o", "-f", "-s8", str(device), name])
            deadline = time.monotonic() + 3
            while not link_state(name)[0]:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"创建 {name} 超时，请检查 {device} 是否被占用")
                time.sleep(0.1)
        command([IP, "link", "set", name, "up"])
        if not link_state(name)[1]:
            raise RuntimeError(f"{name} 初始化后仍未启动")


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in INTERFACES:
        print("用法：bodypipeline-can-init can1|can3|can6", file=sys.stderr)
        return 2
    if os.geteuid() != 0:
        print("CAN 初始化需要已配置的服务权限", file=sys.stderr)
        return 1
    try:
        initialize(sys.argv[1])
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(sys.argv[1] + " 已就绪")
    return 0


if __name__ == "__main__":
    sys.exit(main())
