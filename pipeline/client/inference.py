"""Native Enactive dashboard control; robot/model runtimes remain separate.

Uses Enactive's real /state, /instruction and /control contracts. RLT labels
are bound to the reported episode_id. Checkpoint paths are verified against
policy-server metadata instead of silently ignoring the requested weights.
"""
from __future__ import annotations

import threading
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from pipeline.shared.core import DomainError

from .service import BUSY


class InferenceService:
    def __init__(self, settings, capture, replay):
        self.capture, self.replay = capture, replay
        self.config = settings.capture.get("inference", {})
        self.urls = {
            "inference": self.config.get("dashboard_url", "http://127.0.0.1:8080"),
            "stage2": self.config.get("stage2_dashboard_url", ""),
            "dagger": self.config.get("dagger_dashboard_url", ""),
        }
        for value in self.urls.values():
            if value and urlsplit(value).scheme not in {"http", "https"}:
                raise ValueError("Enactive dashboard 地址必须为 HTTP(S)")
        self.http = httpx.Client(timeout=2, trust_env=False)
        self.mode = "inference"
        self.lock = threading.Lock()
        self.hardware_enabled = False
        self.preparation_required = True
        self.preparation_requested = False

    def call(self, method, url, body=None):
        try:
            response = self.http.request(method, url, json=body, timeout=2 if method == "GET" else 20)
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise DomainError("Enactive 执行服务未连接，请检查模型服务和机器人客户端。", 503) from exc
        if not isinstance(result, dict):
            raise DomainError("Enactive 返回了无效状态", 502)
        if response.status_code >= 400 or result.get("ok") is False:
            raise DomainError(str(result.get("error") or result.get("detail") or "Enactive 拒绝本次操作"),
                              response.status_code if response.status_code >= 400 else 409)
        return result

    def _state(self, mode):
        url = str(self.urls.get(mode, "")).rstrip("/")
        if not url:
            raise DomainError("尚未配置 Enactive " + ("RLT Stage 2" if mode == "stage2" else "DAgger") + " 运行服务。", 503)
        return self.call("GET", url + "/state")

    def _status(self, states, errors, policy=None):
        policy = policy or {}
        current = states.get(self.mode, {})
        training = states.get("stage2", {}).get("rl_training", {})
        if (self.preparation_requested and not current.get("control_pending")
                and current.get("robot_enabled")):
            self.preparation_required = False
            self.preparation_requested = False
        capabilities = {
            "inference": bool(states.get("inference")) and bool(policy) and not states["inference"].get("observer_mode", False),
            "stage2": bool(training) and not states.get("stage2", {}).get("observer_mode", False),
            "dagger": bool(states.get("dagger")) and not states["dagger"].get("observer_mode", False),
        }
        episode = training.get("episode_id")
        message = (current.get("server_error") or errors.get(self.mode)
                   or current.get("message") or training.get("message"))
        return {
            "provider": "enactive", "connected": bool(states),
            "preparing": bool(current.get("control_pending")),
            "state": "RUNNING" if current.get("running") else "PREPARING" if current.get("control_pending") else "READY" if capabilities.get(self.mode) else "UNAVAILABLE",
            "mode": self.mode, "capabilities": capabilities,
            "inference_active": self.mode in {"inference", "dagger"} and bool(current.get("running")),
            "stage2_active": self.mode == "stage2" and bool(training),
            "dagger_active": self.mode == "dagger" and bool(current.get("running")),
            "dagger": states.get("dagger", {}).get("dagger", {}),
            "dagger_controls": states.get("dagger", {}).get("dagger_controls", []),
            "session_id": str(episode) if episode is not None else current.get("episode_id"),
            "feedback_allowed": bool(training.get("running") or training.get("awaiting_label")),
            "feedback_recorded": training.get("label") is not None,
            "last_feedback": "failed" if training.get("label") == "failure" else training.get("label"),
            "checkpoint_path": current.get("checkpoint_path") or current.get("checkpoint") or policy.get("checkpoint"),
            "prompt": current.get("instruction") or current.get("episode_instruction") or "",
            "model_family": policy.get("model_family"),
            "policy_connected": bool(policy),
            "robot_enable_supported": bool(current.get("robot_enable_supported")),
            "robot_enabled": bool(current.get("robot_enabled")) and not self.preparation_required,
            "hardware_enabled": self.hardware_enabled,
            "hardware_enable_required": bool(self.config.get("prepare_hardware")),
            "episode_reset_supported": bool(current.get("episode_reset_supported")),
            "run_status": current.get("run_status"),
            "execution_phase": current.get("execution_phase"),
            "message": message or "Enactive 已连接。",
            "feature_messages": errors,
        }

    def status(self):
        states, errors = {}, {}
        # Only probe configured endpoints. Nothing is launched by opening the page.
        for mode, url in self.urls.items():
            if not url:
                errors[mode] = "尚未配置 Enactive " + ("RLT Stage 2" if mode == "stage2" else "DAgger") + " 运行服务。"
                continue
            try:
                states[mode] = self._state(mode)
            except DomainError as exc:
                errors[mode] = exc.message
        policy = {}
        if states.get("inference"):
            try:
                policy = self.call("GET", str(self.config.get("policy_url", "http://127.0.0.1:9998")).rstrip("/") + "/health")
            except DomainError:
                errors["inference"] = "Enactive 模型服务未连接，请检查当前权重是否已加载。"
        if self.hardware_enabled and self.config.get("prepare_hardware"):
            try:
                healthy = self.capture.adapter.health().get("ready", False)
            except DomainError:
                healthy = False
            if not healthy:
                self.hardware_enabled = False
                self.preparation_required = True
                self.preparation_requested = False
                errors["inference"] = "设备反馈离线，请点击机械臂使能重新启动和恢复。"
        result = self._status(states, errors, policy)
        native_dagger = states.get("dagger", {})
        if "dagger_active" in native_dagger:
            result["dagger_active"] = bool(native_dagger["dagger_active"])
        if getattr(self, "control_modes", None) and self.control_modes.enabled:
            control = self.control_modes.status()
            result["control_mode"] = control.get("mode")
            result["mode_switching_enabled"] = True
            if control.get("mode") != "inference":
                result.update(hardware_enabled=False, robot_enabled=False, inference_active=False)
        return result

    def _verify_checkpoint(self, mode, state, requested):
        checkpoint = state.get("checkpoint_path") or state.get("checkpoint")
        if not checkpoint:
            policy = self.config.get("policy_url", "http://127.0.0.1:9998")
            runtime = self.call("GET", str(policy).rstrip("/") + "/")
            checkpoint = runtime.get("checkpoint")
        if not checkpoint:
            raise DomainError("Enactive 未报告当前权重路径，无法核对选定模型。", 409)
        if Path(str(checkpoint)).expanduser() != Path(requested).expanduser():
            raise DomainError(f"Enactive 当前加载的权重为 {checkpoint}，与输入路径不同；请在 Enactive 切换模型后重试。", 409)

    def action(self, operation, body):
        if getattr(self, "control_modes", None) and self.control_modes.enabled:
            self.control_modes.require("inference")
        with self.lock:
            mode = "inference" if operation == "start" else "stage2" if operation == "feedback" else operation
            with self.capture.lock:
                if operation != "feedback":
                    self.capture.sync_continuous()
                    if any(row["state"] in BUSY for row in self.capture.store.all("capture")):
                        raise DomainError("请先结束当前采集，再切换模型控制模式", 409)
                    if self.replay.row["state"] in {"PREPARING", "RUNNING", "STOPPING", "RECOVERY_REQUIRED"}:
                        raise DomainError("请先结束当前回放", 409)
                state = self._state(mode)
                url = str(self.urls[mode]).rstrip("/")
                if state.get("observer_mode"):
                    raise DomainError("Enactive 当前为只观测模式，不能执行控制操作。", 409)
                if operation == "feedback":
                    training = state.get("rl_training", {})
                    if str(training.get("episode_id")) != body["session_id"]:
                        raise DomainError("回合已经变化，请刷新后为当前回合标记结果。", 409)
                    if not (training.get("running") or training.get("awaiting_label")):
                        raise DomainError("当前没有等待结果的 RLT 回合。", 409)
                    self.call("POST", url + "/control", {
                        "action": "failure" if body["result"] == "failed" else "success",
                        "episode_id": training["episode_id"], "request_id": body["request_id"],
                    })
                else:
                    self._verify_checkpoint(mode, state, body["checkpoint_path"])
                    if operation == "start" and state.get("dagger_active"):
                        raise DomainError("请先结束 DAgger，或使用 DAgger 的接回策略按钮。", 409)
                    if state.get("control_pending") or (state.get("robot_enable_supported") and (not state.get("robot_enabled") or self.preparation_required)):
                        raise DomainError("请先点击机械臂预备，等待到达推理准备位姿。", 409)
                    if operation == "stage2":
                        training = state.get("rl_training", {})
                        if not training:
                            raise DomainError("此 Enactive 服务不是 RLT Stage 2 运行服务。", 409)
                        if state.get("instruction", "").strip() != body["prompt"].strip():
                            raise DomainError("Stage 2 的 prompt 必须与训练配置中的固定任务指令一致。", 409)
                        self.call("POST", url + "/control", {"action": "start", "episode_id": training["episode_id"],
                                                              "request_id": body["request_id"]})
                    elif operation == "dagger" and state.get("dagger_controls"):
                        self.call("POST", url + "/control", {"action": "dagger:start",
                                  "instruction": body["prompt"], "request_id": body["request_id"]})
                    else:
                        # /instruction starts the native Enactive live loop. Do
                        # not issue a second RUN that could repeat a transition.
                        self.call("POST", url + "/instruction", {"instruction": body["prompt"]})
                self.mode = mode
        result = self.status()
        if operation == "feedback":
            result.update(last_feedback=body["result"], feedback_recorded=True, message="当前回合结果已提交到 Enactive。")
        return result

    def dagger_control(self, body):
        """Forward the common correction contract; robot details stay in the runtime."""
        if getattr(self, "control_modes", None) and self.control_modes.enabled:
            self.control_modes.require("inference")
        with self.lock, self.capture.lock:
            self.capture.sync_continuous()
            if any(row["state"] in BUSY for row in self.capture.store.all("capture")):
                raise DomainError("请先结束当前采集", 409)
            if self.replay.row["state"] in {"PREPARING", "RUNNING", "STOPPING", "RECOVERY_REQUIRED"}:
                raise DomainError("请先结束当前回放", 409)
            state = self._state("dagger")
            if state.get("observer_mode"):
                raise DomainError("当前为只观测模式", 409)
            if state.get("control_pending"):
                raise DomainError("正在处理上一项操作，请等待完成", 409)
            if body["action"] not in state.get("dagger_controls", []):
                raise DomainError("当前执行服务不支持此纠偏操作", 409)
            if not state.get("dagger_active"):
                raise DomainError("请先进入 DAgger", 409)
            if body["session_id"] != state.get("dagger", {}).get("session_id"):
                raise DomainError("DAgger 会话已经变化，请刷新页面", 409)
            url = str(self.urls["dagger"]).rstrip("/")
            self.call("POST", url + "/control", {"action": "dagger:" + body["action"],
                      "session_id": body["session_id"], "request_id": body["request_id"]})
            self.mode = "inference" if body["action"] == "finish" else "dagger"
        return self.status()

    def control(self, operation):
        """Hardware enable, policy preparation and folded reset are separate."""
        if operation not in {"enable", "prepare", "reset", "pause"}:
            raise DomainError("未知推理控制操作", 400)
        hardware_result = None
        if getattr(self, "control_modes", None) and self.control_modes.enabled and operation != "pause":
            self.control_modes.require("inference")
        with self.lock:
            with self.capture.lock:
                # Hardware startup must also work while the model runtime is offline.
                try:
                    state = self._state("inference")
                except DomainError:
                    if operation != "enable":
                        raise
                    state = None
                if state and state.get("observer_mode"):
                    raise DomainError("Enactive 当前为只观测模式，不能执行控制操作。", 409)
                if state and state.get("dagger_active") and operation != "pause":
                    raise DomainError("请先结束 DAgger，再进行使能、预备或复位。", 409)
                if operation != "pause":
                    self.capture.sync_continuous()
                    if any(row["state"] in BUSY for row in self.capture.store.all("capture")):
                        raise DomainError("请先结束当前采集，再切换模型控制模式", 409)
                    if self.replay.row["state"] in {"PREPARING", "RUNNING", "STOPPING", "RECOVERY_REQUIRED"}:
                        raise DomainError("请先结束当前回放", 409)
                    if state and state.get("control_pending"):
                        raise DomainError("Enactive 正在执行准备或复位，请等待完成。", 409)
                url = str(self.urls["inference"]).rstrip("/")
                coordinated = getattr(self, "control_modes", None) and self.control_modes.enabled
                if state is not None and coordinated and operation in {"enable", "prepare", "reset"}:
                    self.control_modes.quiesce_inference()
                if operation == "enable":
                    if state is not None and not coordinated:
                        self.call("POST", url + "/control", {"action": "pause"})
                    self.preparation_required = True
                    self.preparation_requested = False
                    self.hardware_enabled = False
                    hardware_result = self.capture.enable_arm()
                    self.hardware_enabled = bool(hardware_result.get("enabled"))
                else:
                    if operation == "prepare":
                        if not state.get("robot_enable_supported"):
                            raise DomainError("当前 Enactive 执行客户端不支持机械臂预备。", 409)
                        if self.config.get("prepare_hardware") and not self.hardware_enabled:
                            raise DomainError("请先点击机械臂使能，启动并恢复设备，再进行机械臂预备。", 409)
                        if not coordinated:
                            self.call("POST", url + "/control", {"action": "pause"})
                        if coordinated:
                            self.capture.adapter.remember_fold()
                        action = "robot_enable"
                    elif operation == "reset":
                        if getattr(self, "control_modes", None) and self.control_modes.enabled:
                            self.preparation_required = True
                            self.preparation_requested = False
                            return {**self.status(), **self.capture.reset_arm(), "robot_enabled": False}
                        if not state.get("episode_reset_supported"):
                            raise DomainError("当前 Enactive 执行客户端不支持复位。", 409)
                        action = "episode_reset"
                    else:
                        action = "pause"
                    if operation in {"prepare", "reset"}:
                        self.preparation_required = True
                        self.preparation_requested = False
                    self.call("POST", url + "/control", {"action": action})
                    if operation == "prepare":
                        self.preparation_requested = True
                self.mode = "inference"
        result = self.status()
        if hardware_result is not None:
            result.update(hardware_enabled=bool(hardware_result.get("enabled")),
                          robot_enabled=False, hardware=hardware_result,
                          message="CAN、相机和双臂控制器已就绪。保持当前姿态；点击机械臂预备后进入推理准备位姿。")
        return result

    def close(self):
        self.http.close()
