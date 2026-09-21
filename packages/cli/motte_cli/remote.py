"""M7 CLI local/server 模式路由（协议 docs/protocols/sdk-and-migration.md frozen@1 §3）。

server 模式只通过 ``MotteClient`` HTTP 访问远端：不打开本地 DB、不构造
Provider、不启动任务；远端不可达/认证失败/超时 → stderr 单行 JSON 诊断 +
非零退出，**绝不静默回退本地**。local 模式语义与既有 CLI 完全一致。

配置优先级（协议 §3）：命令行参数 > 环境变量 > 默认 local。

- ``--mode {local,server}`` / ``MOTTE_CLI_MODE``
- ``--api-url URL`` / ``MOTTE_API_URL``（server 模式必填）
- ``--api-token TOKEN`` / ``MOTTE_API_TOKEN``
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Callable

__all__ = [
    "MODE_ENV",
    "API_URL_ENV",
    "API_TOKEN_ENV",
    "RemoteOk",
    "add_mode_arguments",
    "build_client",
    "call_remote",
    "cli_error",
    "client_error_payload",
    "is_server",
    "local_only_error",
    "remote_error",
    "server_precondition_error",
]

MODE_ENV = "MOTTE_CLI_MODE"
API_URL_ENV = "MOTTE_API_URL"
API_TOKEN_ENV = "MOTTE_API_TOKEN"


def add_mode_arguments(parser: argparse.ArgumentParser) -> None:
    """给一个子命令挂上 §3 的模式路由参数（argparse 子解析器不共享顶层参数）。"""
    group = parser.add_argument_group("mode", "local/server 路由（协议 §3：参数 > 环境变量 > 默认 local）")
    group.add_argument(
        "--mode", choices=("local", "server"),
        help=f"执行模式（默认取 {MODE_ENV}，否则 local）",
    )
    group.add_argument(
        "--api-url", dest="api_url",
        help=f"server 模式 API 根地址（默认取 {API_URL_ENV}；server 模式必填）",
    )
    group.add_argument(
        "--api-token", dest="api_token",
        help=f"server 模式 Bearer token（默认取 {API_TOKEN_ENV}；可省略）",
    )


def _arg_or_env(args: argparse.Namespace, name: str, env: str) -> str | None:
    value = getattr(args, name, None)
    if value:
        return value
    return os.environ.get(env) or None


def resolved_mode(args: argparse.Namespace) -> str:
    mode = _arg_or_env(args, "mode", MODE_ENV)
    return mode or "local"


def resolved_api_url(args: argparse.Namespace) -> str | None:
    return _arg_or_env(args, "api_url", API_URL_ENV)


def resolved_api_token(args: argparse.Namespace) -> str | None:
    return _arg_or_env(args, "api_token", API_TOKEN_ENV)


def is_server(args: argparse.Namespace) -> bool:
    return resolved_mode(args) == "server"


def cli_error(code: str, message: str, *, exit_code: int = 2, **extra: Any) -> int:
    """与 main._error 同一形状：stderr 单行 ``{"error": {...}}``，返回退出码。"""
    payload = {"code": code, "message": message, **extra}
    print(json.dumps({"error": payload}, ensure_ascii=False), file=sys.stderr)
    return exit_code


def server_precondition_error(args: argparse.Namespace) -> int | None:
    """server 模式前置检查：``--db`` 属于 local 语义；API 地址必填。

    返回 None 表示可以继续；否则返回已打印错误的退出码。
    """
    if getattr(args, "db", None):
        return cli_error(
            "MODE_MISMATCH",
            "--db only applies to local mode; server mode never opens a local database "
            "(protocol sdk-and-migration.md §3)",
        )
    if not resolved_api_url(args):
        return cli_error(
            "MODE_CONFIG_MISSING",
            f"server mode requires --api-url or {API_URL_ENV}",
        )
    return None


def local_only_error(args: argparse.Namespace, command: str) -> int | None:
    """local-only 命令（backup/restore/gc/import…涉及宿主文件或凭据）的护栏。"""
    if is_server(args):
        return cli_error(
            "LOCAL_ONLY_COMMAND",
            f"'{command}' touches host files or credentials and never executes remotely; "
            "run it with --mode local (protocol sdk-and-migration.md §3)",
        )
    return None


def build_client(args: argparse.Namespace):
    """按解析后的配置构建 MotteClient（server 模式唯一的数据通道）。

    测试通过 monkeypatch 本函数注入 SyncASGITransport 等自定义 transport
    （tests/cli/test_remote_parity.py）。
    """
    from motte_sdk import MotteClient

    return MotteClient(
        resolved_api_url(args) or "http://localhost:8000",
        token=resolved_api_token(args),
    )


# ------------------------------------------------------------- 错误映射（§3）


def _fallback_code(error: Exception) -> str:
    from motte_sdk.client_errors import (
        AuthenticationError,
        ConflictError,
        MaintenanceError,
        NotFoundError,
        PermissionDeniedError,
        RateLimitedError,
        ValidationError,
    )

    if isinstance(error, AuthenticationError):
        return "AUTH_REQUIRED"
    if isinstance(error, PermissionDeniedError):
        return "PERMISSION_DENIED"
    if isinstance(error, NotFoundError):
        return "NOT_FOUND"
    if isinstance(error, ConflictError):
        return "CONFLICT"
    if isinstance(error, RateLimitedError):
        return "RATE_LIMITED"
    if isinstance(error, MaintenanceError):
        return "MAINTENANCE_MODE"
    if isinstance(error, ValidationError):
        return "REQUEST_INVALID"
    return "API_ERROR"


def client_error_payload(
    error: Exception, *, default_code: str | None = None,
) -> tuple[dict[str, Any], int]:
    """MotteClientError → (CLI error payload, 退出码)。

    - TransportError → ``TRANSPORT``（远端不可达，绝不回退本地）
    - WaitTimeout → ``TIMEOUT``（run 未被取消）；OperationCancelled → 4
    - 5xx → 退出码 3（execution_error 族）；其余 API 错误 → 退出码 2
    - 服务端 404 未携带 code 时用 ``default_code``（local/server 同错误码 parity）
    """
    from motte_sdk.client_errors import (
        ApiError,
        MotteClientError,
        NotFoundError,
        OperationCancelled,
        ServerError,
        TransportError,
        UnsupportedCapabilityError,
        WaitTimeout,
    )

    assert isinstance(error, MotteClientError)  # noqa: S101 - 调用方契约
    if isinstance(error, TransportError):
        return (
            {
                "code": "TRANSPORT",
                "message": str(error),
                "kind": type(error).__name__,
            },
            2,
        )
    if isinstance(error, WaitTimeout):
        payload: dict[str, Any] = {"code": "TIMEOUT", "message": str(error)}
        if error.run is not None:
            payload["run"] = {
                "id": error.run.get("id"),
                "status": error.run.get("status"),
            }
        return payload, 2
    if isinstance(error, OperationCancelled):
        return {"code": "CANCELLED_BY_USER", "message": str(error)}, 4
    if isinstance(error, UnsupportedCapabilityError):
        return (
            {
                "code": "CAPABILITY_UNSUPPORTED",
                "message": str(error),
                "missing": list(error.missing),
            },
            2,
        )
    exit_code = 3 if isinstance(error, ServerError) else 2
    if isinstance(error, ApiError):
        code = getattr(error, "code", None)
        if code is None:
            code = default_code if isinstance(error, NotFoundError) else _fallback_code(error)
        payload = {"code": code, "message": str(error)}
        if error.request_id:
            payload["request_id"] = error.request_id
        if error.http_status is not None:
            payload["http_status"] = error.http_status
        return payload, exit_code
    return {"code": "CLIENT_ERROR", "message": str(error)}, 2


def remote_error(error: Exception, *, default_code: str | None = None) -> int:
    payload, exit_code = client_error_payload(error, default_code=default_code)
    return cli_error(payload.pop("code"), payload.pop("message"), exit_code=exit_code, **payload)


class RemoteOk:
    """call_remote 的成功信封（payload 可能是 falsy 值，不能用 None 判别）。"""

    def __init__(self, payload: Any) -> None:
        self.payload = payload


def call_remote(
    args: argparse.Namespace,
    invoke: Callable[[Any], Any],
    *,
    default_code: str | None = None,
) -> RemoteOk | int:
    """server 模式统一入口：前置检查 → MotteClient 调用 → 错误映射。

    返回 ``RemoteOk(payload)`` 或已打印错误的退出码；KeyboardInterrupt 由
    调用方处理（run-wait 映射为 CANCELLED_BY_USER / 4）。
    """
    precondition = server_precondition_error(args)
    if precondition is not None:
        return precondition
    from motte_sdk.client_errors import MotteClientError

    client = build_client(args)
    try:
        return RemoteOk(invoke(client))
    except MotteClientError as error:
        return remote_error(error, default_code=default_code)
    finally:
        client.close()
