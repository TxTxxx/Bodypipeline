"""Use actual HTTP serialization: mocked transports accept an invalid Bearer header."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient
from test_viewer import make_dataset

from pipeline.server.viewer import create_gateway
from pipeline.server.viewer_data import PreviewRegistry
from pipeline.shared.core import Store


@pytest.mark.parametrize("trusted", [True, False])
def test_gateway_proxies_workspace_and_related_routes_with_optional_token(
    settings_factory, tmp_path, trusted
):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.command, self.path, self.headers.get("Authorization")))
            if self.headers.get("If-None-Match") == '"saved-head"':
                self.send_response(304)
                self.send_header("ETag", '"saved-head"')
                self.end_headers()
                return
            payload = json.dumps(
                {
                    "operation": "manual_trim",
                    "source_job_id": "source",
                    "state": "CONVERTED",
                    "url": "http://viewer.example/bp/trim/episode_0",
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("ETag", '"saved-head"')
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_POST = do_GET

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        token = "" if trusted else "public-test-token-" + "x" * 32
        settings = settings_factory(
            "viewer",
            trusted_network=trusted,
            token=token,
            server_url=f"http://127.0.0.1:{server.server_port}",
        )
        store = Store(settings.root / "state")
        PreviewRegistry(store, 300).register("source", make_dataset(tmp_path))
        headers = {} if trusted else {"Authorization": "Bearer " + token}
        paths = [
            "/api/annotation-workbench/source/episodes",
            "/api/task-templates",
            "/api/subtask-review/source/episodes/0",
            "/api/instructions/source",
            "/api/manual-trims/source/jobs/trim",
        ]
        with TestClient(create_gateway(settings)) as client:
            for path in paths:
                response = client.get(path, headers=headers)
                assert response.status_code == 200, (path, response.text)
            opened = client.post(
                "/api/manual-trims/source/jobs/trim/preview", headers=headers
            )
            assert opened.status_code == 200, opened.text
            assert opened.json()["url"] == "http://viewer.example/bp/trim/episode_0"
            prefetch = client.get(
                "/api/annotation-workbench/source/episodes/0?prefetch=true",
                headers=headers,
            )
            assert prefetch.headers["etag"] == '"saved-head"'
            validated = client.get(
                "/api/annotation-workbench/source/episodes/0",
                headers={**headers, "If-None-Match": prefetch.headers["etag"]},
            )
            assert validated.status_code == 304 and validated.content == b""
        assert len(seen) == len(paths) + 4
        assert all(
            auth == (None if trusted else "Bearer " + token) for _, _, auth in seen
        )
        assert seen[0][1] == paths[0]
        assert seen[-3][:2] == ("POST", "/api/jobs/trim/viewer")
        assert seen[-2][1].endswith("?prefetch=true")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
