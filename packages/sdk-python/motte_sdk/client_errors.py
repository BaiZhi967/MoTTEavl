"""M7 SDK 客户端异常层次（协议 docs/protocols/sdk-and-migration.md frozen@1 §1.1）。

分类规则：先按 HTTP status，再按 ``error.code`` 细化（code 优先于 status 生成子类
信息）。``retryable`` 不是服务端字段，由 SDK 按 isinstance 推导（见
:func:`is_retryable`）。所有异常的 ``str()`` 不包含请求 body（防凭据/prompt 泄漏）。
"""
from __future__ import annotations

from typing import Any, Mapping

MAINTENANCE_CODE = "MAINTENANCE_MODE"

__all__ = [
    "MAINTENANCE_CODE",
    "ApiError",
    "AuthenticationError",
    "ConflictError",
    "ConnectError",
    "MaintenanceError",
    "MotteClientError",
    "NetworkError",
    "NotFoundError",
    "OperationCancelled",
    "PermissionDeniedError",
    "RateLimitedError",
    "ReadTimeout",
    "ServerError",
    "TransportError",
    "UnsupportedCapabilityError",
    "ValidationError",
    "WaitTimeout",
    "classify",
    "is_retryable",
]


class MotteClientError(Exception):
    """SDK 客户端错误基类：携带 request_id | None、http_status | None。"""

    def __init__(
        self,
        message: str,
        *,
        request_id: str | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.request_id = request_id
        self.http_status = http_status

    def __str__(self) -> str:  # pragma: no cover - Exception 默认即 message
        return self.message


# --------------------------------------------------------------------- transport


class TransportError(MotteClientError):
    """网络层失败：连不上/DNS/读超时等（GET 查询的可安全重试见协议 §1.4）。"""


class ConnectError(TransportError):
    """连接建立失败（含连接超时/DNS 解析失败）。"""


class ReadTimeout(TransportError):
    """连接已建立但读取响应超时。"""


class NetworkError(TransportError):
    """其他网络层故障（连接中断、协议错误、拒绝跟随的重定向等）。"""


# ------------------------------------------------------------------------ API


class ApiError(MotteClientError):
    """服务端返回了错误 envelope（``{"error": {...}}``）或 detail 形状。"""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        request_id: str | None = None,
        http_status: int | None = None,
        retry_after: float | None = None,
        details: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(message, request_id=request_id, http_status=http_status)
        self.code = code
        self.retry_after = retry_after
        self.details = details or []


class ValidationError(ApiError):
    """422：请求配置错误——永不自动重试。"""


class AuthenticationError(ApiError):
    """401（以及 code 表明认证失败的 403）。"""


class PermissionDeniedError(ApiError):
    """403：认证通过但被策略拒绝（code 非认证类）。"""


class NotFoundError(ApiError):
    """404。"""


class ConflictError(ApiError):
    """409：含 REQUEST_KEY_CONFLICT / RUN_CONFLICT。"""


class RateLimitedError(ApiError):
    """429：尊重 Retry-After。"""


class ServerError(ApiError):
    """5xx（除维护语义外的服务端错误）。"""


class MaintenanceError(ApiError):
    """503 且 error.code == MAINTENANCE_MODE：维护窗口内。"""


# ------------------------------------------------------------ SDK 自身语义错误


class UnsupportedCapabilityError(MotteClientError):
    """能力协商失败（协议 §1.2）：所需 feature 键缺失，携带 missing 列表。"""

    def __init__(self, missing: list[str], *, request_id: str | None = None) -> None:
        joined = ", ".join(sorted(missing))
        super().__init__(
            f"server capabilities do not advertise required feature(s): {joined}",
            request_id=request_id,
        )
        self.missing = list(missing)


class OperationCancelled(MotteClientError):
    """调用方通过 cancel_event 取消了阻塞等待。"""


class WaitTimeout(MotteClientError):
    """wait/stream 超时：只停止等待，绝不暗中 cancel 远端 Run；携带最后已知 Run。"""

    def __init__(self, message: str, *, run: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.run = dict(run) if run is not None else None


# --------------------------------------------------------------------- 推导


def is_retryable(error: BaseException) -> bool:
    """协议 §1.1：按异常类型推导可重试性（服务端不返回 retryable 字段）。"""
    return isinstance(
        error, (TransportError, ServerError, RateLimitedError, MaintenanceError)
    )


def _safe_detail_items(detail: Any) -> list[dict[str, str]]:
    """FastAPI 422 ``{"detail": [{loc, msg, type, input, ...}]}`` 的安全摘要。

    pydantic v2 的错误项含 ``input``（可能是完整请求体），只保留 loc/msg/type，
    绝不携带输入值。
    """
    if not isinstance(detail, list):
        return []
    items: list[dict[str, str]] = []
    for entry in detail:
        if not isinstance(entry, Mapping):
            continue
        location = ".".join(str(part) for part in entry.get("loc", ())) or "$"
        items.append(
            {
                "field": location,
                "code": str(entry.get("type") or "invalid"),
                "message": str(entry.get("msg") or "invalid value"),
            }
        )
    return items


def _error_fields(payload: Mapping[str, Any] | None) -> tuple[str | None, str]:
    """从两种既有错误形状提取 (code, message)；不发明第三种线上形状。"""
    if not isinstance(payload, Mapping):
        return None, ""
    envelope = payload.get("error")
    if isinstance(envelope, Mapping):
        code = envelope.get("code")
        message = envelope.get("message", envelope.get("detail", ""))
        return (str(code) if code is not None else None), str(message or "")
    detail = payload.get("detail")
    if isinstance(detail, str):
        return None, detail
    if isinstance(detail, Mapping):
        code = detail.get("code")
        message = detail.get("message", detail.get("detail", ""))
        return (str(code) if code is not None else None), str(message or "")
    return None, ""


def classify(
    status: int,
    payload: Mapping[str, Any] | None,
    *,
    request_id: str | None = None,
    retry_after: float | None = None,
) -> ApiError:
    """把 (HTTP status, 响应体) 映射为 typed 异常（协议 §1.1 分类规则）。

    ``payload`` 同时接受应用错误 ``{"error": {...}}``、FastAPI 422
    ``{"detail": [...]}`` 与 HTTPException ``{"detail": str|dict}``。
    生成的异常 message 不包含请求 body。
    """
    code, message = _error_fields(payload)
    details = _safe_detail_items(payload.get("detail") if isinstance(payload, Mapping) else None)
    if not message:
        if details:
            message = "; ".join(
                f"{item['field']}: {item['message']}" for item in details[:5]
            )
        else:
            message = f"api request failed with http status {status}"
    kwargs: dict[str, Any] = {
        "code": code,
        "request_id": request_id,
        "http_status": status,
        "retry_after": retry_after,
        "details": details,
    }
    if status == 422:
        return ValidationError(message, **kwargs)
    if status == 401:
        return AuthenticationError(message, **kwargs)
    if status == 403:
        # code 优先：认证类 403（AUTH_*）归 AuthenticationError，其余归权限拒绝。
        if code is not None and code.upper().startswith("AUTH"):
            return AuthenticationError(message, **kwargs)
        return PermissionDeniedError(message, **kwargs)
    if status == 404:
        return NotFoundError(message, **kwargs)
    if status == 409:
        return ConflictError(message, **kwargs)
    if status == 429:
        return RateLimitedError(message, **kwargs)
    if status >= 500:
        if status == 503 and code == MAINTENANCE_CODE:
            return MaintenanceError(message, **kwargs)
        return ServerError(message, **kwargs)
    return ApiError(message, **kwargs)
