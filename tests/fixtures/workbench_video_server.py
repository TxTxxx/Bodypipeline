"""Isolated video-only acceptance server; real sources are registered read-only.

From repo root: .venv/bin/python tests/fixtures/workbench_video_server.py ROOT --port 18132
ROOT contains workbench_video_scenario's dataset; all state/cache stays in ROOT.
Never uses any existing service configuration, credentials, or AI runner.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def app_for(root, port):
    from pipeline.server.app import create_app
    from pipeline.server.viewer_data import PreviewRegistry
    from pipeline.shared.config import Settings

    settings = Settings(
        role="server",
        root=root / "state",
        trusted_network=True,
        profile_path=str(REPO / "configs/agilex-aloha.json"),
        letools_bin=str(REPO / ".runtime/letools-venv/bin/letools"),
        import_roots=[root, Path("/home/txtxx/data")],
        min_free_bytes=0,
        server_url=f"http://127.0.0.1:{port}",
    )
    app = create_app(settings, background=False)
    store = app.state.runner.store
    registry = PreviewRegistry(store, settings.viewer_ttl)
    for key, source in (
        ("video-fixture", root / "dataset"),
        ("local-screw-04", Path("/home/txtxx/data/robot_screw_04")),
    ):
        if not source.is_dir():
            continue
        store.put(
            "job",
            key,
            {
                "id": key,
                "state": "CONVERTED",
                "dataset_ready": True,
                "dataset_path": str(source),
                "dataset_name": key,
                "created_at": 1,
                "robot_type": "video_probe",
                "target_format": "v3.0",
            },
        )
        registry.register(key, source)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--port", type=int, default=18132)
    args = parser.parse_args()
    uvicorn.run(
        app_for(args.root.resolve(), args.port),
        host="127.0.0.1",
        port=args.port,
        access_log=False,
    )
