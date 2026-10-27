from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pipeline.client.app import create_app
from pipeline.client.controller import ControllerCaptureCoordinator
from pipeline.client.service import CaptureService
from pipeline.shared.core import DomainError


def body(key="controller-capture-001"):
    return dict(
        instruction="",
        dataset_name="controller dataset",
        idempotency_key=key,
        local_root="default",
        local_prefix="",
        remote_target="default",
        remote_prefix="",
    )


class FakeControllerAdapter:
    def __init__(self):
        self.current = {
            "phase": "saved",
            "session_id": None,
            "request_id": None,
            "data_state": "closed",
            "video_state": "closed",
            "controller_owned": False,
        }
        self.starts = 0
        self.stops = 0
        self.imports = []

    def health(self):
        return {"ready": True}

    def start(self, *args):
        self.starts += 1

    def stop(self, *args):
        self.stops += 1
        return {"stopped": True}

    def controller_status(self):
        return dict(self.current)

    def import_controller_recording(self, status, raw_root, session_id):
        self.imports.append((status, raw_root, session_id))
        (raw_root / "episode0").mkdir(parents=True)
        return {
            "imported": True,
            "source_path": status["path"],
            "apex_session_id": status["session_id"],
            "writer_closed": True,
        }


def controller(settings_factory, **settings):
    settings.setdefault(
        "controller_trigger",
        {"enabled": True, "accept_missing_request_id": True},
    )
    adapter = FakeControllerAdapter()
    service = CaptureService(settings_factory(**settings), adapter=adapter)
    return service, adapter, ControllerCaptureCoordinator(service)


def recording_status(**changes):
    return {
        "phase": "recording",
        "session_id": "apex-session-1",
        "request_id": "controller-event-1",
        "data_state": "recording",
        "video_state": "recording",
        "started_at": 100.0,
        "controller_owned": True,
        **changes,
    }


def test_arm_adopt_stop_and_auto_upload(settings_factory):
    service, adapter, bridge = controller(settings_factory)
    armed = bridge.arm(body())
    assert armed["state"] == "DRAFT"
    assert armed["controller_armed"] is True
    assert armed["capture_trigger"] == "controller"

    adapter.current = recording_status()
    assert bridge.tick() is True
    active = service.store.get("capture", armed["id"])
    assert active["state"] == "RECORDING"
    assert active["controller_session_id"] == "apex-session-1"
    assert active["controller_armed"] is False
    assert (adapter.starts, adapter.stops) == (0, 0)

    adapter.current = recording_status(
        phase="saving", data_state="saving", video_state="saving"
    )
    assert bridge.tick() is True
    assert service.store.get("capture", armed["id"])["state"] == "STOPPING"

    source = (
        Path(service.store.get("capture", armed["id"])["raw_root"]).parent / "source"
    )
    adapter.current = recording_status(
        phase="saved",
        request_id=None,
        controller_owned=False,
        data_state="closed",
        video_state="closed",
        ended_at=110.0,
        path=str(source),
    )
    assert bridge.tick() is True
    reviewed = service.store.get("capture", armed["id"])
    assert reviewed["state"] == "SEALING"
    assert reviewed["upload_state"] == "NONE"
    assert reviewed["stopped_at"] == 110.0
    assert reviewed["seal_requested_at"] > 0
    assert len(adapter.imports) == 1
    assert (adapter.starts, adapter.stops) == (0, 0)


def test_non_controller_session_is_not_adopted(settings_factory):
    service, adapter, bridge = controller(settings_factory)
    armed = bridge.arm(body())
    adapter.current = recording_status(request_id=armed["id"], controller_owned=False)
    assert bridge.tick() is False
    assert service.store.get("capture", armed["id"])["state"] == "DRAFT"


def test_controller_cancel_discards_without_upload(settings_factory):
    service, adapter, bridge = controller(settings_factory)
    armed = bridge.arm(body())
    adapter.current = recording_status()
    bridge.tick()
    adapter.current = recording_status(
        phase="cancelled",
        data_state="cancelled",
        video_state="closed",
        ended_at=105.0,
        error_message="controller cancelled",
    )
    assert bridge.tick() is True
    row = service.store.get("capture", armed["id"])
    assert row["state"] == "DISCARDED"
    assert row["controller_cancelled"] is True
    assert row.get("capture_failed") is not True
    assert row["upload_state"] == "NONE"
    with pytest.raises(DomainError):
        service.request_keep(row["id"])


def test_disarm_keeps_draft_but_does_not_adopt(settings_factory):
    service, adapter, bridge = controller(settings_factory)
    armed = bridge.arm(body())
    disarmed = bridge.disarm(armed["id"])
    assert disarmed["state"] == "DRAFT"
    assert disarmed["controller_armed"] is False
    adapter.current = recording_status()
    assert bridge.tick() is False
    assert service.store.get("capture", armed["id"])["state"] == "DRAFT"


def test_arm_api_and_config(settings_factory):
    trigger = {"enabled": True, "accept_missing_request_id": True}
    settings = settings_factory(controller_trigger=trigger)
    with TestClient(create_app(settings, background=False)) as http:
        headers = {"Authorization": "Bearer " + settings.token}
        config = http.get("/api/config", headers=headers).json()
        assert config["controller_trigger"] == trigger
        response = http.post("/api/controller/arm", json=body(), headers=headers)
        assert response.status_code == 201
        armed = response.json()
        assert armed["state"] == "DRAFT" and armed["controller_armed"] is True
        disarmed = http.post(f"/api/controller/disarm/{armed['id']}", headers=headers)
        assert disarmed.status_code == 200
        assert disarmed.json()["controller_armed"] is False
