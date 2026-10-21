"""Server must no longer expose robot replay controls."""

import json

from fastapi.testclient import TestClient

from pipeline.server.app import create_app
from pipeline.shared.config import Settings


def test_server_has_no_replay_routes(settings_factory):
    settings = settings_factory("server")
    with TestClient(create_app(settings, background=False)) as http:
        headers = {"Authorization": "Bearer " + settings.token}
        for method, path in [
            ("GET", "/api/replay/clients"),
            ("GET", "/api/replay/clients/agx-field/status"),
            ("POST", "/api/replay/clients/agx-field/start"),
            ("POST", "/api/replay/clients/agx-field/stop"),
            ("POST", "/api/replay/start"),
            ("GET", "/replay"),
        ]:
            assert http.request(method, path, headers=headers).status_code == 404
        page = http.get("/").text
        assert 'data-server-tab="replay"' not in page
        assert http.get("/api/config", headers=headers).status_code == 200


def test_retired_proxy_settings_do_not_break_server_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_TOKEN", "server-token-" + "a" * 32)
    monkeypatch.setenv("PIPELINE_DEVICE_TOKEN", "device-token-" + "b" * 32)
    path = tmp_path / "server.json"
    path.write_text(json.dumps({"replay_clients": [{"id": "old-client"}]}))
    settings = Settings.load("server", str(path))
    assert not hasattr(settings, "replay_clients")
