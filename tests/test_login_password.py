from fastapi.testclient import TestClient

from pipeline.client.app import create_app
from pipeline.shared.config import Settings


def test_browser_password_is_separate_from_api_token(settings_factory):
    settings = settings_factory(login_password="test-browser-password")
    with TestClient(create_app(settings, background=False)) as http:
        assert http.post("/api/login", json={"token": "wrong"}).status_code == 401
        assert (
            http.post("/api/login", json={"token": settings.token}).status_code == 401
        )
        assert (
            http.get(
                "/api/config", headers={"Authorization": "Bearer test-browser-password"}
            ).status_code
            == 401
        )
        assert (
            http.post("/api/login", json={"token": "test-browser-password"}).status_code
            == 200
        )
        assert http.get("/api/config").status_code == 200
        http.cookies.clear()
        assert (
            http.get(
                "/api/config", headers={"Authorization": "Bearer " + settings.token}
            ).status_code
            == 200
        )


def test_changing_password_invalidates_browser_cookie(settings_factory):
    settings = settings_factory(login_password="test-browser-password")
    with TestClient(create_app(settings, background=False)) as first:
        first.post("/api/login", json={"token": "test-browser-password"})
        cookie = first.cookies.get("pipeline_client")
    settings.login_password = "rotated-test-browser-password"
    with TestClient(create_app(settings, background=False)) as second:
        assert (
            second.get(
                "/api/config", headers={"Cookie": "pipeline_client=" + cookie}
            ).status_code
            == 401
        )
        assert (
            second.post(
                "/api/login", json={"token": "rotated-test-browser-password"}
            ).status_code
            == 200
        )


def test_password_environment_does_not_relax_service_tokens(monkeypatch):
    monkeypatch.setenv("PIPELINE_TOKEN", "api-" + "x" * 32)
    monkeypatch.setenv("PIPELINE_DEVICE_TOKEN", "device-" + "x" * 32)
    monkeypatch.setenv("PIPELINE_LOGIN_PASSWORD", "test-browser-password")
    settings = Settings.load("client")
    assert settings.login_password == "test-browser-password"
    assert settings.token != "test-browser-password"
