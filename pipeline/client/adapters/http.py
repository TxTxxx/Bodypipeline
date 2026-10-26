"""Transport for the common local capture-service API; no ROS/SDK imports."""

from __future__ import annotations

import httpx

from pipeline.shared.core import DomainError


class HttpCaptureAdapter:
    def __init__(self, settings, *, expected_profile_hash=None):
        self.expected_profile_hash = expected_profile_hash
        self.http = httpx.Client(
            base_url=settings.bridge_url,
            headers={"Authorization": f"Bearer {settings.bridge_token}"},
            timeout=45,
            trust_env=False,
        )

    def call(self, method, path, **kwargs):
        try:
            response = self.http.request(method, path, **kwargs)
            payload = response.json()
            if response.status_code >= 400:
                raise DomainError(
                    payload.get("detail", "Capture bridge error"),
                    response.status_code,
                )
            return payload
        except (httpx.HTTPError, ValueError) as exc:
            raise DomainError(
                f"Capture Bridge 通信失败；命令结果可能未知，不自动重试：{type(exc).__name__}",
                503,
            ) from exc

    def control_state(self):
        return self.call("GET", "/control-mode", timeout=3)

    def set_control_mode(self, mode):
        return self.call("POST", "/control-mode", json={"mode": mode}, timeout=120)

    def remember_fold(self):
        return self.call("POST", "/remember-fold")

    def enable(self):
        return self.call("POST", "/enable", timeout=120)

    def reset(self):
        return self.call("POST", "/reset")

    def health(self):
        result = self.call("GET", "/health")
        if (
            self.expected_profile_hash
            and result.get("profile_hash") != self.expected_profile_hash
        ):
            raise DomainError("Bridge 与 Client 的 Profile 不匹配")
        return result

    def start(self, session_id, raw_root, instruction, *, continuous=False):
        result = self.call(
            "POST",
            "/start",
            json={
                "session_id": session_id,
                "raw_root": str(raw_root),
                "instruction": instruction,
                "continuous": continuous,
            },
        )
        if result.get("started") is not True and not (
            result.get("started") is False and result.get("pending") is True
        ):
            raise DomainError("Recorder 未确认启动；请查询状态，不自动重发")
        return result

    def select_next(self, session_id, raw_root, instruction, *, continuous=False):
        return self.call("POST", "/next", json={
            "session_id": session_id, "raw_root": str(raw_root),
            "instruction": instruction, "continuous": continuous,
        })

    def retry_conversion(self, session_id):
        return self.call("POST", f"/conversions/{session_id}/retry")

    def stop(self, session_id):
        return self.call("POST", "/stop", json={"session_id": session_id})

    def status(self):
        return self.call("GET", "/status")

    def joints(self):
        return self.call("GET", "/joints", timeout=2)

    def frame(self, camera_id):
        try:
            response = self.http.get(f"/frames/{camera_id}", timeout=3)
            return response.content if response.status_code == 200 else None
        except httpx.HTTPError:
            return None

    def close(self):
        self.http.close()
