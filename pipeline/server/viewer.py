from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)

from pipeline.shared.core import DomainError, Store, executable_path

from .datasets import dataset_for_job


class ViewerManager:
    """One isolated, unauthenticated viewer at a time for the single-server MVP."""

    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.process = None
        self.log = None
        self.lock = threading.RLock()

    def recover(self):
        store = self.store
        try:
            old = store.get("viewer", "active")
            if old["state"] in {"STARTING", "READY"}:
                old.update(
                    state="INTERRUPTED", error="Server 重启，检查旧进程与端口后再启动"
                )
                store.put("viewer", "active", old)
        except DomainError as exc:
            if exc.code != 404:
                raise

    def start(self, job):
        with self.lock:
            self.poll()
            if self.process and self.process.poll() is None:
                current = self.store.get("viewer", "active")
                if current["job_id"] != job["id"]:
                    raise DomainError("已有另一个数据集的预览会话，请先关闭")
                return current
            data = dataset_for_job(self.settings, job)
            executable = executable_path(self.settings.letools_bin)
            if not executable:
                raise DomainError("未安装 letools / Visualizer 依赖")
            for port in (self.settings.viewer_ui_port, self.settings.viewer_data_port):
                with socket.socket() as sock:
                    try:
                        sock.bind(("127.0.0.1", port))
                    except OSError:
                        raise DomainError(
                            f"预览内部端口 {port} 已占用，不自动终止其他进程"
                        )
            log_path = self.settings.root / "reports/viewer.log"
            self.log = log_path.open("ab")
            command = [
                executable,
                "visualizer",
                "serve",
                str(data),
                "--host",
                "127.0.0.1",
                "--port",
                str(self.settings.viewer_ui_port),
                "--data-port",
                str(self.settings.viewer_data_port),
                "--public-data-url",
                self.settings.viewer_origin.rstrip("/") + "/_data",
                "--no-annotations",
                "--production",
                "--cache-dir",
                str(self.settings.root / "viewer-cache"),
            ]
            if self.settings.viewer_bun:
                command += ["--bun", str(Path(self.settings.viewer_bun).resolve())]
            env = self.settings.tool_environment()
            env["NEXT_TELEMETRY_DISABLED"] = "1"
            import sys

            wrapper = [
                sys.executable,
                "-m",
                "pipeline.server.viewer_bootstrap",
                "--executable",
                executable,
                "--cache",
                str(self.settings.root / "viewer-cache"),
            ]
            if self.settings.viewer_bun:
                wrapper += ["--bun", str(Path(self.settings.viewer_bun).resolve())]
            wrapper += ["--", *command[1:]]
            self.process = subprocess.Popen(
                wrapper,
                stdout=self.log,
                stderr=self.log,
                start_new_session=True,
                env=env,
            )
            state = {
                "state": "STARTING",
                "job_id": job["id"],
                "expires_at": time.time() + self.settings.viewer_ttl,
                "url": self.settings.viewer_origin.rstrip("/") + "/",
                "page_path": "/",
                "log_path": str(log_path),
            }
            self.store.put("viewer", "active", state)
            return state

    def poll(self):
        with self.lock:
            if not self.process:
                return
            row = self.store.get("viewer", "active")
            if self.process.poll() is not None:
                row.update(
                    state="FAILED",
                    error=f"Visualizer 退出：{self.process.returncode}；检查 reports/viewer.log",
                )
                self.store.put("viewer", "active", row)
                self.process = None
                if self.log:
                    self.log.close()
                return
            if time.time() > row["expires_at"]:
                self.stop()
                return
            if row["state"] == "STARTING":
                # LeTools emits a startup JSON with the exact episode URL. It is not '/'.
                from urllib.parse import urlsplit

                text = Path(row["log_path"]).read_text(errors="replace")[-100000:]
                decoder = json.JSONDecoder()
                for i, c in enumerate(text):
                    if c != "{":
                        continue
                    try:
                        obj, _ = decoder.raw_decode(text[i:])
                        if isinstance(obj, dict):
                            for key in ("url", "page_url"):
                                value = obj.get(key)
                                if (
                                    isinstance(value, str)
                                    and urlsplit(value).port
                                    == self.settings.viewer_ui_port
                                ):
                                    parsed = urlsplit(value)
                                    row["page_path"] = parsed.path + (
                                        "?" + parsed.query if parsed.query else ""
                                    )
                    except (ValueError, TypeError):
                        pass
                try:
                    response = httpx.get(
                        f"http://127.0.0.1:{self.settings.viewer_ui_port}"
                        + row["page_path"],
                        timeout=1,
                        trust_env=False,
                    )
                    if response.status_code < 400 and row["page_path"] != "/":
                        row["state"] = "READY"
                except httpx.HTTPError:
                    pass
                self.store.put("viewer", "active", row)

    def stop(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(10)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait()
            self.process = None
            if self.log:
                self.log.close()
                self.log = None
            self.store.put("viewer", "active", {"state": "STOPPED"})
            return {"state": "STOPPED"}


def create_gateway(settings):
    settings.role = "viewer"
    store = Store(settings.root / "state", legacy_name="server.sqlite")
    app = FastAPI(title="LeRobot Viewer Gateway")

    @app.exception_handler(DomainError)
    async def domain_error(_request, exc):
        return JSONResponse({"detail": exc.message}, status_code=exc.code)

    def active_session():
        try:
            row = store.get("viewer", "active")
        except DomainError as exc:
            if exc.code == 404:
                return None
            raise
        if row.get("state") not in {"STARTING", "READY"}:
            return None
        if (
            not isinstance(row.get("expires_at"), (int, float))
            or row["expires_at"] < time.time()
        ):
            return None
        return row

    @app.api_route("/{path:path}", methods=["GET", "HEAD"])
    async def proxy(request: Request, path: str):
        row = active_session()
        if row is None:
            return JSONResponse(
                {"detail": "预览尚未就绪或已过期，请在管理台重新打开"},
                status_code=503,
            )
        if row["state"] == "STARTING" and path == "":
            return HTMLResponse(
                """<!doctype html><html lang="zh"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="2">
<title>正在启动数据预览</title>
<body style="margin:0;background:#f7f7f8;color:#202124;font:15px system-ui,sans-serif">
<main style="max-width:620px;margin:15vh auto;padding:32px;background:#fff;border:1px solid #dedee3;border-radius:12px">
<h1 style="font-size:22px;margin:0 0 12px">正在启动 LeRobot Visualizer</h1>
<p style="line-height:1.7;margin:0">首次启动可能需要安装或准备前端依赖。页面每 2 秒自动刷新，就绪后会直接跳转到当前数据集。</p>
</main></body></html>""",
                headers={"Cache-Control": "no-store"},
            )
        if row["state"] != "READY":
            return JSONResponse({"detail": "预览仍在启动，请稍后刷新"}, status_code=503)
        if path == "":
            target = row.get("page_path")
            if (
                not isinstance(target, str)
                or not target.startswith("/")
                or target.startswith("//")
            ):
                raise DomainError("预览页面地址无效，请关闭后重新启动", 500)
            return RedirectResponse(
                target, status_code=307, headers={"Cache-Control": "no-store"}
            )
        is_data = path == "_data" or path.startswith("_data/")
        port = settings.viewer_data_port if is_data else settings.viewer_ui_port
        target_path = (
            path[len("_data/") :]
            if path.startswith("_data/")
            else ("" if path == "_data" else path)
        )
        url = f"http://127.0.0.1:{port}/" + target_path
        if request.url.query:
            url += "?" + request.url.query
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower()
            in {
                "range",
                "if-range",
                "accept",
                "accept-encoding",
                "if-none-match",
                "rsc",
                "next-router-state-tree",
                "next-router-prefetch",
                "next-url",
            }
        }
        client = httpx.AsyncClient(timeout=120, trust_env=False)
        try:
            response = await client.send(
                client.build_request(request.method, url, headers=headers),
                stream=True,
                follow_redirects=False,
            )
        except Exception:
            await client.aclose()
            raise DomainError("预览后端不可达", 502)

        async def chunks():
            try:
                async for data in response.aiter_raw():
                    yield data
            finally:
                await response.aclose()
                await client.aclose()

        outgoing = {
            k: v
            for k, v in response.headers.items()
            if k.lower()
            in {
                "content-type",
                "content-length",
                "content-range",
                "accept-ranges",
                "content-encoding",
                "etag",
                "vary",
                "location",
                "cache-control",
            }
        }
        return StreamingResponse(
            chunks(), status_code=response.status_code, headers=outgoing
        )

    return app
