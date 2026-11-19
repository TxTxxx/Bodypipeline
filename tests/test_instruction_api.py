"""End-to-end instruction apply job and Viewer gateway proxy."""

import json
import subprocess
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from pipeline.server.app import create_app
from pipeline.server.viewer import create_gateway
from pipeline.server.viewer_data import PreviewRegistry

SCENARIO = Path(__file__).parent / "fixtures/merge_scenario.py"


def admin(app):
    return {"Authorization": "Bearer " + app.state.runner.settings.token}


def instructions_template():
    return {
        "parts": [
            {"type": "text", "text": "请使用"},
            {"type": "variable", "key": "object"},
            {"type": "text", "text": "放入"},
            {"type": "variable", "key": "target"},
        ],
        "variables": {
            "object": {
                "label": "物体",
                "options": ["蓝色圆柱"],
                "default": "蓝色圆柱",
            },
            "target": {
                "label": "目标位置",
                "options": ["右侧盒子"],
                "default": "右侧盒子",
            },
        },
    }


def test_apply_job_rewrites_real_dataset_in_place_and_updates_viewer(
    settings_factory, tmp_path, letools_binary
):
    python = str(Path(letools_binary).with_name("python"))
    fixtures = tmp_path / "fixtures"
    subprocess.run(
        [python, str(SCENARIO), "create", str(fixtures), "v3.0"], check=True
    )
    dataset = fixtures / "input-0"
    dataset_path = str(dataset.resolve())
    settings = settings_factory(
        "server", letools_bin=letools_binary, import_roots=[fixtures]
    )
    app = create_app(settings, background=False)
    runner = app.state.runner
    runner.store.put(
        "job",
        "source",
        {
            "id": "source",
            "state": "CONVERTED",
            "dataset_path": str(dataset),
            "dataset_name": "fixture",
            "created_at": 1,
            "robot_type": "test_arm",
        },
    )
    PreviewRegistry(runner.store, settings.viewer_ttl).register("source", dataset)
    client = TestClient(app, headers=admin(app))

    head = client.get("/api/instructions/source").json()
    saved = client.put(
        "/api/instructions/source/template",
        json={
            "revision": head["revision"],
            "source_signature": head["source_signature"],
            "template": instructions_template(),
        },
    )
    assert saved.status_code == 200, saved.text
    head = saved.json()
    saved = client.put(
        "/api/instructions/source/episodes/0",
        json={
            "revision": head["revision"],
            "source_signature": head["source_signature"],
            "selections": {
                "object": "蓝色圆柱",
                "target": "右侧盒子",
            },
        },
    )
    assert saved.status_code == 200, saved.text
    head = saved.json()
    request = {
        "revision": head["revision"],
        "source_signature": head["source_signature"],
    }
    first = client.post("/api/instructions/source/apply", json=request)
    duplicate = client.post("/api/instructions/source/apply", json=request)
    assert first.status_code == 202, first.text
    assert duplicate.status_code == 202, duplicate.text
    assert duplicate.json()["id"] == first.json()["id"]

    app.state.instruction_apply.tick()
    status = client.get(
        f"/api/instructions/source/apply/{first.json()['id']}"
    ).json()
    assert status["state"] == "CONVERTED", status
    assert status["report"]["updated_episodes"] == [0]
    assert str(dataset.resolve()) == dataset_path

    refreshed = client.get("/api/instructions/source").json()
    assert refreshed["source_signature"] != head["source_signature"]
    episode = client.get("/api/instructions/source/episodes/0").json()
    assert episode["current_instruction"] == "请使用蓝色圆柱放入右侧盒子"
    assert episode["has_draft"] is True
    info = json.loads((dataset / "meta/info.json").read_text())
    assert info["bodypipeline_revision"]

    validation = subprocess.run(
        [letools_binary, "validate", "--deep", str(dataset)],
        capture_output=True,
        text=True,
    )
    assert validation.returncode == 0, validation.stdout + validation.stderr


def test_gateway_proxies_only_registered_instruction_identities(settings_factory, monkeypatch):
    settings = settings_factory("viewer")
    # The gateway validates against the same persisted preview registry.
    from pipeline.server.viewer_data import PreviewRegistry
    from pipeline.shared.core import Store

    store = Store(settings.root / "state")
    store.put("viewer", "service", {"state": "READY"})
    PreviewRegistry(store, settings.viewer_ttl).register(
        "source", settings.root / "dataset"
    )
    (settings.root / "dataset/meta").mkdir(parents=True)
    (settings.root / "dataset/meta/info.json").write_text("{}")
    gateway = create_gateway(settings)
    real_client = httpx.AsyncClient
    calls = []

    def make_client(**kwargs):
        async def response(request):
            calls.append(request)
            assert request.headers["authorization"] == "Bearer " + settings.token
            return httpx.Response(200, json={"revision": 4})

        return real_client(transport=httpx.MockTransport(response))

    monkeypatch.setattr("pipeline.server.viewer.httpx.AsyncClient", make_client)
    with TestClient(gateway) as client:
        headers = {"Authorization": "Bearer " + settings.token}
        assert client.get("/api/instructions/source", headers=headers).json() == {
            "revision": 4
        }
        assert (
            client.get("/api/instructions/missing", headers=headers).status_code
            == 404
        )
        assert len(calls) == 1
        assert str(calls[0].url).endswith("/api/instructions/source")
