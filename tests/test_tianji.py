"""PR Client capture ownership, immutable packaging and trusted upload routing."""

import copy
import json
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from pipeline.client.adapters.apex import ApexAdapter
from pipeline.client.adapters.registry import create_adapter
from pipeline.client.controller import ControllerCaptureCoordinator
from pipeline.client.packagers import TianjiMCAPPackager
from pipeline.client.service import CaptureService
from pipeline.client.upload import UploadAgent
from pipeline.server.app import create_app
from pipeline.server.ingest import IngestService
from pipeline.shared.core import DomainError, digest, file_hash
from pipeline.shared.tianji import (
    TIANJI_MCAP_FORMAT,
    recording_files,
)


def profile():
    return json.loads(Path("configs/tianji-marvin-pro-v1.json").read_text())


def recording(path):
    for part in ("data", "video"):
        (path / part).mkdir(parents=True)
        (path / part / "metadata.yaml").write_text("fixture metadata")
        (path / part / "bag_0.mcap").write_bytes(
            b"fixture raw mcap: not real robot data"
        )
    return path


def setup_apex(settings_factory, tmp_path, *, timeout=False, drain=True):
    storage = tmp_path / "apex"
    storage.mkdir()
    original = recording(storage / "session")
    settings = settings_factory(
        mode="ros2",
        capture_adapter="apex",
        capture_packager="tianji_mcap",
        profiles={
            "tianji": dict(
                profile=profile(), provider="tianji", raw_formats=[TIANJI_MCAP_FORMAT]
            )
        },
        default_profile="tianji",
        capture_options=dict(
            allow_recording=True,
            project_id="dedicated",
            storage_root=str(storage),
            timeout_s=0.01,
        ),
    )
    state = dict(phase="idle", project_id="dedicated")
    calls = []

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        calls.append((request.method, route))
        if request.method == "GET" and route == "projects":
            data = dict(
                projects=[
                    dict(id="dedicated", available=True, storage_dir=str(storage))
                ]
            )
        elif request.method == "GET" and route == "status":
            data = state.copy()
        elif request.method == "POST" and route == "start":
            body = json.loads(request.content)
            state.update(
                phase="recording",
                session_id="apex-owned",
                request_id=body["request_id"],
                data_state="recording",
                video_state="recording",
                path=str(original),
            )
            if timeout:
                raise httpx.ReadTimeout("Acknowledgement lost")
            data = state.copy()
        elif request.method == "POST" and route == "stop":
            assert json.loads(request.content) == dict(session_id="apex-owned")
            state.update(
                phase="saved",
                data_state="closed",
                video_state="closed" if drain else "saving",
            )
            data = state.copy()
        else:
            pytest.fail("Unexpected API call: " + str(request.url))
        return httpx.Response(200, json=dict(ok=True, data=data))

    adapter = ApexAdapter(
        settings,
        http=httpx.Client(
            base_url="http://127.0.0.1:8080", transport=httpx.MockTransport(handler)
        ),
    )
    return adapter, settings, state, calls, original


def test_owned_start_stop_and_immutable_package(settings_factory, tmp_path):
    adapter, s, state, calls, original = setup_apex(settings_factory, tmp_path)
    raw = tmp_path / "managed"
    before = {
        p.relative_to(original): p.read_bytes() for p in recording_files(original)
    }
    adapter.start("pipeline-session", raw, "task")
    adapter.start("pipeline-session", raw, "task")
    assert calls.count(("POST", "start")) == 1
    adapter.stop("pipeline-session")
    adapter.stop("pipeline-session")
    assert calls.count(("POST", "stop")) == 1
    sealed = TianjiMCAPPackager().seal(raw, s.profile(), {})
    assert len(sealed["files"]) == 5
    for name, content in before.items():
        assert (original / name).read_bytes() == content
        assert (raw / "episode0" / name).read_bytes() == content
    adapter.close()
    assert {route for method, route in calls if method == "POST"} == {"start", "stop"}


def test_lost_start_ack_reconciles_without_repost(settings_factory, tmp_path):
    adapter, s, state, calls, original = setup_apex(
        settings_factory, tmp_path, timeout=True
    )
    raw = tmp_path / "managed"
    with pytest.raises(DomainError, match="unknown"):
        adapter.start("pipeline-session", raw, "task")
    adapter.start("pipeline-session", raw, "task")
    adapter.stop("pipeline-session")
    assert calls.count(("POST", "start")) == 1


def test_owner_change_never_stops_other_recording(settings_factory, tmp_path):
    adapter, s, state, calls, original = setup_apex(settings_factory, tmp_path)
    adapter.start("pipeline-session", tmp_path / "managed", "task")
    state["session_id"] = "someone-else"
    with pytest.raises(DomainError, match="another"):
        adapter.stop("pipeline-session")
    adapter.close()
    assert ("POST", "stop") not in calls


def test_restart_can_resolve_lost_ack_and_stop_own_session(settings_factory, tmp_path):
    adapter, settings, state, calls, original = setup_apex(
        settings_factory, tmp_path, timeout=True
    )
    raw = tmp_path / "managed"
    with pytest.raises(DomainError):
        adapter.start("pipeline-session", raw, "task")
    restarted = ApexAdapter(settings, http=adapter.http)
    restarted.stop("pipeline-session")
    assert (raw / "episode0/data/bag_0.mcap").is_file()
    assert calls.count(("POST", "start")) == 1


def test_partial_copy_does_not_overwrite_or_seal(settings_factory, tmp_path):
    adapter, settings, state, calls, original = setup_apex(settings_factory, tmp_path)
    raw = tmp_path / "managed"
    adapter.start("pipeline-session", raw, "task")
    (raw / ".copying").mkdir(parents=True)
    (raw / ".copying/partial").write_text("keep for recovery")
    with pytest.raises(DomainError, match="refusing overwrite"):
        adapter.stop("pipeline-session")
    assert not (raw / "episode0").exists()
    assert (raw / ".copying/partial").read_text() == "keep for recovery"


def test_stale_preview_is_not_reported_as_live(settings_factory, tmp_path):
    adapter, settings, state, calls, original = setup_apex(settings_factory, tmp_path)
    settings.capture_options["preview_root"] = str(tmp_path / "preview")
    preview = Path(settings.capture_options["preview_root"])
    preview.mkdir()
    (preview / "head.jpg").write_bytes(b"stale image")
    (preview / "latest.json").write_text(
        json.dumps(
            dict(source_time=1, received_time=1, files=dict(head_left="head.jpg"))
        )
    )
    assert adapter.frame("head_left") is None


def test_writer_not_drained_cannot_seal(settings_factory, tmp_path):
    adapter, s, state, calls, original = setup_apex(
        settings_factory, tmp_path, drain=False
    )
    raw = tmp_path / "managed"
    adapter.start("pipeline-session", raw, "task")
    with pytest.raises(DomainError, match="writer state unknown"):
        adapter.stop("pipeline-session")
    assert not (raw / "episode0").exists()


def test_controller_saved_recording_is_imported_without_recorder_mutation(
    settings_factory, tmp_path
):
    adapter, settings, state, calls, original = setup_apex(settings_factory, tmp_path)
    state.update(
        phase="saved",
        session_id="apex-controller-owned",
        request_id=None,
        data_state="closed",
        video_state="closed",
        path=str(original),
        ended_at=123.0,
    )
    raw = tmp_path / "controller-managed"
    result = adapter.import_controller_recording(state, raw, "client-session")
    assert result["imported"] is True
    assert result["apex_session_id"] == "apex-controller-owned"
    for path in recording_files(original):
        relative = path.relative_to(original)
        assert (raw / "episode0" / relative).read_bytes() == path.read_bytes()
    source = json.loads((raw / "episode0/bodypipeline-source.json").read_text())
    assert source["session_id"] == "client-session"
    assert source["apex_session_id"] == "apex-controller-owned"
    assert calls == []


def test_legacy_controller_session_without_request_id_is_detected(
    settings_factory, tmp_path
):
    adapter, settings, state, calls, original = setup_apex(settings_factory, tmp_path)
    settings.controller_trigger["accept_missing_request_id"] = True
    adapter.controller_trigger = settings.controller_trigger
    state.update(
        phase="recording",
        session_id="apex-controller-owned",
        request_id=None,
        data_state="recording",
        video_state="recording",
        path=str(original),
    )
    status = adapter.controller_status()
    assert status["controller_owned"] is True
    assert calls == [("GET", "status")]


def test_controller_selection_accepts_active_apex_recording(settings_factory, tmp_path):
    adapter, settings, state, calls, original = setup_apex(settings_factory, tmp_path)
    settings.controller_trigger.update(enabled=True, accept_missing_request_id=True)
    state.update(
        phase="recording",
        session_id="already-recording",
        request_id=None,
        data_state="recording",
        video_state="recording",
        path=str(original),
    )
    health = adapter.health()
    assert health["ready"] is False
    assert health["reasons"] == ["Apex recorder is occupied"]
    assert health["controller_ready"] is True
    service = CaptureService(settings, adapter=adapter)
    coordinator = ControllerCaptureCoordinator(service)
    coordinator.select(
        dict(
            dataset_name="racing selection",
            instruction="",
            local_root="default",
            local_prefix="",
            remote_target="default",
            remote_prefix="",
        )
    )
    assert coordinator.tick() is True
    row = service.store.all("capture")[0]
    assert row["controller_session_id"] == "already-recording"
    state.update(phase="saved", data_state="closed", video_state="closed")
    assert coordinator.tick() is True
    assert service.store.get("capture", row["id"])["state"] == "SEALING"
    assert all(method == "GET" for method, route in calls)


@pytest.mark.parametrize(
    "changes",
    [
        {"request_id": "another-client-recording"},
        {"project_id": "another-project"},
        {"phase": "error"},
    ],
)
def test_controller_readiness_rejects_unrelated_or_failed_apex_session(
    settings_factory,
    tmp_path,
    changes,
):
    adapter, settings, state, calls, original = setup_apex(settings_factory, tmp_path)
    settings.controller_trigger.update(enabled=True, accept_missing_request_id=True)
    state.update(
        phase="recording",
        session_id="some-session",
        request_id=None,
        data_state="recording",
        video_state="recording",
        path=str(original),
    )
    state.update(changes)
    assert adapter.health()["controller_ready"] is False
    service = CaptureService(settings, adapter=adapter)
    coordinator = ControllerCaptureCoordinator(service)
    with pytest.raises(DomainError, match="采集前检查失败"):
        coordinator.select(
            dict(
                dataset_name="selected dataset",
                instruction="",
                local_root="default",
                local_prefix="",
                remote_target="default",
                remote_prefix="",
            )
        )
    assert service.store.all("capture") == []
    assert all(method == "GET" for method, route in calls)


def test_disabled_health_and_close_are_read_only(settings_factory, tmp_path):
    adapter, s, state, calls, original = setup_apex(settings_factory, tmp_path)
    s.capture_options["allow_recording"] = False
    assert not adapter.health()["ready"]
    assert not adapter.health()["controller_ready"]
    with pytest.raises(DomainError, match="disabled"):
        adapter.start("blocked", tmp_path / "managed", "task")
    adapter.close()
    assert all(method == "GET" for method, route in calls)


def test_symlink_and_missing_video_rejected(tmp_path):
    root = recording(tmp_path / "recording")
    (root / "link").symlink_to(root / "data/bag_0.mcap")
    with pytest.raises(DomainError, match="symbolic"):
        recording_files(root)
    (root / "link").unlink()
    (root / "video/bag_0.mcap").unlink()
    with pytest.raises(DomainError, match="missing video"):
        recording_files(root)


def test_mismatched_adapter_fails_closed(settings_factory):
    with pytest.raises(ValueError, match="paired"):
        create_adapter(settings_factory(mode="ros2", capture_adapter="apex"))


def server_settings(settings_factory, **overrides):
    config = json.loads(Path("configs/server.tianji.json").read_text())
    config.pop("root")
    config.update(overrides)
    return settings_factory("server", **config)


def test_tianji_upload_and_runtime_isolation(settings_factory, tmp_path):
    s = server_settings(settings_factory)
    ingest = IngestService(s)
    raw = tmp_path / "raw"
    recording(raw / "episode0")
    files = TianjiMCAPPackager().seal(raw, profile(), {})["files"]
    manifest = dict(
        manifest_version=1,
        complete=True,
        session_id="tianji-fixture",
        robot_id=s.robot_id,
        project_id=s.project_id,
        control_mode="teleoperation",
        capture_mode="ros2",
        instruction="fixture",
        profile=profile(),
        profile_hash=digest(profile()),
        raw_format=TIANJI_MCAP_FORMAT,
        files=files,
    )
    runtime = ingest.catalog.default()
    row = ingest.create(manifest, "default", "", runtime=runtime)
    for i, spec in enumerate(files):
        data = (raw / spec["path"]).read_bytes()
        ingest.chunk(row["id"], i, 0, spec["sha256"], data)
    receipt = ingest.complete(row["id"])
    assert receipt["state"] == "COMMITTED"
    manifest["raw_format"] = "agilex_joint_camera_hdf5_v1"
    with pytest.raises(DomainError, match="raw_format"):
        ingest.validate_manifest(manifest, runtime=runtime)
    manifest["raw_format"] = TIANJI_MCAP_FORMAT
    manifest["profile"]["robot_type"] = "ARX_X5"
    manifest["profile_hash"] = digest(manifest["profile"])
    with pytest.raises(DomainError, match="robot_type"):
        ingest.validate_manifest(manifest, runtime=runtime)


def test_pr_profile_has_explicit_server_approval(settings_factory):
    s = server_settings(settings_factory)
    runtime = IngestService(s).catalog.default()
    client_config = json.loads(Path("configs/client.tianji-marvin.json").read_text())
    assert (s.robot_id, s.project_id) == (
        client_config["robot_id"],
        client_config["project_id"],
    )
    assert digest(profile()) != runtime.profile_hash
    assert runtime.accepted_capture_profile_hashes == (digest(profile()),)
    for key in (
        "robot_type",
        "control_mode",
        "joint_names",
        "joint_units",
        "output_fps",
    ):
        assert profile()[key] == runtime.profile[key]
    assert [c["id"] for c in profile()["cameras"]] == [
        c["id"] for c in runtime.profile["cameras"]
    ]
    provider = IngestService(s).catalog.provider(runtime)
    provider.validate_profile(runtime.profile)
    assert set(runtime.raw_formats) <= set(provider.raw_formats)
    for raw_format in runtime.raw_formats:
        IngestService(s).catalog.validate_capture(
            runtime,
            dict(
                profile=profile(), profile_hash=digest(profile()), raw_format=raw_format
            ),
        )
    changed = copy.deepcopy(profile())
    changed["joint_names"].reverse()
    with pytest.raises(DomainError, match="批准版本"):
        IngestService(s).catalog.validate_capture(
            runtime,
            dict(
                profile=changed,
                profile_hash=digest(changed),
                raw_format=TIANJI_MCAP_FORMAT,
            ),
        )


def test_pr_client_service_uploads_to_current_server(
    settings_factory, tmp_path, monkeypatch
):
    adapter, s, state, calls, original = setup_apex(settings_factory, tmp_path)
    s.robot_id = "marvin-pro-001"
    s.project_id = "bodypipeline-tianji"
    capture = CaptureService(s, adapter=adapter)
    row = capture.create(
        dict(
            instruction="synthetic capture routing",
            idempotency_key="apex-upload",
            local_root="default",
            remote_target="default",
        )
    )
    assert capture.start(row["id"])["state"] == "RECORDING"
    assert capture.stop(row["id"])["state"] == "REVIEW"
    sealed = capture.keep(row["id"])
    assert sealed["state"] == "SEALED", sealed
    raw = Path(sealed["raw_root"])
    manifest = json.loads((raw.parent / "manifest.json").read_text())
    assert manifest["raw_format"] == TIANJI_MCAP_FORMAT
    assert "package_path" not in sealed

    app = create_app(server_settings(settings_factory), background=False)
    with TestClient(app) as server:
        original_client = httpx.Client

        def forward(request):
            response = server.request(
                request.method,
                str(request.url),
                content=request.content,
                headers=dict(request.headers),
            )
            return httpx.Response(response.status_code, content=response.content)

        monkeypatch.setattr(
            "pipeline.client.upload.httpx.Client",
            lambda **kwargs: original_client(
                transport=httpx.MockTransport(forward), **kwargs
            ),
        )
        UploadAgent(capture).upload(sealed)
    result = capture.store.get("capture", row["id"])
    assert result["upload_state"] == "ACKED", result
    uploaded = app.state.ingest.store.get("upload", result["upload_id"])
    assert uploaded["state"] == "COMMITTED"
    assert uploaded["runtime_id"] == "tianji_thor"
    assert uploaded["profile_hash"] == app.state.runtimes.default().profile_hash
    assert app.state.ingest.manifest(uploaded)["profile_hash"] == digest(profile())
    for spec in manifest["files"]:
        assert file_hash(Path(uploaded["raw_path"]) / spec["path"]) == spec["sha256"]
        assert file_hash(raw / spec["path"]) == spec["sha256"]
    assert (original / "data/bag_0.mcap").is_file()
    adapter.close()


def test_current_server_converts_pr_mcap_with_official_toolchain(
    settings_factory, tmp_path, letools_binary
):
    """Synthetic wire-format coverage, never a robot or field acceptance test."""
    provider_bin = Path(letools_binary).with_name("tianji-provider")
    if not provider_bin.is_file():
        pytest.skip("optional Tianji provider is not installed in the toolchain")
    raw = tmp_path / "raw"
    raw.mkdir()
    generated = subprocess.run(
        [
            str(Path(letools_binary).with_name("python")),
            "tests/fixtures/tianji_mcap.py",
            str(raw / "episode0"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert generated.returncode == 0, generated.stderr
    s = server_settings(
        settings_factory,
        letools_bin=letools_binary,
        tianji_provider_bin=str(provider_bin),
        processing_mode="real",
    )
    app = create_app(s, background=False)
    ingest = app.state.ingest
    files = TianjiMCAPPackager().seal(raw, profile(), {})["files"]
    manifest = dict(
        manifest_version=1,
        complete=True,
        session_id="synthetic-mcap-conversion",
        robot_id=s.robot_id,
        project_id=s.project_id,
        control_mode="teleoperation",
        capture_mode="ros2",
        instruction="SYNTHETIC fixture only",
        profile=profile(),
        profile_hash=digest(profile()),
        raw_format=TIANJI_MCAP_FORMAT,
        files=files,
    )
    runtime = ingest.catalog.default()
    upload = ingest.create(manifest, "default", "", runtime=runtime)
    for i, spec in enumerate(files):
        data = (raw / spec["path"]).read_bytes()
        ingest.chunk(upload["id"], i, 0, spec["sha256"], data)
    ingest.complete(upload["id"])
    job = app.state.runner.enqueue(upload["id"], run_doctor=False)
    assert app.state.runner.tick()
    result = ingest.store.get("job", job["id"])
    assert result["state"] == "CONVERTED", result
    assert result["provider"] == "tianji"
    dataset = Path(result["dataset_path"])
    info = json.loads((dataset / "meta/info.json").read_text())
    assert info["robot_type"] == "tianji_marvin_pro"
    assert info["total_episodes"] == 1
    assert info["total_frames"] == result["alignment_report"]["frames"] == 60
    for field in ("observation.state", "observation.action"):
        assert info["features"][field]["shape"] == [16]
        assert info["features"][field]["names"] == profile()["joint_names"]
    assert {"observation.wrench_windows", "observation.eef"} <= set(info["features"])
    assert len(list((dataset / "videos").rglob("*.mp4"))) == 4
    assert json.loads((Path(result["work_path"]) / "validate.json").read_text())[
        "valid"
    ]
    for spec in files:
        assert file_hash(raw / spec["path"]) == spec["sha256"]
