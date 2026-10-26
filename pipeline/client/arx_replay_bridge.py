"""ARX replay using the already-running capture controllers.

Motion and its physical limits come from the installed ARX execution tool. This
module owns goal publication, not the SDK, CAN interfaces or driver processes.
"""

from __future__ import annotations

import importlib.util
import json
import math
import threading
import time
from pathlib import Path

import numpy as np

from pipeline.shared.core import DomainError, digest

MOVING = {"aligning", "settling", "replaying", "finishing"}
CAPTURING = {"PREPARING", "STARTING", "RECORDING", "STOPPING", "RECOVERY_REQUIRED"}


def load_tool(root, name):
    path = Path(root).resolve() / (name + ".py")
    spec = importlib.util.spec_from_file_location("bodypipeline_arx_" + name, path)
    if spec is None or spec.loader is None:
        raise DomainError(f"无法加载 ARX 执行工具：{path}", 503)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ArxReplayControl:
    def __init__(
        self,
        recorder,
        transport,
        profile,
        motion_factory,
        validators,
        clock=time.monotonic,
    ):
        self.recorder, self.transport, self.profile = recorder, transport, profile
        self.motion_factory, self.validators, self.clock = (
            motion_factory,
            validators,
            clock,
        )
        self.mutex = recorder.mutex
        self.motion = motion_factory()
        self.owner = None
        self.request_hash = None
        self.closed = False

    def _capture_busy(self):
        return self.recorder.row is not None and self.recorder.row["phase"] in CAPTURING

    def status(self):
        with self.mutex:
            q, fresh, _ = self.transport.feedback()
            return {
                "state": self.motion.phase,
                "message": self.motion.reason,
                "result": self.motion.result,
                "frame": self.motion.frame,
                "feedback_fresh": fresh,
                "positions": np.asarray(q).reshape(2, 7).tolist()
                if q is not None
                and np.asarray(q).shape == (14,)
                and np.isfinite(q).all()
                else [],
            }

    def health(self):
        with self.mutex:
            try:
                self.transport.check()
                status = self.status()
                ready = (
                    not self.closed
                    and status["feedback_fresh"]
                    and not self._capture_busy()
                    and self.motion.phase not in MOVING | {"release_failed"}
                )
                return {
                    "ready": ready,
                    "detail": "" if ready else "请结束采集并等待双臂反馈",
                }
            except Exception as exc:
                return {"ready": False, "detail": str(exc)}

    def start(self, body):
        with self.mutex:
            if self.closed:
                raise DomainError("回放 Bridge 已关闭", 503)
            owner = body.get("owner")
            if not isinstance(owner, str) or not 1 <= len(owner) <= 128:
                raise DomainError("回放 owner 无效", 400)
            try:
                signature = digest(body)
            except (TypeError, ValueError) as exc:
                raise DomainError("回放请求必须包含有限的 JSON 数值", 400) from exc
            if self.owner == owner:
                if signature != self.request_hash:
                    raise DomainError("同一回放 owner 不能更换轨迹")
                return self.status()
            if self._capture_busy() or self.motion.phase in MOVING:
                raise DomainError("请先结束当前采集或回放")
            if self.motion.phase == "release_failed":
                raise DomainError("回放目标尚未排空，请先恢复采集准备")
            self.transport.check()
            q, fresh, _ = self.transport.feedback()
            if not fresh:
                raise DomainError("尚未获得双臂新鲜反馈", 503)
            try:
                values = np.asarray(body["values"], dtype=float)
                fps, speed, duration = (
                    float(body[k]) for k in ("fps", "speed", "duration")
                )
                if (
                    values.ndim != 2
                    or values.shape[1] != 14
                    or not 1 <= len(values) <= 1_000_000
                    or not np.isfinite(values).all()
                ):
                    raise ValueError("回放需要非空、有限的 N×14 轨迹")
                if (
                    not all(math.isfinite(x) for x in (fps, speed, duration))
                    or not 1 <= fps <= 240
                    or not 0 < speed <= 1
                    or not 0 < duration <= 30
                ):
                    raise ValueError("回放时序参数无效")
                self.validators.validate_motion(values, fps, speed, self.profile)
                self.validators.validate_feedback_position(q, self.profile)
                tolerance = [
                    a["start_tolerance"] for a in self.profile["arms"] for _ in range(7)
                ]
                self.motion.begin(
                    values, q, fps, speed, duration, tolerance, self.clock()
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise DomainError(str(exc), 400) from exc
            self.owner, self.request_hash = owner, signature
            return self.status()

    def _require_owner(self, owner):
        if not self.owner or owner != self.owner:
            raise DomainError("回放会话不匹配", 409)

    def heartbeat(self, owner):
        with self.mutex:
            self._require_owner(owner)
            self.motion.last_heartbeat = self.clock()
            return self.status()

    def stop(self, owner):
        with self.mutex:
            self._require_owner(owner)
            q, fresh, _ = self.transport.feedback()
            # A late stop after returning to manual preparation must not resume
            # position publication and override the operator's physical buttons.
            if self.motion.phase != "idle" and self.motion.phase != "release_failed":
                self.motion.stop("用户停止回放，保持当前位置", q if fresh else None)
            return self.status()

    def tick(self):
        with self.mutex:
            if (
                self.closed
                or self.motion.target is None
                and self.motion.phase not in MOVING
            ):
                return
            try:
                if self._capture_busy():
                    raise DomainError("采集已接管，停止回放目标发布")
                self.transport.check()
                q, fresh, seq = self.transport.feedback()
                target = self.motion.tick(self.clock(), q, fresh, seq)
                if target is not None and fresh:
                    self.transport.publish(target)
            except Exception as exc:
                self.motion.stop(str(exc), failed=True)

    def release_for_capture(self):
        with self.mutex:
            self.check_capture_allowed()
            if self.motion.phase == "idle":
                return
            self.motion.target = None
            self.motion.phase = "release_failed"
            self.motion.reason = "正在排空回放位置目标"
            if not self.transport.drain():
                raise DomainError("回放目标未确认排空，尚不能开始采集")
            self.motion = self.motion_factory()
            self.motion.reason = "回放目标已排空，等待实体按键准备采集"
            # No gravity/home/disable command: the operator chooses the mode.

    def check_capture_allowed(self):
        with self.mutex:
            if self.closed or self.motion.phase in MOVING:
                raise DomainError("请先结束当前回放")

    def close(self):
        with self.mutex:
            self.closed = True
            self.motion.target = None
        self.transport.close()


class RosReplayTransport:
    def __init__(self, source, config, profile):
        from arm_control.msg import JointControl
        from rclpy.duration import Duration
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.node import Node

        self.source, self.Duration, self.message = source, Duration, JointControl
        self.closed, self.error = False, None
        self.node = Node("bodypipeline_arx_replay")
        self.nodes = config.get("controller_nodes", ["arm_master_l", "arm_master_r"])
        self.topics = config.get(
            "command_topics",
            [
                "/bodypipeline/replay/left/joint_control",
                "/bodypipeline/replay/right/joint_control",
            ],
        )
        self.feedback_topics = [
            profile["arm_topics"][side] for side in ("left", "right")
        ]
        if (
            len(self.nodes) != 2
            or len(self.topics) != 2
            or len(set(self.topics)) != 2
            or set(self.topics) & set(self.feedback_topics)
        ):
            self.node.destroy_node()
            raise DomainError("ARX 回放需独立的双臂命令话题与反馈话题", 400)
        self.publishers = [
            self.node.create_publisher(JointControl, t, 10) for t in self.topics
        ]
        self.executor = SingleThreadedExecutor()
        self.executor.add_node(self.node)
        self.thread = None

    def check(self):
        if self.error or self.closed:
            raise DomainError(self.error or "ROS 回放连接已关闭", 503)
        for node, command, feedback in zip(
            self.nodes, self.topics, self.feedback_topics
        ):
            targets = self.node.get_subscriptions_info_by_topic(command)
            reports = self.node.get_publishers_info_by_topic(feedback)
            senders = self.node.get_publishers_info_by_topic(command)
            if (
                len(targets) != 1
                or targets[0].node_name != node
                or targets[0].topic_type != "arm_control/msg/JointControl"
            ):
                raise DomainError(
                    f"{node} 缺少专用关节回放入口；请部署 ARX 共用驱动补丁与配置", 503
                )
            if (
                len(reports) != 1
                or reports[0].node_name != node
                or reports[0].topic_type != "arx5_arm_msg/msg/RobotStatus"
            ):
                raise DomainError(f"{feedback} 不是预期采集控制器的唯一反馈", 503)
            if len(senders) != 1 or senders[0].node_name != self.node.get_name():
                raise DomainError(f"{command} 存在其他目标发布者", 409)

    def feedback(self):
        with self.source.lock:
            rows = [
                self.source.receipts[name][-1] if self.source.receipts[name] else None
                for name in ("left_arm", "right_arm")
            ]
            if any(row is None for row in rows):
                return None, False, (0, 0)
            q = np.asarray([v for msg, _ in rows for v in msg.joint_pos], dtype=float)
            fresh = (
                not self.error
                and q.shape == (14,)
                and np.isfinite(q).all()
                and all(time.monotonic() - at < 0.5 for _, at in rows)
            )
            # Distinguish actual feedback callbacks from repeated reads of the
            # same sample, as the installed tool's subscription counters do.
            seq = tuple(at for _, at in rows)
            return q, bool(fresh), seq

    def publish(self, values):
        for i, pub in enumerate(self.publishers):
            msg = self.message()
            # JointControl has eight slots; the installed callback reads J1..J6
            # and gripper from the first seven, leaving the last slot unused.
            msg.joint_pos = np.asarray(
                values[i * 7 : i * 7 + 7], dtype=float
            ).tolist() + [0.0]
            pub.publish(msg)

    def drain(self):
        # DDS acknowledgements confirm delivery, not a physical mode change.
        # The operator still performs HOME then gravity compensation afterward.
        return all(
            pub.wait_for_all_acked(self.Duration(seconds=1)) for pub in self.publishers
        )

    def start(self, callback):
        self.node.create_timer(0.01, callback)

        def spin():
            try:
                self.executor.spin()
            except Exception as exc:
                if not self.closed:
                    self.error = str(exc)

        self.thread = threading.Thread(target=spin, daemon=True)
        self.thread.start()

    def close(self):
        self.closed = True
        self.executor.shutdown(timeout_sec=5)
        if self.thread:
            self.thread.join(timeout=5)
        self.node.destroy_node()


def create_replay_control(settings, recorder, source):
    config = settings.replay
    profile_path = config.get("hardware_profile")
    if not profile_path:
        raise DomainError("共用驱动回放需要配置 replay.hardware_profile", 400)
    profile = json.loads(Path(profile_path).read_text())
    if [arm.get("can_port") for arm in profile.get("arms", [])] != ["can1", "can3"]:
        raise DomainError("回放硬件配置必须对应现有左 can1、右 can3 双臂", 400)
    motion = load_tool(config["tool_root"], "ros_motion")
    validators = load_tool(config["tool_root"], "core")
    transport = RosReplayTransport(source, config, settings.profile())
    try:
        control = ArxReplayControl(
            recorder, transport, profile, motion.Motion, validators
        )
        transport.start(control.tick)
        return control
    except Exception:
        transport.close()
        raise
