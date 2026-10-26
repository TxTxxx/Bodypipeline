from __future__ import annotations

import argparse

import uvicorn

from pipeline.shared.config import Settings


def main():
    parser = argparse.ArgumentParser(
        description="ARX X5 capture client and ROS2 bridge"
    )
    parser.add_argument("role", choices=["client", "bridge"])
    parser.add_argument("--config")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    settings = Settings.load(args.role, args.config)
    if args.role == "client":
        from pipeline.client.app import create_app
    else:
        if args.host not in ("127.0.0.1", "::1"):
            parser.error("ROS bridge 必须只监听 loopback")
        if len(settings.bridge_token) < 24:
            parser.error("Bridge 必须设置 PIPELINE_BRIDGE_TOKEN")
        from pipeline.client.adapters.registry import (
            create_capture_bridge as create_app,
        )
    port = (
        args.port
        or {"client": 8080, "bridge": 8090}[args.role]
    )
    app = create_app(settings)
    uvicorn.run(
        app, host=args.host, port=port, workers=1, proxy_headers=False, access_log=False
    )


if __name__ == "__main__":
    main()
