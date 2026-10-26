"""Quarantine uploaded copies using their original upload receipts."""

import json
import subprocess
from pathlib import Path, PurePosixPath

import httpx

from pipeline.shared.core import DomainError

from .ssh_upload import ssh_arguments, upload_destination


def trash_uploaded_copy(settings, row):
    state = row.get("upload_state") or "NONE"
    if state == "NONE":
        return None
    receipt = row.get("receipt") or {}
    if state == "ACKED" and receipt.get("transport") != "ssh-rsync":
        with httpx.Client(base_url=settings.server_url, timeout=30, trust_env=False,
                          headers={"Authorization": "Bearer " + settings.device_token}) as http:
            response = http.post(f"/device/uploads/{row['upload_id']}/trash")
            if response.status_code >= 400:
                raise DomainError("接收端回收失败：" + response.text)
            result = response.json()
            if result.get("state") != "TRASHED":
                raise DomainError("接收端未确认回收完成")
            return result

    config = dict(settings.capture["upload_ssh"])
    if receipt and (receipt.get("host"), receipt.get("port")) != (config["host"], config["port"]):
        raise DomainError("该片段的上传服务器与当前配置不同，未操作远端文件")
    config["identity_file"] = config["trash_identity_file"]
    _, destination = upload_destination(settings, row)
    destination = row.get("remote_path") or receipt.get("remote_path") or destination
    remote = PurePosixPath(destination)
    roots = [config["root"], *config.get("previous_roots", [])]
    root = next((value for value in roots if remote.is_relative_to(PurePosixPath(value))), None)
    if root is None:
        raise DomainError("该片段的远端路径不属于已配置的上传目录")
    relative = remote.relative_to(PurePosixPath(root))
    expected = receipt.get("sha256")
    manifest_path = Path(row["raw_root"]).parent / "manifest.json"
    if not expected and manifest_path.is_file():
        expected = json.loads(manifest_path.read_text())["files"][0]["sha256"]
    request = {"root": root, "relative_path": str(relative), "session_id": row["id"], "sha256": expected}
    command = [*ssh_arguments(config), f"{config['user']}@{config['host']}", "trash"]
    result = subprocess.run(command, input=json.dumps(request), capture_output=True,
                            text=True, timeout=120)
    if result.returncode:
        raise DomainError("远端回收失败：" + result.stderr.strip())
    response = json.loads(result.stdout)
    if response.get("trashed") is not True:
        raise DomainError("远端未确认回收完成")
    return response
