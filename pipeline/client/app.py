from __future__ import annotations

import asyncio
import io
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import quote

from fastapi import FastAPI
from fastapi.responses import Response, StreamingResponse
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from pipeline.shared.core import DomainError, ProcessLock
from pipeline.shared.security import install_security
from pipeline.shared.web import mount_ui

from .control_modes import ControlModeService
from .inference import InferenceService
from .dagger_episodes import DaggerEpisodeService
from .replay import ReplayService
from .service import CaptureService
from .upload import UploadAgent
from .video import VideoDownload


class CaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_name: str | None = Field(default=None, min_length=1, max_length=2000)
    collector_name: str = Field(default="", max_length=200)
    data_path: str | None = Field(default=None, min_length=1, max_length=4096)
    instruction: str = Field(default="", max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=128)
    # Kept for compatibility with older clients; the new UI uses data_path.
    local_root: str = "default"
    local_prefix: str = ""
    continuous: bool = False


class DataPathRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_name: str = Field(min_length=1, max_length=2000)
    collector_name: str = Field(min_length=1, max_length=200)


class ControlModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["capture", "inference"]


class InferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    checkpoint_path: str = Field(min_length=1, max_length=4096)
    prompt: str = Field(min_length=1, max_length=4000)
    request_id: str = Field(min_length=8, max_length=128)


class InferenceFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result: Literal["failed", "success"]
    request_id: str = Field(min_length=8, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)


class DaggerControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["takeover", "resume", "save", "discard", "finish"]
    session_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=8, max_length=128)


class ReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root: str
    path: str
    request_id: str = Field(min_length=8, max_length=128)


def create_app(settings, background=True):
    service = CaptureService(settings)
    uploader = UploadAgent(service)
    video = VideoDownload(service)
    replay = ReplayService(settings, service)
    service.replay = replay
    inference = InferenceService(settings, service, replay)
    dagger_episodes = DaggerEpisodeService(settings, service, inference)
    control_modes = ControlModeService(settings, service, inference, replay)
    service.control_modes = inference.control_modes = control_modes
    lock = ProcessLock(settings.root / "client.lock")
    stopping = asyncio.Event()

    async def upload_loop():
        while not stopping.is_set():
            await asyncio.to_thread(uploader.process_once)
            try:
                await asyncio.wait_for(stopping.wait(), timeout=settings.poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def dagger_episode_loop():
        while not stopping.is_set():
            await asyncio.to_thread(dagger_episodes.tick)
            try:
                await asyncio.wait_for(stopping.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass

    async def health_loop():
        while not stopping.is_set():
            try:
                await asyncio.to_thread(service.sync_continuous)
                await asyncio.to_thread(service.sync_pending_starts)
                await asyncio.to_thread(service.sync_recording_stops)
            except Exception:
                # A temporary status outage never authorizes another start.
                pass
            for row in service.store.all("capture"):
                if row["state"] == "RECORDING":
                    import shutil

                    status = await asyncio.to_thread(service.health)
                    if status.get("status", {}).get("session_id") not in (
                        None,
                        row["id"],
                    ):
                        continue
                    low_disk = (
                        shutil.disk_usage(row["raw_root"]).free
                        < settings.min_free_bytes
                    )
                    failed = status.get("status", {}).get("fail", False)
                    if low_disk or failed:
                        await asyncio.to_thread(
                            service.stop,
                            row["id"],
                            fault="磁盘低水位" if low_disk else "Recorder 报告失败",
                        )
            try:
                await asyncio.wait_for(stopping.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass

    @asynccontextmanager
    async def lifespan(app):
        lock.acquire()
        service.recover()
        tasks = (
            [
                asyncio.create_task(health_loop()),
                asyncio.create_task(upload_loop()),
                asyncio.create_task(dagger_episode_loop()),
            ]
            if background
            else []
        )
        try:
            yield
        finally:
            stopping.set()
            video.close()
            if tasks:
                await asyncio.gather(*tasks)
            await asyncio.to_thread(replay.close)
            await asyncio.to_thread(dagger_episodes.close)
            inference.close()
            service.adapter.close()
            lock.release()

    app = FastAPI(title="Embodied Pipeline Client", lifespan=lifespan)
    app.state.service = service
    app.state.uploader = uploader
    app.state.video = video
    app.state.replay = replay
    app.state.inference = inference
    app.state.dagger_episodes = dagger_episodes
    install_security(app, settings)
    mount_ui(app, replay=True)

    @app.get("/api/replay/status")
    def replay_status():
        return replay.status()

    @app.get("/api/replay/captures")
    def replay_captures():
        return replay.captures()

    @app.get("/api/replay/files")
    def replay_files(root: str, directory: str = ""):
        return replay.files(root, directory)

    @app.get("/api/replay/inspect")
    def replay_inspect(root: str, path: str):
        return replay.inspect(root, path)

    @app.post("/api/replay/start", status_code=202)
    def replay_start(body: ReplayRequest):
        return replay.start(body.root, body.path, body.request_id)

    @app.post("/api/replay/stop", status_code=202)
    def replay_stop():
        return replay.stop()

    @app.get("/healthz")
    def live():
        return {"service": "client", "alive": True}

    @app.get("/api/config")
    def config():
        return {
            "role": "client",
            "mode": settings.mode,
            "robot_id": settings.robot_id,
            "profile": service.profile,
            "local_roots": [
                {"id": k, "path": str(v)} for k, v in service.roots.items()
            ],
            "data_root": str(service._capture_root()),
            "capture_destination": service.capture_destination(),
            "upload_destination": {
                key: value
                for key, value in settings.capture.get("upload_ssh", {}).items()
                if key in {"host", "port", "user", "root"}
            },
            "auto_upload_on_next_recording": True,
            "mode_switching_enabled": control_modes.enabled,
            "recording_enabled": settings.capture.get("recording_enabled", True),
            "continuous_capture": service.backend.id == "arx_ros2"
            and settings.capture.get("start_trigger", "arx_buttons") == "arx_buttons"
            and settings.capture.get("continuous", True),
        }

    @app.get("/api/health")
    def health():
        return service.health()

    @app.get("/api/control-mode")
    def control_mode_status():
        return control_modes.status()

    @app.post("/api/control-mode")
    def control_mode_switch(body: ControlModeRequest):
        return control_modes.switch(body.mode)

    @app.post("/api/arm/enable")
    def arm_enable():
        if control_modes.enabled:
            control_modes.require("capture")
        return service.enable_arm()

    @app.post("/api/arm/reset")
    def arm_reset():
        return service.reset_arm()

    @app.post("/api/data-paths", status_code=201)
    def create_data_path(body: DataPathRequest):
        return service.create_data_path(body.task_name, body.collector_name)

    @app.get("/api/data-paths")
    def browse_data_paths(path: str = ""):
        return service.browse_data_paths(path)

    @app.post("/api/capture-destination")
    def select_capture_destination(body: CaptureRequest):
        data = body.model_dump(exclude_none=True)
        data["instruction"] = data["instruction"].strip()
        return service.select_destination(data)

    @app.get("/api/captures")
    def captures():
        with service.lock:
            return [service.describe(row) for row in service.store.all("capture")]

    @app.get("/api/capture-status")
    def capture_status():
        # Keep polling small: full profiles and manifests are not needed by the UI.
        omitted = {
            "profile",
            "profile_hash",
            "request_hash",
            "idempotency_key",
            "receipt",
            "manifest_hash",
        }
        with service.lock:
            return [
                {k: v for k, v in service.describe(row).items() if k not in omitted}
                for row in service.store.all("capture")
            ]

    @app.post("/api/captures/start", status_code=201)
    def create_and_start(body: CaptureRequest):
        data = body.model_dump(exclude_none=True)
        data["instruction"] = data["instruction"].strip()
        if not (data.get("dataset_name") or data["instruction"]):
            raise DomainError("请填写任务名称", 400)
        return service.create_and_start(data)

    @app.post("/api/captures", status_code=201)
    def create(body: CaptureRequest):
        data = body.model_dump(exclude_none=True)
        data["instruction"] = data["instruction"].strip()
        if not (data.get("dataset_name") or data["instruction"]):
            raise DomainError("请填写任务名称", 400)
        return service.create(data)

    @app.get("/api/captures/{cid}")
    def get(cid: str):
        return service.store.get("capture", cid)

    @app.post("/api/captures/{cid}/start")
    def start(cid: str):
        return service.start(cid)

    @app.post("/api/captures/{cid}/stop")
    def stop(cid: str):
        return service.stop(cid)

    @app.post("/api/captures/{cid}/discard")
    def discard(cid: str):
        return service.stop(cid, discard=True)

    @app.post("/api/captures/{cid}/delete")
    def delete_capture(cid: str):
        return service.delete(cid)

    @app.post("/api/captures/{cid}/retry-convert-upload")
    def retry_convert_upload(cid: str):
        return service.retry_conversion_and_upload(cid)

    @app.post("/api/captures/{cid}/video")
    def download_video(cid: str):
        filename, content, finish = video.start(cid)
        return StreamingResponse(content, media_type="video/mp4", headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename),
            "Cache-Control": "no-store",
        }, background=BackgroundTask(finish))

    @app.post("/api/captures/{cid}/retry-upload")
    def retry_upload(cid: str):
        with service.lock:
            row = service.store.get("capture", cid)
            if row.get("upload_state") != "RETRY":
                raise DomainError("该 Episode 没有等待重试的上传")
            if row.get("discard_requested"):
                raise DomainError("该片段已请求删除，不能重新上传")
            row.update(upload_state="QUEUED", retry_at=0)
            return service.save(row)

    @app.get("/api/joints")
    def joint_positions():
        names = service.profile.get("joint_names") or []
        try:
            snapshot = service.adapter.joints()
        except (DomainError, AttributeError):
            snapshot = {"ready": False, "positions": [None] * len(names),
                        "timestamp": None, "sample_id": None,
                        "simulated": settings.mode == "mock"}
        names = names or snapshot.get("names") or [
            f"joint_{index + 1}" for index in range(len(snapshot.get("positions", [])))
        ]
        units = service.profile.get("joint_units") or ["rad"] * len(names)
        return {**snapshot, "names": names, "units": units}


    @app.get("/api/inference/status")
    def inference_status():
        return inference.status()

    @app.post("/api/inference/start")
    def inference_start(body: InferenceRequest):
        return inference.action("start", body.model_dump())

    @app.post("/api/inference/enable")
    def inference_enable():
        return inference.control("enable")

    @app.post("/api/inference/prepare")
    def inference_prepare():
        return inference.control("prepare")

    @app.post("/api/inference/pause")
    def inference_pause():
        return inference.control("pause")

    @app.post("/api/inference/reset")
    def inference_reset():
        return inference.control("reset")

    @app.post("/api/inference/stage2")
    def inference_stage2(body: InferenceRequest):
        return inference.action("stage2", body.model_dump())

    @app.post("/api/inference/dagger")
    def inference_dagger(body: InferenceRequest):
        return inference.action("dagger", body.model_dump())

    @app.post("/api/inference/dagger/control")
    def inference_dagger_control(body: DaggerControlRequest):
        return inference.dagger_control(body.model_dump())

    @app.get("/api/inference/dagger/episodes")
    def inference_dagger_episodes():
        return {"enabled": dagger_episodes.enabled, "episodes": dagger_episodes.rows(),
                "error": dagger_episodes.error, "auto_upload": True}

    @app.post("/api/inference/dagger/episodes/{episode_id}/retry")
    def retry_dagger_episode(episode_id: str):
        dagger_episodes.retry(episode_id)
        return {"ok": True}

    @app.post("/api/inference/dagger/episodes/{episode_id}/delete", status_code=202)
    def delete_dagger_episode(episode_id: str):
        return dagger_episodes.delete(episode_id)

    @app.post("/api/inference/feedback")
    def inference_feedback(body: InferenceFeedbackRequest):
        return inference.action("feedback", body.model_dump())

    @app.get("/api/cameras/{camera_id}/frame")
    def frame(camera_id: str, preview: Literal["full", "low"] = "full"):
        if camera_id not in {c["id"] for c in service.profile["cameras"]}:
            raise DomainError("相机不存在", 404)
        content = service.adapter.frame(camera_id)
        if not content:
            raise DomainError("暂未收到相机图像", 503)
        if preview == "low":
            # Preview only; recorded images remain at their original resolution.
            with Image.open(io.BytesIO(content)) as image:
                image.thumbnail((320, 320))
                output = io.BytesIO()
                image.convert("RGB").save(output, format="JPEG", quality=45)
                content = output.getvalue()
        return Response(
            content, media_type="image/jpeg", headers={"Cache-Control": "no-store"}
        )

    return app
