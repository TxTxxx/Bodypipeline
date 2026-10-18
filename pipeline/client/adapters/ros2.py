from __future__ import annotations

import httpx

from pipeline.shared.core import DomainError


class Ros2Adapter:
    """ROS stays in its own Python environment. Requests never invoke a shell."""

    def __init__(self, settings):
        self.settings = settings
        self.http = httpx.Client(
            base_url=settings.bridge_url,
            headers={"Authorization": f"Bearer {settings.bridge_token}"},
            timeout=45,
            trust_env=False,
        )

    def call(self, method, path, **kwargs):
        try:
            response = self.http.request(method, path, **kwargs)
            if response.status_code >= 400:
                raise DomainError(
                    response.json().get("detail", "ROS bridge error"),
                    response.status_code,
                )
            return response.json()
        except httpx.HTTPError as exc:
            raise DomainError(
                f"ROS Bridge 通信失败；命令结果可能未知，不自动重试：{type(exc).__name__}",
                503,
            ) from exc

    def health(self):
        return self.call("GET", "/health")

    def start(self, session_id, raw_root, instruction):
        return self.call(
            "POST",
            "/start",
            json={
                "session_id": session_id,
                "raw_root": str(raw_root),
                "instruction": instruction,
            },
        )

    def stop(self, session_id):
        return self.call("POST", "/stop", json={"session_id": session_id})

    def status(self):
        return self.call("GET", "/status")

    def frame(self, camera_id):
        try:
            r = self.http.get(f"/frames/{camera_id}", timeout=3)
            return r.content if r.status_code == 200 else None
        except httpx.HTTPError:
            return None

    def close(self):
        # Never send implicit stop/start to hardware on API shutdown.
        self.http.close()
