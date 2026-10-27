from pathlib import Path
from types import SimpleNamespace

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

    def close(self):
        pass

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


def selection_body(**changes):
    data = body()
    data.pop("idempotency_key")
    return {**data, **changes}


def test_selected_dataset_creates_new_sessions_after_save_and_discard(settings_factory):
    service, adapter, bridge = controller(settings_factory)
    selected = bridge.select(selection_body())
    assert selected["data"]["dataset_name"] == "controller dataset"
    assert service.store.all("capture") == []

    for index, phase in enumerate(["saved", "cancelled", "saved"]):
        adapter.current = recording_status(session_id=f"controller-session-{index}")
        assert bridge.tick() is True
        rows = service.store.all("capture")
        assert len(rows) == index + 1
        current = next(r for r in rows if r["state"] == "RECORDING")
        assert current["dataset_name"] == "controller dataset"
        assert bridge.tick() is False
        assert len(service.store.all("capture")) == index + 1
        adapter.current.update(
            phase=phase,
            data_state="closed",
            video_state="closed",
            path=str(Path(current["raw_root"]).parent / "source"),
        )
        assert bridge.tick() is True
        finished = service.store.get("capture", current["id"])
        assert finished["state"] == ("SEALING" if phase == "saved" else "DISCARDED")
        assert bridge.selection() == selected
        assert bridge.tick() is False
    assert len(adapter.imports) == 2
    assert (adapter.starts, adapter.stops) == (0, 0)


def test_selection_survives_restart_and_does_not_readopt_a_known_session(
    settings_factory,
):
    service, adapter, bridge = controller(settings_factory)
    adapter.current = recording_status(phase="saved", session_id="previous-recording")
    bridge.select(selection_body())
    service = CaptureService(service.settings, adapter=adapter)
    bridge = ControllerCaptureCoordinator(service)
    adapter.current.update(phase="recording")
    assert bridge.tick() is False
    adapter.current = recording_status(session_id="new-recording")
    assert bridge.tick() is True
    row = service.store.all("capture")[0]
    adapter.current.update(phase="cancelled")
    assert bridge.tick() is True
    # Even if the adapter repeats a stale recording status, never recreate it.
    adapter.current.update(phase="recording")
    assert bridge.tick() is False
    assert len(service.store.all("capture")) == 1
    assert service.store.get("capture", row["id"])["state"] == "DISCARDED"


def test_switch_and_clear_selection(settings_factory):
    service, adapter, bridge = controller(settings_factory)
    legacy = bridge.arm(body())
    bridge.select(selection_body(dataset_name="second dataset"))
    assert service.store.get("capture", legacy["id"])["controller_armed"] is False
    adapter.current = recording_status()
    assert bridge.tick() is True
    current = next(r for r in service.store.all("capture") if r["state"] == "RECORDING")
    assert current["dataset_name"] == "second dataset"
    with pytest.raises(DomainError, match="切换"):
        bridge.select(selection_body(dataset_name="third dataset"))
    # Clearing affects the next start; the current session still gets finalized.
    bridge.clear_selection()
    adapter.current.update(phase="cancelled")
    assert bridge.tick() is True
    adapter.current = recording_status(session_id="next-session")
    assert bridge.tick() is False


def test_selection_respects_manual_capture_replay_and_session_ownership(
    settings_factory,
):
    service, adapter, bridge = controller(settings_factory)
    bridge.select(selection_body())
    adapter.current = recording_status(controller_owned=False)
    assert bridge.tick() is False
    adapter.current["controller_owned"] = True
    service.replay = SimpleNamespace(row={"state": "RUNNING"})
    assert bridge.tick() is False
    service.replay.row["state"] = "STOPPED"
    manual = service.create(body())
    manual["state"] = "RECOVERY_REQUIRED"
    service.save(manual)
    assert bridge.tick() is False
    assert len(service.store.all("capture")) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"local_root": "unknown"},
        {"local_prefix": "../escape"},
        {"remote_prefix": "../escape"},
    ],
)
def test_invalid_selection_does_not_create_or_replace_context(
    settings_factory, changes
):
    service, _, bridge = controller(settings_factory)
    with pytest.raises(DomainError):
        bridge.select(selection_body(**changes))
    assert bridge.selection() is None
    assert service.store.all("capture") == []


def test_selection_api_persists_without_creating_a_draft(settings_factory):
    settings = settings_factory(controller_trigger={"enabled": True})
    app = create_app(settings, background=False)
    app.state.service.adapter = FakeControllerAdapter()
    with TestClient(app) as http:
        headers = {"Authorization": "Bearer " + settings.token}
        assert (
            http.put("/api/controller/selection", json=selection_body()).status_code
            == 401
        )
        response = http.put(
            "/api/controller/selection", json=selection_body(), headers=headers
        )
        assert response.status_code == 200
        assert response.json()["data"]["dataset_name"] == "controller dataset"
        assert http.get("/api/captures", headers=headers).json() == []
        assert (
            http.get("/api/controller/selection", headers=headers).json()
            == response.json()
        )
        assert (
            http.put(
                "/api/controller/selection", json=selection_body(), headers=headers
            ).json()
            == response.json()
        )
        assert (
            http.delete("/api/controller/selection", headers=headers).json()["data"]
            is None
        )


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
