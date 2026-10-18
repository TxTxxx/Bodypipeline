import hashlib

import pytest
from fastapi.testclient import TestClient
from test_ingest import manifest

from pipeline.server.app import create_app


def test_device_isolation_and_ownership(settings_factory, monkeypatch):
    monkeypatch.setenv("ROBOT_ONE_SECRET", "first-device-" + "a" * 32)
    monkeypatch.setenv("ROBOT_TWO_SECRET", "second-device-" + "b" * 32)
    s = settings_factory(
        "server",
        devices=[
            {
                "robot_id": "r1",
                "project_id": "p1",
                "token_env": "ROBOT_ONE_SECRET",
                "targets": ["default"],
            },
            {
                "robot_id": "r2",
                "project_id": "p2",
                "token_env": "ROBOT_TWO_SECRET",
                "targets": ["default"],
            },
        ],
    )
    first = {"Authorization": "Bearer " + "first-device-" + "a" * 32}
    second = {"Authorization": "Bearer " + "second-device-" + "b" * 32}
    human = {"Authorization": "Bearer " + s.token}
    with TestClient(create_app(s, background=False)) as http:
        assert http.get("/device/targets", headers=human).status_code == 401
        assert http.get("/api/jobs", headers=first).status_code == 401
        m, data = manifest(s)
        m.update(robot_id="r1", project_id="p1")
        result = http.post(
            "/device/uploads", headers=first, json={"manifest": m, "target": "default"}
        )
        assert result.status_code == 201, result.text
        uid = result.json()["id"]
        assert http.get("/device/uploads/" + uid, headers=second).status_code == 403
        assert (
            http.post(
                "/device/uploads/" + uid + "/complete", headers=second
            ).status_code
            == 403
        )
        assert (
            http.post(
                "/device/uploads",
                headers=second,
                json={"manifest": m, "target": "default"},
            ).status_code
            == 403
        )
        assert (
            http.put(
                f"/device/uploads/{uid}/files/0?offset=0",
                content=data,
                headers={**first, "X-Chunk-SHA256": hashlib.sha256(data).hexdigest()},
            ).status_code
            == 200
        )
        assert (
            http.post("/device/uploads/" + uid + "/complete", headers=first).status_code
            == 200
        )


def test_malformed_requests_and_login_limits(settings_factory):
    s = settings_factory("server")
    with TestClient(create_app(s, background=False)) as http:
        assert http.post("/api/login", content=b"not-json").status_code == 400
        assert (
            http.post(
                "/api/login",
                json={"token": s.token},
                headers={"Origin": "https://evil.invalid"},
            ).status_code
            == 403
        )
        assert http.post("/api/login", content=b"x" * 5000).status_code == 413
        for _ in range(10):
            assert (
                http.post("/api/login", json={"token": "incorrect"}).status_code == 401
            )
        assert http.post("/api/login", json={"token": "incorrect"}).status_code == 429


def test_symlink_file_prefix_and_quota(settings_factory, tmp_path):
    from pipeline.server.ingest import IngestService
    from pipeline.shared.core import DomainError

    s = settings_factory("server", max_upload_bytes=4)
    i = IngestService(s)
    m, data = manifest(s)
    with pytest.raises(DomainError):
        i.create(m, "default", "")
    s.max_upload_bytes = 100000
    m["files"] = [
        {"path": "episode0/a", "size_bytes": 1, "sha256": "a" * 64},
        {"path": "episode0/a/b", "size_bytes": 1, "sha256": "b" * 64},
    ]
    with pytest.raises(DomainError):
        i.create(m, "default", "")
    m, data = manifest(s)
    r = i.create(m, "default", "")
    (i.root(r) / "data/episode0").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(DomainError):
        i.chunk(r["id"], 0, 0, hashlib.sha256(data).hexdigest(), data)
