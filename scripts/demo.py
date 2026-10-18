#!/usr/bin/env python3
"""Start local mock client, demo server, and unauthenticated viewer gateway.
No SSH or ROS calls. Generated credentials stay in a mode-0600 local file.
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument(
    "--real-processing",
    action="store_true",
    help="Use real LeTools and Doctor on synthetic captures; requires toolchain installation",
)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
os.chdir(root)
state = root / ".runtime"
state.mkdir(exist_ok=True)
credentials = state / "local-credentials.json"
if not credentials.exists():
    fd = os.open(credentials, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(
            {
                "PIPELINE_TOKEN": secrets.token_urlsafe(32),
                "PIPELINE_DEVICE_TOKEN": secrets.token_urlsafe(32),
                "PIPELINE_BRIDGE_TOKEN": secrets.token_urlsafe(32),
            },
            f,
            indent=2,
        )
env = {**os.environ, **json.loads(credentials.read_text())}
local_server = json.loads((root / "configs/server.demo.json").read_text())
if args.real_processing:
    local_server["processing_mode"] = "real"
    tool = root / ".runtime/letools-venv/bin/letools"
    if tool.is_file():
        local_server["letools_bin"] = str(tool)
bun = root / ".runtime/viewer-toolchain/node_modules/.bin/bun"
if bun.exists():
    local_server["viewer_bun"] = str(bun)
local_config = state / "server.local.json"
local_config.write_text(json.dumps(local_server, indent=2))
children = []
try:
    for role, config in [
        ("server", str(local_config)),
        ("viewer", str(local_config)),
        ("client", "configs/client.mock.json"),
    ]:
        children.append(
            subprocess.Popen(
                [sys.executable, "-m", "pipeline.cli", role, "--config", config],
                env=env,
            )
        )
    print(
        "\nClient: http://127.0.0.1:8080\nServer: http://127.0.0.1:8081\nViewer: http://127.0.0.1:8082/",
        flush=True,
    )
    print(
        f"Credentials: {credentials}\nUse PIPELINE_TOKEN to log in to Client/Server; Viewer requires no login. Ctrl-C stops these local processes.\n",
        flush=True,
    )
    while True:
        import time

        time.sleep(0.5)
        if any(p.poll() is not None for p in children):
            raise RuntimeError("One service exited; inspect logs above")
except KeyboardInterrupt:
    pass
finally:
    for p in children:
        if p.poll() is None:
            p.terminate()
    for p in children:
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
