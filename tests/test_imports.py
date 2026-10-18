import json
import shutil

import pytest
from fastapi.testclient import TestClient
from test_processing import captured

from pipeline.server.app import create_app
from pipeline.server.datasets import dataset_for_job
from pipeline.shared.core import DomainError


def admin(settings):
    return {"Authorization": "Bearer " + settings.token}


def test_import_existing_raw_manifest_enters_conversion_pipeline(
    settings_factory, tmp_path
):
    raw, _ = captured(settings_factory)
    import_root = tmp_path / "history"
    import_root.mkdir()
    dataset = import_root / "old-capture"
    shutil.copytree(raw.parent, dataset)

    settings = settings_factory("server", import_roots=[import_root])
    app = create_app(settings, background=False)
    with TestClient(app) as http:
        response = http.post(
            "/api/imports",
            headers=admin(settings),
            json={"path": str(dataset)},
        )
        assert response.status_code == 201, response.text
        imported = response.json()
        assert imported["kind"] == "raw"
        assert imported["robot_type"] == settings.profile()["robot_type"]

        groups = http.get("/api/collections", headers=admin(settings)).json()
        assert len(groups) == 1
        assert groups[0]["uploads"][0]["source_kind"] == "raw_import"
        assert groups[0]["uploads"][0]["import_path"] == str(dataset.resolve())

        job = app.state.runner.enqueue(
            imported["upload_id"], target_format="v3.0", run_doctor=False
        )
        app.state.runner.tick()
        result = app.state.ingest.store.get("job", job["id"])
        assert result["state"] == "DEMO_READY", result


def test_import_existing_lerobot_is_preview_and_doctor_ready(
    settings_factory, tmp_path
):
    import_root = tmp_path / "datasets"
    dataset = import_root / "old-lerobot"
    (dataset / "meta").mkdir(parents=True)
    (dataset / "meta/info.json").write_text(
        json.dumps(
            {
                "codebase_version": "v3.0",
                "robot_type": "agilex_aloha",
                "total_episodes": 12,
                "total_frames": 3456,
                "fps": 30,
            }
        )
    )
    settings = settings_factory(
        "server", import_roots=[dataset], letools_bin="/usr/bin/true"
    )
    app = create_app(settings, background=False)
    with TestClient(app) as http:
        response = http.post(
            "/api/imports",
            headers=admin(settings),
            json={"path": str(dataset)},
        )
        assert response.status_code == 201, response.text
        imported = response.json()
        assert imported["kind"] == "lerobot"
        job = http.get(
            f"/api/jobs/{imported['job_id']}", headers=admin(settings)
        ).json()
        assert job["dataset_available"] is True
        assert job["dataset_path"] == str(dataset.resolve())
        assert job["source_kind"] == "lerobot_import"
        assert (
            dataset_for_job(settings, app.state.ingest.store.get("job", job["id"]))
            == dataset.resolve()
        )
        assert (
            http.post(
                f"/api/jobs/{job['id']}/checks", headers=admin(settings)
            ).status_code
            == 202
        )


def test_import_rejects_paths_outside_configured_roots(settings_factory, tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    (outside / "meta").mkdir()
    (outside / "meta/info.json").write_text("{}")
    settings = settings_factory("server", import_roots=[allowed])
    app = create_app(settings, background=False)
    with TestClient(app) as http:
        response = http.post(
            "/api/imports",
            headers=admin(settings),
            json={"path": str(outside)},
        )
        assert response.status_code == 403
        with pytest.raises(DomainError):
            app.state.imports.resolve(str(outside))
