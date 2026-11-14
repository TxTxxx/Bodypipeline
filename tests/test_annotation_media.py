"""Real CPU decoder/HTTP regressions; no model and no user data writes."""

from __future__ import annotations

import asyncio
import hashlib
import io
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from pipeline.server.annotation_media import AnnotationMedia
from pipeline.server.app import create_app
from pipeline.server.viewer_data import PreviewRegistry
from pipeline.shared.core import DomainError


@pytest.fixture(scope="module")
def video_source(tmp_path_factory):
    repo = Path(__file__).resolve().parents[1]
    python = repo / ".runtime/letools-venv/bin/python"
    if not python.exists():
        pytest.skip("isolated LeTools media environment not installed")
    root = tmp_path_factory.mktemp("video-source") / "fixture"
    subprocess.run(
        [
            str(python),
            str(repo / "tests/fixtures/workbench_video_scenario.py"),
            str(root),
        ],
        capture_output=True,
        check=True,
    )
    return root / "dataset"


@pytest.fixture
def video_app(settings_factory, letools_binary, video_source):
    settings = settings_factory(
        "server", letools_bin=letools_binary, import_roots=[video_source.parent]
    )
    app = create_app(settings, background=False)
    store = app.state.runner.store
    store.put(
        "job",
        "video",
        {
            "id": "video",
            "state": "CONVERTED",
            "dataset_path": str(video_source),
            "dataset_name": "video",
            "robot_type": "synthetic_video",
        },
    )
    PreviewRegistry(store, settings.viewer_ttl).register("video", video_source)
    return app


BASE = "/api/annotation-workbench/video"


def client_for(app):
    return TestClient(
        app, headers={"Authorization": "Bearer " + app.state.runner.settings.token}
    )


def frame_url(media, episode, frame, camera=0):
    return (
        f"{BASE}/episodes/{episode}/frame/{frame}?camera={media['cameras'][camera]['key']}"
        f"&signature={media['source_signature']}"
    )


def code(image):
    return sum(
        1 << bit for bit in range(12) if image.getpixel((26 + bit * 28, 232))[0] > 128
    )


def test_shared_three_camera_first_last_pixels_offsets_and_reused_process(
    video_app, video_source
):
    files = sorted(p for p in video_source.rglob("*") if p.is_file())
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    with client_for(video_app) as client:
        decoder = video_app.state.annotation_media
        for episode in (0, 1, 2):
            response = client.get(f"{BASE}/episodes/{episode}/media")
            assert response.status_code == 200, response.text
            media = response.json()
            assert media["timestamps"][0] == 0
            assert len(media["cameras"]) == 3
            pid = decoder.process.pid
            for n in (0, 119, 48, 49, 48):
                for camera in range(3):
                    result = client.get(frame_url(media, episode, n, camera))
                    assert result.status_code == 200, result.text
                    assert int(result.headers["x-data-frame"]) == n
                    assert (
                        abs(
                            float(result.headers["x-media-pts"])
                            - media["cameras"][camera]["start"]
                            - n / 30
                        )
                        < 1e-6
                    )
                    assert code(
                        Image.open(io.BytesIO(result.content)).convert("RGB")
                    ) == n | (episode << 8)
                    assert decoder.process.pid == pid
        worker = decoder.process
    assert worker.poll() == 0
    assert before == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def test_duplicate_reads_deduplicate_and_bad_requests_dont_poison_decoder(
    video_app, monkeypatch
):
    decoder = video_app.state.annotation_media
    real = decoder._exchange
    sent = []

    def exchange(payload):
        sent.append(payload)
        return real(payload)

    monkeypatch.setattr(decoder, "_exchange", exchange)
    with client_for(video_app) as client:
        media = client.get(f"{BASE}/episodes/1/media").json()
        with ThreadPoolExecutor(8) as pool:
            results = list(
                pool.map(lambda _: client.get(frame_url(media, 1, 44)), range(8))
            )
        assert all(r.status_code == 200 for r in results)
        assert sum(p["mode"] == "frame" for p in sent) == 1
        assert client.get(frame_url(media, 1, 120)).status_code == 422
        assert client.get(frame_url(media, 1, -1)).status_code == 422
        assert (
            client.get(
                frame_url(media, 1, 45).replace("camera_0&", "missing&")
            ).status_code
            == 422
        )
        assert (
            client.get(frame_url(media, 1, 45) + "-invalid-camera").status_code == 409
        )
        assert client.get(frame_url(media, 1, 45)).status_code == 200
        pid = decoder.process.pid
        decoder.process.kill()
        decoder.process.wait()
        assert client.get(f"{BASE}/episodes/2/media").status_code == 200
        assert decoder.process.pid != pid


def test_video_ranges_check_source_signature(video_app):
    with client_for(video_app) as client:
        media = client.get(f"{BASE}/episodes/0/media").json()
        path = media["cameras"][0]["path"]
        url = f"{BASE}/media/{path}"
        assert client.get(url + "?signature=" + "0" * 64).status_code == 409
        response = client.get(
            url + "?signature=" + media["source_signature"],
            headers={"Range": "bytes=0-100"},
        )
        assert response.status_code == 206
        assert len(response.content) == 101


@pytest.mark.asyncio
async def test_disconnected_queued_request_is_not_dispatched_and_canceled_active_is_drained(
    settings_factory, monkeypatch
):
    service = AnnotationMedia(settings_factory("server"))
    started, release = threading.Event(), threading.Event()
    calls = []

    def read(payload, _cache):
        calls.append(payload)
        started.set()
        assert release.wait(3)
        return payload

    monkeypatch.setattr(service, "_read", read)

    class Request:
        disconnected = False

        async def is_disconnected(self):
            return self.disconnected

    first = asyncio.create_task(service.read(Request(), "first"))
    await asyncio.to_thread(started.wait, 2)
    request = Request()
    queued = asyncio.create_task(service.read(request, "obsolete"))
    await asyncio.sleep(0.02)
    request.disconnected = True
    with pytest.raises(DomainError) as result:
        await queued
    assert result.value.code == 499
    first.cancel()
    last = asyncio.create_task(service.read(Request(), "latest"))
    await asyncio.sleep(0.08)
    assert calls == ["first"]  # cancel did not free a still-running protocol
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert await last == "latest"
    assert calls == ["first", "latest"]
