from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .core import DomainError


def install_security(app: FastAPI, settings, device_prefix: str | None = None):
    cookie_name = f"pipeline_{settings.role}"

    def valid_cookie(value):
        try:
            expiry, signature = value.split(".", 1)
            expected = hmac.new(
                settings.token.encode(),
                f"{settings.role}:{expiry}".encode(),
                hashlib.sha256,
            ).hexdigest()
            return int(expiry) > time.time() and hmac.compare_digest(
                signature, expected
            )
        except (ValueError, AttributeError):
            return False

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse({"detail": exc.message}, status_code=exc.code)

    @app.middleware("http")
    async def auth(request: Request, call_next):
        p = request.url.path
        if (
            p in ("/", "/healthz", "/api/login", "/assets/app.js", "/assets/style.css")
            and settings.role != "viewer"
            or p in ("/api/login", "/login")
            and settings.role == "viewer"
        ):
            response = await call_next(request)
        else:
            bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
            is_device = bool(device_prefix and p.startswith(device_prefix))
            key = settings.token
            token_ok = (
                bool(key and bearer and hmac.compare_digest(bearer, key))
                if not is_device
                else False
            )
            if is_device:
                for device_key, identity in settings.device_registry():
                    if (
                        device_key
                        and bearer
                        and hmac.compare_digest(bearer, device_key)
                    ):
                        request.state.device = identity
                        token_ok = True
                        break
            cookie_ok = not is_device and valid_cookie(request.cookies.get(cookie_name))
            if not (token_ok or cookie_ok):
                return JSONResponse(
                    {"detail": "请先登录或提供有效凭据"}, status_code=401
                )
            if (
                cookie_ok
                and not token_ok
                and request.method not in ("GET", "HEAD", "OPTIONS")
            ):
                origin = request.headers.get("origin")
                expected = f"{request.url.scheme}://{request.url.netloc}"
                if origin != expected:
                    return JSONResponse({"detail": "Origin 校验失败"}, status_code=403)
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if settings.role != "viewer":
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' blob: data:; script-src 'self'; style-src 'self'; frame-ancestors 'none'"
            )
        return response

    attempts = {}

    @app.post("/api/login")
    async def login(request: Request):
        # Do not expose credentials in URLs, logs, localStorage, or HTML.
        from json import JSONDecodeError, loads

        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
            raise DomainError("Origin 校验失败", 403)
        address = request.client.host if request.client else "unknown"
        recent = [t for t in attempts.get(address, []) if t > time.time() - 60]
        if len(recent) >= 10:
            raise DomainError("登录尝试过多，请稍后重试", 429)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 4096:
                raise DomainError("请求过大", 413)
        try:
            body = loads(raw)
        except (JSONDecodeError, UnicodeError):
            raise DomainError("无效登录请求", 400)
        if len(attempts) > 10000:
            attempts.clear()
        attempts[address] = recent + [time.time()]
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("token"), str)
            or not hmac.compare_digest(body["token"], settings.token)
        ):
            raise DomainError("密钥不正确", 401)
        attempts.pop(address, None)
        expiry = str(int(time.time()) + 8 * 3600)
        sig = hmac.new(
            settings.token.encode(),
            f"{settings.role}:{expiry}".encode(),
            hashlib.sha256,
        ).hexdigest()
        response = JSONResponse({"ok": True})
        response.set_cookie(
            cookie_name,
            expiry + "." + sig,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            max_age=8 * 3600,
        )
        return response

    @app.post("/api/logout")
    def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(cookie_name)
        return response
