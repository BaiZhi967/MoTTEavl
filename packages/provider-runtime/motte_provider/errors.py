class ProviderError(Exception):
    """Provider 调用异常基类。"""


class ProviderHTTPError(ProviderError):
    error_class: str | None = None

    def __init__(self, message: str, *, status: int | None = None, error_class: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        if error_class is not None:
            self.error_class = error_class
        elif status is not None:
            self.error_class = classify_status(status)


class ProviderProtocolError(ProviderError):
    error_class = "protocol"


class ProviderAuthenticationError(ProviderHTTPError):
    error_class = "auth"


class ProviderRateLimitError(ProviderHTTPError):
    error_class = "rate_limit"


class ProviderTimeoutError(ProviderError):
    error_class = "timeout"


def classify_status(status: int) -> str:
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limit"
    if status in (408, 504):
        return "timeout"
    if status >= 500:
        return "server"
    return "client"


# 响应体 error.type（OpenAI / Anthropic 通行拼写）→ 错误分类；状态码之外的精化来源
_ERROR_TYPE_CLASSES = {
    "authentication_error": "auth",
    "permission_error": "auth",
    "invalid_api_key": "auth",
    "forbidden": "auth",
    "rate_limit_error": "rate_limit",
    "rate_limit_exceeded": "rate_limit",
    "overloaded_error": "server",
    "api_error": "server",
    "server_error": "server",
    "internal_server_error": "server",
    "timeout_error": "timeout",
    "request_timeout": "timeout",
}


def classify_error_type(error_type: object) -> str | None:
    """按响应体 error.type 精化错误分类；未知类型返回 None（回退状态码分类）。"""
    if not isinstance(error_type, str):
        return None
    return _ERROR_TYPE_CLASSES.get(error_type.strip().lower())


def classify_exception(error: Exception) -> str:
    return getattr(error, "error_class", None) or (
        "network" if isinstance(error, (TimeoutError, OSError)) else "unknown"
    )
