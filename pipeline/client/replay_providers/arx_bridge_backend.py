"""Loopback HTTP transport to the persistent capture/replay Bridge."""

from __future__ import annotations

import http.client
import json
import os
from urllib.parse import urlsplit


def bridge_address(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("ARX 回放 Bridge 必须是本机 loopback HTTP 地址")
    return parsed.hostname, parsed.port or 80


class CaptureBridgeBackend:
    def __init__(self, url, token=None, connection_factory=http.client.HTTPConnection):
        self.host, self.port = bridge_address(url)
        self.token = (
            token if token is not None else os.environ.get("PIPELINE_BRIDGE_TOKEN", "")
        )
        if len(self.token) < 24:
            raise ValueError("缺少独立的 PIPELINE_BRIDGE_TOKEN")
        self.connection_factory = connection_factory

    def request(self, path="/status", body=None):
        if path not in {"/health", "/status", "/start", "/heartbeat", "/stop"}:
            raise ValueError("未知回放操作")
        connection = self.connection_factory(self.host, self.port, timeout=10)
        try:
            connection.request(
                "GET" if body is None else "POST",
                "/replay" + path,
                body=None if body is None else json.dumps(body, allow_nan=False),
                headers={
                    "Authorization": "Bearer " + self.token,
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            result = json.loads(response.read())
            if response.status >= 400:
                raise RuntimeError(result.get("detail", "ARX Bridge 回放请求失败"))
            return result
        finally:
            connection.close()

    def status(self):
        return self.request()

    def start_services(self):
        # Same runner protocol as the legacy tool, but never starts a driver.
        health = self.request("/health")
        if not health.get("ready"):
            raise RuntimeError(health.get("detail") or "ARX 共用驱动回放尚未就绪")
