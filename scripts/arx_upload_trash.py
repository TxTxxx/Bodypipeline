"""Forced SSH command: move one uploaded HDF5 into the fixed arx/trash tree."""

import hashlib
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath


ROOT = Path("/data/share/robotsdata/arx")


def checksum(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def trash(root, request):
    relative = PurePosixPath(request["relative_path"])
    session = request["session_id"]
    if (relative.is_absolute() or len(relative.parts) != 4
            or any(part in {".", ".."} for part in relative.parts)
            or relative.parts[0] == "trash"
            or not re.fullmatch(r"\d{8}", relative.parts[2])
            or not re.fullmatch(r"episode[1-9]\d*\.hdf5", relative.name)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session)):
        raise ValueError("非法片段路径")
    root = root.resolve()
    source = root / relative
    target = root / "trash" / relative.parent / session / relative.name
    for path in [source, target]:
        if path.resolve() != path or not path.is_relative_to(root):
            raise ValueError("拒绝越界或符号链接路径")
    expected = request.get("sha256")
    existing = source if source.exists() else target
    if existing.exists() and not expected:
        raise ValueError("缺少上传摘要，未操作远端文件")
    if existing.exists() and expected and checksum(existing) != expected:
        raise ValueError("远端文件与该片段的上传摘要不一致")
    if source.exists():
        if target.exists():
            raise ValueError("回收站已有同名文件，未覆盖")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.rename(source, target)
    return {"trashed": True, "trash_path": str(target), "missing": not target.exists()}


if __name__ == "__main__":
    try:
        if os.environ.get("SSH_ORIGINAL_COMMAND") != "trash":
            raise ValueError("此密钥仅允许回收片段")
        request = json.loads(sys.stdin.read(16384))
        root = Path(request.get("root", str(ROOT)))
        if root not in {ROOT, Path("/data/share/robotsdata/ARX_X5")}:
            raise ValueError("未授权的回收目录")
        result = trash(root, request)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
