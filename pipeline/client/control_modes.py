"""Exclusive capture/inference mode coordination over the common adapter API."""
from __future__ import annotations

import threading
import time

from pipeline.shared.core import DomainError

from .service import BUSY


class ControlModeService:
    def __init__(self, settings, capture, inference, replay):
        self.enabled = bool(settings.capture.get("switch_control_modes"))
        self.capture, self.inference, self.replay = capture, inference, replay
        self.lock = threading.RLock()

    def status(self):
        if not self.enabled:
            return {"mode": "capture", "supported_modes": ["capture"], "switching": False}
        try:
            return self.capture.adapter.control_state()
        except DomainError as exc:
            return {"mode": None, "supported_modes": ["capture", "inference"],
                    "switching": False, "error": exc.message}

    def require(self, mode):
        if not self.enabled:
            return
        state = self.status()
        if state.get("switching") or state.get("mode") != mode:
            raise DomainError("请先切换到" + ("采集模式" if mode == "capture" else "推理模式"), 409)

    def quiesce_inference(self):
        """Wait for the executor acknowledgement, not just the dashboard flag."""
        url = str(self.inference.urls["inference"]).rstrip("/") + "/control"
        reply = self.inference.call("POST", url, {"action": "robot_quiesce"})
        revision = reply.get("control_revision")
        if not reply.get("ok") or not isinstance(revision, int):
            raise DomainError("Enactive 未提供执行暂停确认，不能交接机械臂控制权", 409)
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            state = self.inference._state("inference")
            result = state.get("last_control_result") or {}
            if result.get("action") == "robot_quiesce" and result.get("revision") == revision:
                if result.get("ok") and not state.get("running") and not state.get("control_pending"):
                    return
                raise DomainError("Enactive 动作执行未能安全暂停", 409)
            time.sleep(.1)
        raise DomainError("等待 Enactive 动作执行暂停超时，未切换控制权", 504)

    def switch(self, mode):
        if mode not in {"capture", "inference"}:
            raise DomainError("未知控制模式", 400)
        if not self.enabled:
            raise DomainError("当前部署未启用控制模式切换", 409)
        with self.lock, self.capture.lock:
            current = self.status()
            if current.get("mode") == mode and not current.get("switching") and not current.get("error"):
                return {**current, "message": "当前已处于" + ("采集模式" if mode == "capture" else "推理模式")}
            if not current.get("hot_switch_supported", False):
                raise DomainError("已阻止通过重启电机驱动切换模式，避免控制中断引起抖动。当前驱动需要升级为连续控制版本。", 409)
            if self.replay.row["state"] in {"PREPARING", "RUNNING", "STOPPING", "RECOVERY_REQUIRED"}:
                raise DomainError("请先结束当前回放", 409)
            # A boolean pause does not prove the executor finished an action chunk.
            try:
                native = self.inference._state("inference")
            except DomainError as exc:
                if exc.code != 503:
                    raise
                native = None
            if native is not None:
                if native.get("control_pending"):
                    raise DomainError("机械臂正在预备或复位，请等待动作完成再切换模式", 409)
                self.quiesce_inference()
            self.capture.sync_continuous()
            rows = [row for row in self.capture.store.all("capture") if row["state"] in BUSY]
            for row in rows:
                stopped = self.capture.stop(row["id"])
                deadline = time.monotonic() + 60
                while stopped["state"] == "STOPPING" and time.monotonic() < deadline:
                    self.capture.sync_continuous()
                    self.capture.sync_recording_stops()
                    stopped = self.capture.store.get("capture", row["id"])
                    if stopped["state"] == "STOPPING":
                        time.sleep(.1)
                if stopped["state"] not in {"SAVED", "DISCARDED"}:
                    raise DomainError("当前 Episode 尚未确认保存，暂不切换驱动", 409)
            self.inference.hardware_enabled = False
            self.inference.preparation_required = True
            self.inference.preparation_requested = False
            result = self.capture.adapter.set_control_mode(mode)
            if result.get("mode") != mode or result.get("switching"):
                raise DomainError("驱动尚未确认切换完成", 503)
            self.capture.sync_continuous()
            return {**result, "message": ("已切换到采集模式" if mode == "capture" else "已切换到推理模式")}
