"""Run ONLY inside the robot's ROS2 Python environment. Never starts robot drivers."""

from __future__ import annotations

import json
import math
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from pipeline.shared.core import DomainError, ProcessLock, atomic_json


class StartRequest(BaseModel):
    session_id: str = Field(pattern=r"^[a-zA-Z0-9-]{1,64}$")
    raw_root: str
    instruction: str = Field(min_length=1, max_length=2000)


class StopRequest(BaseModel):
    session_id: str


def create_bridge(settings):
    # Lazy imports keep ROS dependencies out of client/server and mock development.
    import cv2
    import rclpy
    from cv_bridge import CvBridge
    from data_msgs.msg import CaptureStatus
    from data_msgs.srv import CaptureService
    from geometry_msgs.msg import PoseStamped
    from rclpy.executors import MultiThreadedExecutor
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import Image, JointState

    rclpy.init()
    node = rclpy.create_node("embodied_pipeline_bridge")
    profile = settings.profile()
    client = node.create_client(CaptureService, profile["service"])
    mutex = threading.RLock()
    lock = ProcessLock(settings.root / "bridge.lock")
    state_file = settings.root / "bridge-state.json"
    state = (
        json.loads(state_file.read_text())
        if state_file.exists()
        else {"phase": "IDLE", "session_id": None}
    )
    if state["phase"] != "IDLE":
        state["phase"] = "UNKNOWN"
    state["boot_at"] = time.time()
    frames, seen, encoded_at = {}, {}, {}
    status = {"fail": False, "quit": False, "received_at": None}
    codec = CvBridge()
    pending = [None]

    def save():
        atomic_json(state_file, state)

    def image_callback(msg, camera_id, topic):
        seen[topic] = time.monotonic()
        if time.monotonic() - encoded_at.get(camera_id, 0) < 0.2:
            return
        try:
            image = codec.imgmsg_to_cv2(msg, desired_encoding="bgr8")
            height, width = image.shape[:2]
            if width > 640:
                image = cv2.resize(image, (640, max(1, int(height * 640 / width))))
            ok, jpg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 65])
            if ok:
                frames[camera_id] = bytes(jpg)
                encoded_at[camera_id] = time.monotonic()
        except Exception:
            pass  # Recording is independent. A failed preview remains unavailable/stale.

    for camera in profile["cameras"]:
        node.create_subscription(
            Image,
            camera["topic"],
            lambda m, c=camera: image_callback(m, c["id"], c["topic"]),
            qos_profile_sensor_data,
        )
    for topic in profile["joint_topics"]:
        node.create_subscription(
            JointState,
            topic,
            lambda m, t=topic: seen.__setitem__(t, time.monotonic()),
            qos_profile_sensor_data,
        )
    for topic in profile.get("pose_topics", []):
        node.create_subscription(
            PoseStamped,
            topic,
            lambda m, t=topic: seen.__setitem__(t, time.monotonic()),
            qos_profile_sensor_data,
        )

    def status_callback(msg):
        if state["phase"] in {"STARTING", "RECORDING", "STOPPING", "UNKNOWN"}:
            status.update(
                topics=list(msg.topics),
                count_in_seconds=list(msg.count_in_seconds),
                frequencies=[
                    float(v) if math.isfinite(v) else None for v in msg.frequencies
                ],
                fail=status["fail"] or bool(msg.fail),
                quit=bool(msg.quit),
                received_at=time.time(),
            )

    node.create_subscription(
        CaptureStatus, profile["status_topic"], status_callback, 10
    )

    def worker_nodes():
        return [
            name
            for name in node.get_node_names()
            if name.startswith("data_capture_worker_")
        ]

    def health():
        required = (
            [c["topic"] for c in profile["cameras"]]
            + profile["joint_topics"]
            + profile.get("pose_topics", [])
        )
        ages = {
            t: round(time.monotonic() - seen[t], 3) if t in seen else None
            for t in required
        }
        alive = client.service_is_ready()
        active = worker_nodes()
        return {
            "ready": alive
            and state["phase"] == "IDLE"
            and not active
            and all(a is not None and a < 2 for a in ages.values()),
            "service_ready": alive,
            "phase": state["phase"],
            "session_id": state.get("session_id"),
            "worker_nodes": active,
            "topic_ages": ages,
            "mode": "ros2",
        }

    def call(req):
        if pending[0] is not None and not pending[0].done():
            raise DomainError("上一个 ROS 请求结果未知，禁止重复下发")
        if not client.wait_for_service(timeout_sec=2):
            raise DomainError("采集 Service 不可达", 503)
        future = client.call_async(req)
        pending[0] = future
        done = threading.Event()
        future.add_done_callback(lambda _: done.set())
        if not done.wait(30):
            raise DomainError("ROS 请求超时；结果未知，不自动重试", 504)
        result = future.result()
        if not result or not result.success:
            raise DomainError(
                "Recorder 未确认成功：" + (result.message if result else "无响应")
            )
        return result

    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    thread = threading.Thread(target=executor.spin, daemon=True)

    @asynccontextmanager
    async def lifespan(app):
        lock.acquire()
        save()
        thread.start()
        try:
            yield
        finally:
            # Do not implicitly start/stop hardware on bridge shutdown.
            executor.shutdown()
            thread.join(timeout=5)
            node.destroy_node()
            rclpy.shutdown()
            lock.release()

    app = FastAPI(title="Local ROS2 capture bridge", lifespan=lifespan)
    # No browser cookies are used for this internal, loopback-only service.
    import hmac

    @app.middleware("http")
    async def auth(request: Request, call_next):
        from fastapi.responses import JSONResponse

        if not hmac.compare_digest(
            request.headers.get("authorization", ""), "Bearer " + settings.bridge_token
        ):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)

    @app.exception_handler(DomainError)
    async def error(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": exc.message}, status_code=exc.code)

    @app.get("/health")
    def get_health():
        return health()

    @app.get("/status")
    def get_status():
        return {**status, **state}

    @app.get("/frames/{cid}")
    def frame(cid: str):
        if cid not in frames or time.monotonic() - encoded_at.get(cid, 0) > 3:
            raise DomainError("没有新鲜预览帧", 503)
        return Response(frames[cid], media_type="image/jpeg")

    @app.post("/start")
    def start(body: StartRequest):
        with mutex:
            if (
                state["phase"] == "RECORDING"
                and state.get("session_id") == body.session_id
            ):
                return {"started": True}
            h = health()
            if not h["ready"]:
                raise DomainError(f"采集前检查未通过：{h}")
            raw = Path(body.raw_root)
            roots = [Path(p).resolve() for p in settings.local_roots.values()]
            if (
                not roots
                or not raw.is_absolute()
                or raw.resolve() != raw
                or not any(raw.is_relative_to(r) for r in roots)
            ):
                raise DomainError("不在 Bridge 允许的采集根目录内", 400)
            if (
                not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(raw))
                or ".." in raw.parts
                or (raw / "episode0").exists()
            ):
                raise DomainError("拒绝危险路径或覆盖已有 episode", 400)
            # Both Client and Bridge persist the intent before a non-idempotent service call.
            state.update(
                phase="STARTING", session_id=body.session_id, raw_root=str(raw)
            )
            status.update(fail=False, quit=False, received_at=None)
            save()
            request = CaptureService.Request()
            request.start, request.end = True, False
            request.episode_index, request.dataset_dir = 0, str(raw)
            request.instructions = (
                body.instruction
            )  # Legacy node may ignore it; manifest remains authoritative.
            try:
                call(request)
                deadline = time.monotonic() + 2
                owned = worker_nodes()
                while not owned and time.monotonic() < deadline:
                    time.sleep(0.1)
                    owned = worker_nodes()
                if len(owned) != 1:
                    raise DomainError("无法确认唯一 recorder worker，禁止宣称启动成功")
                deadline = time.monotonic() + 3
                while status.get("received_at") is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                if status.get("received_at") is None:
                    raise DomainError("Recorder 未发布首条状态，禁止宣称录制已开始")
                if status.get("fail"):
                    raise DomainError("Recorder 首条状态报告采集异常")
                state.update(phase="RECORDING", owner_nodes=owned)
                save()
                return {"started": True}
            except Exception:
                state["phase"] = "UNKNOWN"
                save()
                raise

    @app.post("/stop")
    def stop(body: StopRequest):
        with mutex:
            if state.get("session_id") != body.session_id:
                raise DomainError("会话 owner 不匹配")
            if state["phase"] == "IDLE":
                return {"stopped": True, "fail": status["fail"]}
            current = worker_nodes()
            owned = state.get("owner_nodes", [])
            if not owned or (current and set(current) != set(owned)):
                raise DomainError(
                    "无法证明当前 recorder 属于该会话，需要人工核验；不停止其他 owner"
                )
            state["phase"] = "STOPPING"
            save()
            request = CaptureService.Request()
            request.start, request.end, request.episode_index = False, True, -1
            try:
                call(request)
                state["phase"] = "IDLE"
                save()
                return {
                    "stopped": True,
                    "fail": status["fail"],
                    "error": "Recorder health failure" if status["fail"] else None,
                }
            except Exception:
                state["phase"] = "UNKNOWN"
                save()
                raise

    return app
