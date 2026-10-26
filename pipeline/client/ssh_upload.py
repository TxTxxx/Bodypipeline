"""Upload sealed packages to a directory restricted by an rrsync SSH key."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from collections import deque
from pathlib import Path, PurePosixPath

from pipeline.shared.core import DomainError, digest, file_hash


def ssh_arguments(config):
    return [
        "ssh",
        "-p", str(config["port"]),
        "-i", str(config["identity_file"]),
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "UserKnownHostsFile=" + str(config["known_hosts_file"]),
        "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
    ]


def upload_destination(settings, row):
    config = settings.capture["upload_ssh"]
    public = row.get("published_path") or row.get("saved_path")
    relative = Path(public).relative_to(Path(settings.capture["data_root"]))
    if not relative.parts or any(part in {".", ".."} for part in relative.parts):
        raise DomainError("上传文件必须位于采集数据根目录内")
    return relative, str(PurePosixPath(config["root"]) / relative.as_posix())


def upload_package(settings, row, manifest, on_progress):
    """Keep the public task/collector/date/name and resume interrupted copies.

    The receiver's key forces rrsync into config['root']; its destination must
    therefore be relative, never a second absolute path below that root.
    """
    config = settings.capture["upload_ssh"]
    relative, remote_path = upload_destination(settings, row)
    if len(manifest["files"]) != 1:
        raise DomainError("SSH 上传目前支持单文件采集包")
    spec = manifest["files"][0]
    source = Path(row["raw_root"]).parent / spec["path"]
    if file_hash(source) != spec["sha256"]:
        raise DomainError("封存后文件发生变化，不能上传")

    # A hard link gives rsync the public filename without copying the package.
    staging = Path(row["raw_root"]).parent / "ssh-upload"
    staged = staging / relative
    staged.parent.mkdir(parents=True, exist_ok=True)
    if not staged.exists():
        os.link(source, staged)
    elif not staged.samefile(source):
        raise DomainError("上传暂存文件与当前采集包不一致")

    source_arg = str(staging) + "/./" + relative.as_posix()
    command = [
        "rsync",
        "--relative",
        "--checksum",
        "--partial-dir=.rsync-partial",
        "--delay-updates",
        "--fsync",
        "--perms",
        "--chmod=Du=rwx,Dg=rwx,Do=rx,Fu=rw,Fg=rw,Fo=r",
        "--info=progress2",
        "--outbuf=L",
        "--timeout=120",
        "-e", shlex.join(ssh_arguments(config)),
        "--",
        source_arg,
        f"{config['user']}@{config['host']}:.",
    ]
    output = deque(maxlen=12)
    last_update = 0.0
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env={**os.environ, "LC_ALL": "C"},
    ) as process:
        for line in process.stdout:
            if line.strip():
                output.append(line.strip())
            progress = re.match(r"\s*([\d,]+)\s+\d+%", line)
            if progress and time.monotonic() - last_update >= 1:
                on_progress(min(int(progress[1].replace(",", "")), spec["size_bytes"]))
                last_update = time.monotonic()
        if process.wait():
            raise DomainError("SSH 上传失败：" + "\n".join(output))

    on_progress(spec["size_bytes"])
    return {
        "id": row["id"],
        "state": "COMMITTED",
        "manifest_hash": digest(manifest),
        "transport": "ssh-rsync",
        "host": config["host"],
        "port": config["port"],
        "remote_path": remote_path,
        "size_bytes": spec["size_bytes"],
        "sha256": spec["sha256"],
        "transfer_verified_by": "rsync checksum",
    }
