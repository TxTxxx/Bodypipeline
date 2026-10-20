"""Import and convert upstream ALOHA-style HDF5 episode directories."""

import io
import json
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from pipeline.server.app import create_app
from pipeline.server.jobs import JobRunner
from pipeline.server.providers.aloha_hdf5 import AlohaHDF5Provider
from pipeline.shared.hdf5 import AGILEX_ALIGNED_SCHEMA, ALOHA_HDF5_FORMAT


def profile():
    data = json.loads(Path("configs/arx-x5-hdf5-v1.json").read_text())
    return data


def jpeg_bytes(size=(64, 48), color=(10, 120, 200)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


def write_episode(
    path,
    *,
    frames=4,
    dimensions=14,
    images=("head", "left_wrist", "right_wrist"),
):
    import h5py

    payload = jpeg_bytes()
    with h5py.File(path, "w") as handle:
        rng = np.random.default_rng(frames)
        handle["action"] = rng.normal(size=(frames, dimensions)).astype("float32")
        handle["observations/qpos"] = rng.normal(size=(frames, dimensions)).astype(
            "float32"
        )
        for camera in images:
            handle[f"observations/images/{camera}"] = np.tile(
                np.frombuffer(payload, dtype=np.uint8), (frames, 1)
            )
    return path


def make_dataset(root):
    root.mkdir(parents=True, exist_ok=True)
    write_episode(root / "episode_0.hdf5")
    write_episode(root / "episode_1.hdf5")
    return root


def hdf5_settings(settings_factory, tmp_path):
    return settings_factory(
        "server",
        profiles={
            "arx": {
                "profile": profile(),
                "provider": "aloha_hdf5",
                "raw_formats": [ALOHA_HDF5_FORMAT],
            }
        },
        default_profile="arx",
        import_roots=[tmp_path.resolve()],
    )


def test_import_registers_one_upload_per_episode_without_touching_source(
    settings_factory, tmp_path
):
    settings = hdf5_settings(settings_factory, tmp_path)
    source = make_dataset(tmp_path / "unscrew")
    before = {item.name for item in source.iterdir()}
    app = create_app(settings, background=False)
    admin = {"Authorization": "Bearer " + settings.token}
    with TestClient(app) as http:
        response = http.post(
            "/api/imports",
            headers=admin,
            json={"path": str(source), "robot_type": "ARX_X5"},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["kind"] == "hdf5"
        assert body["episode_count"] == 2
        assert body["provider"] == "aloha_hdf5"
        assert body["runtime_id"] == "arx"

        rows = http.get("/api/uploads", headers=admin).json()
        imported = [r for r in rows if r["collection_id"] == body["collection_id"]]
        assert len(imported) == 2
        assert {r["raw_format"] if "raw_format" in r else None for r in imported} == {
            None
        }
        assert all(r["source_kind"] == "raw_import" for r in imported)
        assert all(r["conversion_blockers"] == [] for r in imported)

        # Re-importing the same directory is idempotent.
        again = http.post(
            "/api/imports",
            headers=admin,
            json={"path": str(source), "robot_type": "ARX_X5"},
        )
        assert again.status_code == 201
        assert again.json()["collection_id"] == body["collection_id"]

    assert {item.name for item in source.iterdir()} == before


def test_import_requires_a_runtime_when_the_directory_declares_none(
    settings_factory, tmp_path
):
    settings = hdf5_settings(settings_factory, tmp_path)
    source = make_dataset(tmp_path / "unscrew")
    app = create_app(settings, background=False)
    with TestClient(app) as http:
        response = http.post(
            "/api/imports",
            headers={"Authorization": "Bearer " + settings.token},
            json={"path": str(source)},
        )
        assert response.status_code == 400
        assert "HDF5" in response.json()["detail"]


def test_import_rejects_an_episode_that_does_not_match_the_profile(
    settings_factory, tmp_path
):
    settings = hdf5_settings(settings_factory, tmp_path)
    source = tmp_path / "broken"
    source.mkdir()
    write_episode(source / "episode_0.hdf5", dimensions=8)
    app = create_app(settings, background=False)
    with TestClient(app) as http:
        response = http.post(
            "/api/imports",
            headers={"Authorization": "Bearer " + settings.token},
            json={"path": str(source), "robot_type": "ARX_X5"},
        )
        assert response.status_code == 422
        assert "episode_0.hdf5" in response.json()["detail"]


def test_prepare_translates_a_uniform_episode_into_the_aligned_schema(tmp_path):
    import h5py

    source = write_episode(tmp_path / "episode_7.hdf5")
    provider = AlohaHDF5Provider()
    report = provider.prepare(source, tmp_path / "work/aligned", profile())

    assert report["aligned_kind"] == "hdf5"
    assert report["frames"] == 4
    aligned = Path(report["aligned_path"])
    assert aligned.is_file()
    with h5py.File(aligned, "r") as handle:
        assert handle.attrs["schema"] == AGILEX_ALIGNED_SCHEMA
        assert handle.attrs["source_format"] == ALOHA_HDF5_FORMAT
        assert handle["observation.state"].shape == (4, 14)
        assert handle["action"].shape == (4, 14)
        image = handle["observation.images.head"]
        assert image.attrs["width"] == 64 and image.attrs["height"] == 48
        assert image.attrs["encoded_format"] == "jpeg"
        assert bytes(image[0]) == jpeg_bytes()
        assert "observation.images.right_wrist" in handle


def test_job_hashes_the_episode_file_and_reaches_demo_ready(settings_factory, tmp_path):
    settings = hdf5_settings(settings_factory, tmp_path)
    source = tmp_path / "single"
    source.mkdir()
    write_episode(source / "episode_0.hdf5")
    app = create_app(settings, background=False)
    admin = {"Authorization": "Bearer " + settings.token}
    with TestClient(app) as http:
        imported = http.post(
            "/api/imports",
            headers=admin,
            json={"path": str(source), "robot_type": "ARX_X5"},
        ).json()
        job = http.post(
            f"/api/collections/{imported['collection_id']}/process",
            headers=admin,
            json={"instruction": "unscrew the screw", "run_doctor": False},
        )
        assert job.status_code == 202, job.text

    runner = JobRunner(settings, app.state.ingest)
    runner.tick()
    row = app.state.ingest.store.get("job", job.json()["id"])
    assert row["state"] == "DEMO_READY", row
    aligned = Path(row["work_path"]) / "aligned/episode0.hdf5"
    assert aligned.is_file()
