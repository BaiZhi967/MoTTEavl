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


def classify_exception(error: Exception) -> str:
    return getattr(error, "error_class", None) or (
        "network" if isinstance(error, (TimeoutError, OSError)) else "unknown"
    )
