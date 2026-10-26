"""Reuse RosOperator for observations and explicit arm enable/reset requests.

The recorder observes physical keys. Driver startup and controller commands occur
only through the operator's enable/reset endpoints, never from source startup.
"""

from __future__ import annotations

import importlib
import math
import sys
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

from pipeline.shared.arx import CAMERAS, SOURCES
from pipeline.shared.core import DomainError

from .arx_buttons import ArxButtonSequence
from .arx_hardware import ArxObservationLauncher
from .arx_raw import RawStreamTap


class ArxRos2Source:
    def __init__(self, settings):
        # These paths are trusted deployment configuration, never request fields.
        root = Path(settings.capture["repository"]).expanduser().resolve()
        act = root / "act"
        if not (act / "utils/ros_operator.py").is_file():
            raise DomainError("ARX repository 必须指向 ROS2_AC-one_Play")
        sys.path.insert(0, str(act))
        loader = importlib.import_module("utils.setup_loader")
        if Path(loader.__file__).resolve() != act / "utils/setup_loader.py":
            raise DomainError("导入了其他仓库的 utils.setup_loader")
        loader.setup_loader(act)
        module = importlib.import_module("utils.ros_operator")
        if Path(module.__file__).resolve() != act / "utils/ros_operator.py":
            raise DomainError("导入了其他仓库的 utils.ros_operator")
        if not hasattr(module.RosOperator, "_take_observation_messages"):
            raise DomainError("现场 RosOperator 缺少已核查的快照接口")
        import cv2
        import rclpy
        from rclpy.executors import SingleThreadedExecutor
        from std_msgs.msg import Int32MultiArray
        from std_srvs.srv import Trigger

        self.cv2 = cv2
        self.rclpy = rclpy
        self.closed = False
        self.executor_error = None
        self.lock = threading.Lock()
        self.raw_tap = (
            RawStreamTap()
            if settings.capture.get("recording_mode", "snapshot") == "raw_first"
            else None
        )
        self.seen = {}
        self.preview_messages = {}
        self.receipts = {name: deque(maxlen=32) for name in SOURCES}
        self.buttons = ArxButtonSequence()
        self.trigger_type = Trigger
        self.hardware = ArxObservationLauncher(settings)
        self.control_mode = self.hardware.control_mode
        self.mode_error = None
        self.mode_switching = False
        self.arm_topics_by_mode = {
            "capture": dict(settings.profile()["arm_topics"]),
            "inference": {"left": "/arm_slave_l_status", "right": "/arm_slave_r_status"},
        }
        self.joint_can_checked_at = 0.0
        self.joint_missing_can = []
        self.wait_for_buttons = (self.control_mode == "capture"
                                 and (settings.capture.get("start_trigger", "arx_buttons") == "arx_buttons"
                                      or settings.capture.get("switch_control_modes", False)))
        self.taken = None
        self.taken_received = None
        owner = self
        config = self._config(settings.profile())

        class ObservationNode(module.RosOperator):
            def create_subscription(
                self, msg_type, topic, callback, qos_profile, **kwargs
            ):
                arm_topics = {topic: side for topics in owner.arm_topics_by_mode.values() for side, topic in topics.items()}
                callback = owner.select_arm_callback(topic, callback)
                sensor_topics = set(config["camera_config"].values()) | set(arm_topics)
                if owner.raw_tap is not None and topic in sensor_topics:
                    qos_profile = int(settings.capture.get("raw_ros_queue_depth", 128))
                    if not 2 <= qos_profile <= 1024:
                        raise DomainError("raw_ros_queue_depth 必须介于 2 和 1024")
                return super().create_subscription(
                    msg_type, topic, callback, qos_profile, **kwargs
                )

            def _take_observation_messages(self, ts):
                with owner.lock:
                    messages = super()._take_observation_messages(ts)
                    owner.taken = messages
                    owner.taken_received = None
                    if messages is not None:
                        received = []
                        for name in SOURCES:
                            key = (
                                ("images", name) if name in CAMERAS else ("arms", name)
                            )
                            msg = messages[key]
                            at = next(
                                (
                                    at
                                    for item, at in owner.receipts[name]
                                    if item is msg
                                ),
                                None,
                            )
                            if at is None:
                                break
                            received.append(at)
                        if len(received) == len(SOURCES):
                            owner.taken_received = min(received)
                    return messages

            def joy_callback(self, msg):
                with owner.lock:
                    values = list(msg.data)
                    owner.buttons.observe(values, time.monotonic())
                super().joy_callback(msg)

        def callback(method, name, image=False):
            def receive(node, msg):
                received_ns = time.monotonic_ns()
                with owner.lock:
                    at = received_ns / 1e9
                    owner.seen[name] = at
                    owner.receipts[name].append((msg, at))
                    if image:
                        owner.preview_messages[name] = msg
                if owner.raw_tap is not None:
                    # Both subscriptions point to the same arm topic. Record
                    # feedback once; avoid the vendor's duplicate arm enqueue.
                    if image or method.startswith("feedback_"):
                        owner.raw_tap.observe(name, msg, received_ns)
                    return
                getattr(module.RosOperator, method)(node, msg)

            return receive

        for name, method in zip(
            CAMERAS, ("img_head_callback", "img_left_callback", "img_right_callback")
        ):
            setattr(ObservationNode, method, callback(method, name, True))
        for side in ("left", "right"):
            for role in ("feedback", "controller"):
                method = f"{role}_{side}_callback"
                setattr(ObservationNode, method, callback(method, f"{side}_arm"))
        rclpy.init()
        try:
            self.node = ObservationNode(
                SimpleNamespace(
                    camera_names=list(CAMERAS),
                    use_depth_image=False,
                    use_base=False,
                    record="Distance",
                ),
                config,
                in_collect=True,
            )
            if not hasattr(self.node, "_take_observation_messages") or not hasattr(
                self.node, "observation_lock"
            ):
                raise DomainError(
                    "现场 RosOperator 缺少已核查的快照接口；请勿回退到未知实现"
                )
            self.joy_topic = config["joy_config"]["joy_topic"]
            self.joy_publisher = self.node.create_publisher(
                Int32MultiArray, self.joy_topic, 10
            )
            # Subscribe to both feedback namespaces; callbacks select the active
            # mode, so policy commands can never be recorded as measured joints.
            for side in ("left", "right"):
                for mode in ("capture", "inference"):
                    topic = self.arm_topics_by_mode[mode][side]
                    if topic == config["arm_config"][f"feedback_{side}_topic"]:
                        continue
                    for role in ("feedback", "controller"):
                        self.node.create_subscription(self.node.robot_status, topic,
                            getattr(self.node, f"{role}_{side}_callback"), 128)
            self.services_by_mode = {
                mode: {
                    action: [self.node.create_client(Trigger, f"/arm_{kind}_{side}/{action}")
                             for side in ("l", "r")]
                    for action in ("reset_check", "reset", "enable", "health_check", "remember_fold", "select_capture", "select_inference")
                }
                for mode, kind in (("capture", "slave" if self.hardware.resident_control else "master"), ("inference", "slave"))
            }
            self.arm_services = self.services_by_mode[self.control_mode]
            self.executor = SingleThreadedExecutor()
            self.executor.add_node(self.node)
            self.thread = threading.Thread(target=self._spin, daemon=True)
            self.thread.start()
        except Exception:
            if hasattr(self, "node"):
                self.node.destroy_node()
            rclpy.shutdown()
            raise

    def _spin(self):
        try:
            self.executor.spin()
        except Exception as exc:
            # Jazzy can raise InvalidHandle while shutdown destroys guard
            # conditions. Unexpected failures must make the source unavailable.
            if not self.closed:
                with self.lock:
                    self.executor_error = f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _config(profile):
        topics = {c["id"]: c["topic"] for c in profile["cameras"]}
        arms = profile["arm_topics"]
        return {
            "camera_config": {
                f"img_{key}_topic": topics[name]
                for key, name in zip(("head", "left", "right"), CAMERAS)
            },
            "arm_config": {
                f"{role}_{side}_topic": arms[side]
                for role in ("feedback", "controller")
                for side in ("left", "right")
            },
            "joy_config": {"joy_topic": profile["joy_topic"]},
        }

    def health(self):
        missing_can = self.hardware._missing_can()
        with self.lock:
            ages = {
                name: time.monotonic() - self.seen[name] if name in self.seen else None
                for name in SOURCES
            }
            buttons_age = (
                time.monotonic() - self.buttons.seen_at
                if self.buttons.seen_at is not None
                else None
            )
        observations_ready = (not self.closed and not missing_can and not self.executor_error
                              and all(age is not None and age < 2 for age in ages.values())
                              and (not self.wait_for_buttons or (buttons_age is not None and buttons_age < 2)))
        return {
            "control_mode": getattr(self, "control_mode", "capture"),
            "mode_switching": getattr(self, "mode_switching", False),
            "mode_error": getattr(self, "mode_error", None),
            "observations_ready": observations_ready,
            "ready": observations_ready and not getattr(self, "mode_switching", False) and not getattr(self, "mode_error", None),
            "can_interfaces": {name: name not in missing_can for name in ("can1", "can3", "can6")},
            "topic_ages": ages,
            "buttons_age": buttons_age,
            "source_error": self.executor_error,
            "reset_available": all(
                client.service_is_ready() for client in self.arm_services["reset_check"]
            ),
        }

    def ensure_observation(self, timeout=30):
        self.hardware.start()
        # Require fresh callbacks, not cached ROS messages from a failed device.
        with self.lock:
            self.seen.clear()
            for receipts in self.receipts.values():
                receipts.clear()
        deadline = time.monotonic() + timeout
        repair_at = time.monotonic() + min(8, timeout / 2)
        repaired = False
        missing = []
        while time.monotonic() < deadline:
            health = self.health()
            if health.get("ready") or (getattr(self, "mode_switching", False) and health.get("observations_ready")):
                return health
            if self.executor_error:
                raise DomainError(f"ARX 观测订阅异常：{self.executor_error}")
            missing = [name for name, age in health.get("topic_ages", {}).items()
                       if age is None or age >= 2]
            if self.wait_for_buttons and (health.get("buttons_age") is None or health["buttons_age"] >= 2):
                missing.append("buttons")
            if not all(health.get("can_interfaces", {}).values()):
                missing.append("can")
            if not repaired and time.monotonic() >= repair_at:
                self.hardware.repair_observation(missing)
                with self.lock:
                    self.seen.clear()
                    for receipts in self.receipts.values():
                        receipts.clear()
                repaired = True
            time.sleep(0.25)
        raise DomainError("自动恢复后设备仍未就绪：" + ", ".join(missing)
                          + "。请检查对应设备的供电、连接和日志。", 503)

    def select_arm_callback(self, topic, callback):
        side = next((side for topics in self.arm_topics_by_mode.values()
                     for side, value in topics.items() if value == topic), None)
        if side is None:
            return callback
        def selected(msg):
            if self.arm_topics_by_mode.get(self.control_mode, {}).get(side) == topic:
                callback(msg)
        return selected

    def control_state(self):
        hot_switch = False
        if self.hardware.resident_control:
            try:
                self.hardware.assert_resident_compatible()
                hot_switch = True
            except DomainError:
                pass
        return {"hot_switch_supported": hot_switch,"mode": None if self.mode_error or self.mode_switching else self.control_mode,
                "requested_mode": self.hardware.control_mode, "switching": self.mode_switching,
                "error": self.mode_error, "supported_modes": ["capture", "inference"]}

    def set_control_mode(self, mode):
        if mode not in self.arm_topics_by_mode:
            raise DomainError("未知机械臂控制模式", 400)
        self.hardware.assert_resident_compatible()
        original = self.control_mode
        self.mode_switching = True
        self.mode_error = None
        self.buttons.disarm()
        try:
            # A stopped stack may be selected, but only explicit enable starts
            # hardware. A running stack must acknowledge both input gates.
            marker = f"ros2 launch arx_x5_controller {self.hardware.controller_launch}"
            running = self.hardware._external_running(marker)
            if running:
                self._arm_service_calls("select_" + mode)
            self.hardware.switch_mode(mode)
            with self.lock:
                self.control_mode = mode
                self.arm_services = self.services_by_mode[mode]
                self.wait_for_buttons = mode == "capture"
                self.taken = self.taken_received = None
            self.mode_switching = False
            return self.control_state()
        except Exception as exc:
            # Fail closed rather than claim two arms switched successfully.
            if original in self.arm_topics_by_mode:
                try:
                    self._arm_service_calls("select_" + original)
                except Exception:
                    pass
            self.mode_switching = False
            self.mode_error = str(exc)
            raise

    def remember_fold(self):
        return self._arm_service_calls("remember_fold")

    def prepare_control(self, timeout=20):
        """Start the control stack and wait only for arm services.

        Camera readiness is reported separately; it must not block explicit
        motor enable when a RealSense is disconnected.
        """
        self.hardware.start()
        deadline = time.monotonic() + float(timeout)
        clients = self.arm_services.get("enable", [])
        while time.monotonic() < deadline:
            if len(clients) == 2 and all(client.service_is_ready() for client in clients):
                return {"control_ready": True}
            time.sleep(0.1)
        raise DomainError("机械臂控制器接口未在规定时间内就绪，请检查 CAN 和控制器日志", 503)

    def enable_arm(self):
        # The controller recreates a faulted vendor driver while preserving its
        # folded reference. A local /arx_joy echo cannot confirm motor enable.
        if self.hardware.resident_control:
            self._arm_service_calls("select_" + self.control_mode)
        results = self._arm_service_calls("enable", timeout=35)
        self._arm_service_calls("health_check")
        return {"enabled": True, "controllers": results,
                "message": "双臂控制器已确认使能"}

    def _arm_service_calls(self, action, timeout=3):
        clients = self.arm_services[action]
        label = "使能" if action in {"enable", "health_check"} else "合拢参考" if action == "remember_fold" else "复位"
        if len(clients) != 2 or not all(
            client.service_is_ready() for client in clients
        ):
            raise DomainError(f"机械臂{label}接口未就绪，请启动已更新的双臂控制器", 503)
        futures = [client.call_async(self.trigger_type.Request()) for client in clients]
        deadline = time.monotonic() + timeout
        results = []
        failures = []
        for future in futures:
            done = threading.Event()
            future.add_done_callback(lambda _, event=done: event.set())
            if not done.wait(max(0, deadline - time.monotonic())):
                raise DomainError(
                    f"机械臂{label}接口响应超时，请检查控制器状态；不会自动重发", 504
                )
            response = future.result()
            if response is None or not response.success:
                failures.append(response.message if response else f"机械臂{label}接口没有响应")
            else:
                results.append(response.message)
        if failures:
            raise DomainError("；".join(failures), 409)
        return results

    def check_reset(self):
        with self.lock:
            now = time.monotonic()
            for name in ("left_arm", "right_arm"):
                if not self.receipts[name] or now - self.receipts[name][-1][1] > 0.5:
                    raise DomainError("双臂反馈未就绪，不能复位", 503)
        return self._arm_service_calls("reset_check")

    def reset_arm(self):
        self._arm_service_calls("reset")
        return {
            "resetting": True,
            "message": "复位指令已发出，正在返回首次起臂前的收拢位姿。连续采集已结束。",
        }

    def arm_recording(self, at):
        with self.lock:
            self.buttons.arm(at)

    def recording_started(self, at):
        with self.lock:
            self.buttons.recording_started(at)

    def preparation(self):
        with self.lock:
            return self.buttons.status()

    def disarm_recording(self):
        with self.lock:
            self.buttons.disarm()

    def read(self):
        if self.executor_error:
            raise DomainError(f"ARX 观测订阅异常：{self.executor_error}")
        if self.raw_tap is not None:
            return self.raw_tap.control_sample()
        observation = self.node.get_observation()
        if observation is None:
            return None
        if self.taken_received is None:
            return None
        sampled_at = time.monotonic()
        messages = self.taken
        source_times = {}
        for name in SOURCES:
            key = ("images", name) if name in CAMERAS else ("arms", name)
            stamp = messages[key].header.stamp
            source_times[name] = float(stamp.sec) + float(stamp.nanosec) * 1e-9
        images = {}
        for name in CAMERAS:
            ok, encoded = self.cv2.imencode(
                ".jpg", observation["images"][name], [self.cv2.IMWRITE_JPEG_QUALITY, 90]
            )
            if not ok:
                raise DomainError(f"相机 JPEG 编码失败：{name}")
            images[name] = encoded.tobytes()
        return {
            **observation,
            "images": images,
            "timestamp": sampled_at,
            "received_monotonic": self.taken_received,
            "source_timestamps": source_times,
        }

    def joints(self):
        if time.monotonic() - self.joint_can_checked_at > 1:
            self.joint_missing_can = self.hardware._missing_can()
            self.joint_can_checked_at = time.monotonic()
        values, ages, received = [], {}, []
        with self.lock:
            # Timestamp after CAN checks and after locking: feedback may have
            # arrived during those checks, and its age must never be negative.
            now_mono, now_wall = time.monotonic(), time.time()
            for side in ("left", "right"):
                rows = self.receipts[side + "_arm"]
                msg, at = rows[-1] if rows else (None, None)
                age = now_mono - at if at is not None else None
                ages[side] = age
                q = list(msg.joint_pos) if msg is not None else []
                valid = (not self.closed and not self.executor_error
                         and ("can1" if side == "left" else "can3") not in self.joint_missing_can
                         and age is not None
                         and age < 0.5 and len(q) == 7
                         and all(math.isfinite(float(value)) for value in q))
                values.extend([float(value) for value in q] if valid else [None] * 7)
                if at is not None:
                    received.append(at)
        return {
            "groups": [
                {"label": "左臂 · LEFT ARM", "indices": list(range(7)),
                 "labels": ["J1", "J2", "J3", "J4", "J5", "J6", "夹爪"]},
                {"label": "右臂 · RIGHT ARM", "indices": list(range(7, 14)),
                 "labels": ["J1", "J2", "J3", "J4", "J5", "J6", "夹爪"]},
            ],
            "positions": values,
            "ready": all(value is not None for value in values),
            "timestamp": now_wall - (now_mono - max(received)) if received else None,
            "sample_id": ":".join(str(at) for at in received) if received else None,
            "ages": ages,
            "simulated": False,
        }

    def begin_raw(self, journal, since):
        if self.raw_tap is None:
            raise DomainError("观测源未配置 raw_first")
        self.raw_tap.begin(journal, since)

    def end_raw(self, journal):
        if self.raw_tap is not None:
            self.raw_tap.end(journal)

    def frame(self, camera_id):
        with self.lock:
            msg = self.preview_messages.get(camera_id)
            fresh = time.monotonic() - self.seen.get(camera_id, 0) < 2
        if msg is None or not fresh:
            return None
        # Preview decoding is independent of the recorder's observation queues.
        try:
            image = self.node.bridge.compressed_imgmsg_to_cv2(msg, "passthrough")
            if image.shape[1] > 640:
                image = self.cv2.resize(
                    image, (640, max(1, int(image.shape[0] * 640 / image.shape[1])))
                )
            ok, encoded = self.cv2.imencode(".jpg", image)
            return encoded.tobytes() if ok else None
        except Exception:
            return None

    def close(self):
        self.closed = True
        try:
            self.executor.shutdown(timeout_sec=5)
            self.thread.join(timeout=5)
        finally:
            # Stop child hardware processes before touching the ROS context so
            # a shutdown exception cannot leave controllers or cameras running.
            self.hardware.stop()
            try:
                self.node.destroy_node()
            finally:
                try:
                    self.rclpy.shutdown()
                except Exception:
                    pass
