import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from pipeline.server.app import create_app
from pipeline.server.viewer import ViewerManager, create_gateway
from pipeline.server.viewer_bootstrap import UPSTREAM, patch_cache
from pipeline.shared.core import DomainError, Store


def test_gateway_is_public_range_safe_and_expires(settings_factory, monkeypatch):
    s = settings_factory("viewer")
    store = Store(s.root / "state")
    state = {
        "state": "READY",
        "page_path": "/local/test/0",
        "expires_at": time.time() + 300,
    }
    store.put("viewer", "active", state)
    seen = []

    async def upstream(request):
        seen.append(request)
        return httpx.Response(
            206,
            stream=httpx.ByteStream(b"1234"),
            headers={
                "Content-Type": "video/mp4",
                "Content-Range": "bytes 1-4/20",
                "Accept-Ranges": "bytes",
            },
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "pipeline.server.viewer.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(upstream), **kwargs),
    )
    with TestClient(create_gateway(s)) as browser:
        root = browser.get("/", follow_redirects=False)
        assert root.status_code == 307
        assert root.headers["location"] == "/local/test/0"
        r = browser.get("/_data/local/a.mp4", headers={"Range": "bytes=1-4"})
        assert r.status_code == 206 and r.content == b"1234"
        assert r.headers["content-range"] == "bytes 1-4/20"
        assert seen[-1].headers["range"] == "bytes=1-4"
        assert (
            "cookie" not in seen[-1].headers and "authorization" not in seen[-1].headers
        )
        assert str(seen[-1].url) == "http://127.0.0.1:8765/local/a.mp4"
        assert (
            browser.post(
                "/_data/local/a.mp4", headers={"Origin": "http://testserver"}
            ).status_code
            == 405
        )
        state["state"] = "STARTING"
        state["expires_at"] = time.time() + 300
        store.put("viewer", "active", state)
        waiting = browser.get("/")
        assert waiting.status_code == 200
        assert 'http-equiv="refresh"' in waiting.text
        assert "正在启动 LeRobot Visualizer" in waiting.text
        state["expires_at"] = time.time() - 1
        store.put("viewer", "active", state)
        assert browser.get("/_data/local/a.mp4").status_code == 503
        store.delete("viewer", "active")
        missing = browser.get("/")
        assert missing.status_code == 503
        assert "预览尚未就绪" in missing.json()["detail"]


def test_viewer_patch_is_version_checked_and_idempotent(tmp_path):
    (tmp_path / "source").mkdir()
    (tmp_path / ".letools-source.json").write_text(
        json.dumps({"upstream_commit": UPSTREAM})
    )
    config = tmp_path / "source/next.config.ts"
    config.write_text("const nextConfig: NextConfig = {\n};\n")
    patch_cache(tmp_path)
    once = config.read_text()
    patch_cache(tmp_path)
    assert config.read_text() == once
    assert "env: { DATASET_URL:" in once
    (tmp_path / ".letools-source.json").write_text(
        json.dumps({"upstream_commit": "unexpected"})
    )
    with pytest.raises(RuntimeError):
        patch_cache(tmp_path)


def test_corrupt_viewer_state_is_not_reported_as_stopped(settings_factory):
    settings = settings_factory("server")
    app = create_app(settings, background=False)
    with TestClient(app) as http:
        path = settings.root / "state/viewer/active.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text("broken JSON")
        response = http.get(
            "/api/viewer", headers={"Authorization": "Bearer " + settings.token}
        )
        assert response.status_code == 500
        assert "状态文件损坏" in response.json()["detail"]
        with pytest.raises(DomainError) as invalid:
            ViewerManager(settings, app.state.ingest.store).recover()
        assert invalid.value.code == 500
