"""单用户安全边界（M7 协议 §10，docs/protocols/sdk-and-migration.md frozen@1）。

三个纯 ASGI 中间件（不经过 BaseHTTPMiddleware，避免干扰 SSE 流式响应）：

- `SingleUserSecurityMiddleware`：Host allowlist（防 DNS rebinding）、写方法的
  Origin 校验（防不可信网页 CSRF 式触发）、可选 Bearer token（远程部署的显式
  单用户认证）。凭据走 Authorization 头而非 cookie，无经典 CSRF 面。
- `MaintenanceModeMiddleware`：维护窗口内拒绝 /api/* 的写方法（503
  MAINTENANCE_MODE），/api/v1/maintenance/* 自身豁免以便解除；读路径不受影响。
- 组合顺序由 create_app 决定：CORS（如配置）→ 安全 → 维护 → 应用。

默认（未配置环境变量）保持本地开发可用：Host allowlist 覆盖 loopback 名称与
testserver；无 token。远程/LAN 部署必须显式设置 MOTTE_ALLOWED_HOSTS /
MOTTE_ALLOWED_ORIGINS / MOTTE_API_TOKEN（见 docs/operations/upgrade.md）。
"""
from __future__ import annotations

import os
import secrets
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEFAULT_ALLOWED_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]", "testserver")
WRITE_METHODS = ("POST", "PUT", "DELETE", "PATCH")
#: token 豁免路径：探活与能力握手必须先于认证可用（协议 §10）。
TOKEN_EXEMPT_PATHS = ("/health", "/api/v1/capabilities")


def _hostname(raw: str) -> str:
    """从 Host/Origin 头提取小写主机名（剥离端口，兼容 IPv6 字面量）。"""
    if not raw:
        return ""
    candidate = raw if "://" in raw else "//" + raw
    try:
        return (urlsplit(candidate).hostname or "").lower()
    except ValueError:
        return ""


def _json_response(
    send: Send, status: int, code: str, message: str,
) -> Awaitable[None]:
    import json as _json

    async def responder() -> None:
        body = _json.dumps(
            {"error": {"code": code, "message": message}}, ensure_ascii=False
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return responder()


class SingleUserSecurityMiddleware:
    """Host/Origin/token 三层单用户边界；纯 ASGI，流式响应直通。"""

    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_hosts: set[str] | None = None,
        allowed_origins: set[str] | None = None,
        api_token: str | None = None,
    ) -> None:
        self.app = app
        self.allowed_hosts = {host.lower() for host in (allowed_hosts or DEFAULT_ALLOWED_HOSTS)}
        self.allowed_origins = {origin.lower() for origin in (allowed_origins or set())}
        self.api_token = api_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = _hostname(headers.get("host", ""))
        if host and host not in self.allowed_hosts:
            await _json_response(
                send, 400, "HOST_REJECTED",
                f"host {host!r} is not allowed; configure MOTTE_ALLOWED_HOSTS explicitly",
            )
            return
        method = scope.get("method", "GET")
        if method in WRITE_METHODS:
            origin = headers.get("origin")
            if origin:
                origin_host = _hostname(origin)
                same_site = origin_host and origin_host in self.allowed_hosts
                explicit = origin.lower().rstrip("/") in self.allowed_origins
                if not (same_site or explicit):
                    await _json_response(
                        send, 403, "ORIGIN_REJECTED",
                        f"origin {origin!r} is not allowed; configure MOTTE_ALLOWED_ORIGINS"
                        " explicitly",
                    )
                    return
        if self.api_token:
            path = scope.get("path", "")
            if path not in TOKEN_EXEMPT_PATHS:
                authorization = headers.get("authorization", "")
                scheme, _, token = authorization.partition(" ")
                if scheme.lower() != "bearer" or not secrets.compare_digest(
                    token.strip(), self.api_token
                ):
                    await _json_response(
                        send, 401, "AUTH_REQUIRED",
                        "this server requires a bearer token (MOTTE_API_TOKEN)",
                    )
                    return
        await self.app(scope, receive, send)


class MaintenanceModeMiddleware:
    """维护窗口内拒绝 /api/* 写方法；读路径与维护管理端点豁免。"""

    def __init__(self, app: ASGIApp, *, is_active: Callable[[], bool]) -> None:
        self.app = app
        self._is_active = is_active

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        if (
            path.startswith("/api/")
            and method in WRITE_METHODS
            and not path.startswith("/api/v1/maintenance")
            and self._is_active()
        ):
            await _json_response(
                send, 503, "MAINTENANCE_MODE",
                "server is in a maintenance window; writes are rejected until it ends",
            )
            return
        await self.app(scope, receive, send)


def security_config_from_env(
    *,
    allowed_hosts: Any = None,
    allowed_origins: Any = None,
    api_token: Any = None,
) -> dict[str, Any]:
    """create_app 参数 → 环境变量 → 默认值；None 哨兵区分"显式关闭"与"未指定"。"""
    if allowed_hosts is None:
        raw = os.environ.get("MOTTE_ALLOWED_HOSTS", "")
        allowed_hosts = {
            host.strip().lower() for host in raw.split(",") if host.strip()
        } or set(DEFAULT_ALLOWED_HOSTS)
    else:
        allowed_hosts = {host.strip().lower() for host in allowed_hosts}
    if allowed_origins is None:
        raw = os.environ.get("MOTTE_ALLOWED_ORIGINS", "")
        allowed_origins = {
            origin.strip().lower() for origin in raw.split(",") if origin.strip()
        }
    else:
        allowed_origins = {
            origin.strip().lower() for origin in allowed_origins if str(origin).strip()
        }
    if api_token is None:
        api_token = os.environ.get("MOTTE_API_TOKEN") or None
    return {
        "allowed_hosts": allowed_hosts,
        "allowed_origins": allowed_origins,
        "api_token": api_token,
    }
