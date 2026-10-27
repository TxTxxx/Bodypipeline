"""Arm and adopt recorder sessions started by the teleoperation controller."""

from __future__ import annotations

from pathlib import Path

from pipeline.shared.core import DomainError, now

from .service import BUSY, CaptureService


class ControllerCaptureCoordinator:
    """Bridge a controller-owned recorder session into the generic Client flow.

    The teleoperation controller starts and stops Apex directly. This class only
    arms one explicit Client draft and adopts the matching recorder session. It
    never calls start/stop on Apex and never requests upload.
    """

    def __init__(self, service: CaptureService):
        self.service = service

    @property
    def enabled(self) -> bool:
        return self.service.settings.controller_trigger.get("enabled") is True

    def _active(self) -> dict | None:
        return next(
            (
                row
                for row in self.service.store.all("capture")
                if row.get("capture_trigger") == "controller"
                and row["state"] in {"STARTING", "RECORDING", "STOPPING"}
            ),
            None,
        )

    def arm(self, data: dict) -> dict:
        if not self.enabled:
            raise DomainError("当前部署未启用遥操作手柄采集", 409)
        with self.service.lock:
            health = self.service.adapter.health()
            if not health.get("ready"):
                raise DomainError(f"采集前检查失败：{health}")
            if any(row["state"] in BUSY for row in self.service.store.all("capture")):
                raise DomainError("机器人已有采集或状态未知的会话")
            row = self.service.create(data)
            if row["state"] != "DRAFT":
                raise DomainError("该请求已创建过会话，请使用新的准备请求")
            for other in self.service.store.all("capture"):
                if (
                    other["id"] != row["id"]
                    and other["state"] == "DRAFT"
                    and other.get("controller_armed")
                ):
                    other["controller_armed"] = False
                    self.service.save(other)
            row.update(
                state="DRAFT",
                capture_trigger="controller",
                controller_armed=True,
                controller_armed_at=now(),
                error=None,
            )
            return self.service.save(row)

    def disarm(self, capture_id: str) -> dict:
        with self.service.lock:
            row = self.service.store.get("capture", capture_id)
            if row["state"] != "DRAFT" or not row.get("controller_armed"):
                raise DomainError("该会话没有等待手柄启动")
            row.update(controller_armed=False)
            return self.service.save(row)

    def tick(self) -> bool:
        if not self.enabled:
            return False
        status_fn = getattr(self.service.adapter, "controller_status", None)
        if not callable(status_fn):
            return False
        try:
            status = status_fn()
        except DomainError:
            # Recorder availability is already surfaced by /api/health. A
            # transient status error must not mutate an armed or active row.
            return False

        with self.service.lock:
            active = self._active()
            if active is None:
                armed = next(
                    (
                        row
                        for row in sorted(
                            self.service.store.all("capture"),
                            key=lambda value: value.get("controller_armed_at", 0),
                            reverse=True,
                        )
                        if row["state"] == "DRAFT" and row.get("controller_armed")
                    ),
                    None,
                )
                if (
                    armed is None
                    or not status.get("controller_owned")
                    or status.get("phase") != "recording"
                    or status.get("data_state") != "recording"
                    or status.get("video_state") != "recording"
                ):
                    return False
                if any(
                    row["id"] != armed["id"] and row["state"] in BUSY
                    for row in self.service.store.all("capture")
                ):
                    return False
                raw = Path(armed["raw_root"])
                if raw.resolve() != raw or (raw / "episode0").exists():
                    armed.update(
                        state="RECOVERY_REQUIRED",
                        controller_armed=False,
                        error="手柄录制目标目录不安全或已存在",
                    )
                    self.service.save(armed)
                    return True
                raw.mkdir(parents=True, exist_ok=True)
                armed.update(
                    state="RECORDING",
                    controller_armed=False,
                    controller_session_id=status.get("session_id"),
                    controller_request_id=status.get("request_id"),
                    started_at=status.get("started_at") or now(),
                    error=None,
                )
                self.service.save(armed)
                return True

            if status.get("session_id") != active.get("controller_session_id"):
                if status.get("phase") in {"idle", "saved", "cancelled", "error"}:
                    active.update(
                        state="RECOVERY_REQUIRED",
                        error="手柄录制会话发生变化，需人工确认",
                    )
                    self.service.save(active)
                    return True
                return False

            phase = status.get("phase")
            if phase == "recording":
                if active["state"] != "RECORDING":
                    active["state"] = "RECORDING"
                    self.service.save(active)
                return False
            if phase == "saving":
                if active["state"] != "STOPPING":
                    active["state"] = "STOPPING"
                    self.service.save(active)
                    return True
                return False
            if phase == "saved":
                try:
                    if (
                        status.get("data_state") != "closed"
                        or status.get("video_state") != "closed"
                    ):
                        return False
                    imported = self.service.adapter.import_controller_recording(
                        status, Path(active["raw_root"]), active["id"]
                    )
                    active.update(
                        state="REVIEW",
                        stopped_at=status.get("ended_at") or now(),
                        controller_import=imported,
                        error=None,
                    )
                except Exception as exc:
                    active.update(
                        state="RECOVERY_REQUIRED",
                        error=f"手柄录制已结束，但导入 Client 失败：{exc}",
                    )
                self.service.save(active)
                if active["state"] == "REVIEW":
                    self.service.request_keep(active["id"])
                return True
            if phase in {"cancelled", "error", "failed"}:
                active.update(
                    stopped_at=status.get("ended_at") or now(),
                    controller_cancelled=phase == "cancelled",
                    error=None
                    if phase == "cancelled"
                    else status.get("error_message") or "手柄录制失败",
                )
                if phase == "cancelled":
                    self.service._discard(active)
                else:
                    active.update(state="RECOVERY_REQUIRED", capture_failed=True)
                    self.service.save(active)
                return True
            return False
