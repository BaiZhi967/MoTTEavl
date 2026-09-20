"""Small, dependency-free HTTP transport for OpenAI-compatible providers."""

import json
import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from datetime import timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .errors import (
    ProviderAuthenticationError,
    ProviderHTTPError,
    ProviderRateLimitError,
    classify_error_type,
    classify_status,
)

AUTH_STYLES = ("bearer", "x-api-key")


@dataclass(frozen=True)
class TransportOutcome:
    """一次调用的完整计量结果；失败时挂在异常的 outcome 属性上。"""

    url: str
    path: str
    request_body: dict
    status: int | None = None
    response_body: dict | None = None
    attempts: int = 0
    latency_ms: float = 0.0
    error_class: str | None = None
    error_message: str | None = None
    headers: dict = field(default_factory=dict)


class HTTPTransport:
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        timeout: float = 30.0,
        max_retries: int = 2,
        backoff_initial: float = 0.5,
        backoff_max: float = 30.0,
        auth: str = "bearer",
        default_headers: dict[str, str] | None = None,
        opener: Callable = urlopen,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if auth not in AUTH_STYLES:
            raise ValueError(f"unsupported auth style: {auth!r} (expected one of {AUTH_STYLES})")
        self.base_url = base_url.rstrip("/") + "/"
        self._api_key = api_key
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self.backoff_initial = max(0.0, float(backoff_initial))
        self.backoff_max = max(self.backoff_initial, float(backoff_max))
        self._auth = auth
        self._default_headers = dict(default_headers or {})
        self._opener = opener
        self._sleep = sleep
        self._clock = clock
        self._wall_clock = wall_clock

    def __repr__(self) -> str:
        return (
            f"HTTPTransport(base_url={self.base_url!r}, timeout={self.timeout!r}, "
            f"max_retries={self.max_retries!r}, api_key='[redacted]')"
        )

    def post_json(self, path: str, payload: dict) -> dict:
        """兼容入口：返回响应 JSON，失败抛 ProviderError。"""
        return self.post_json_detailed(path, payload).response_body

    def post_json_detailed(self, path: str, payload: dict) -> TransportOutcome:
        """POST JSON 并返回计量结果（attempts/latency_ms/错误分类）。"""
        url = self.base_url + path.lstrip("/")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **self._default_headers,
        }
        if self._api_key:
            if self._auth == "x-api-key":
                headers["x-api-key"] = self._api_key
            else:
                headers["Authorization"] = f"Bearer {self._api_key}"
        request = Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")

        started = self._clock()
        attempt = 0
        while True:
            attempt += 1
            try:
                with self._opener(request, timeout=self.timeout) as response:
                    raw = response.read()
                body = json.loads(raw)
                return TransportOutcome(
                    url=url,
                    path=path,
                    request_body=payload,
                    status=getattr(response, "status", 200),
                    response_body=body,
                    attempts=attempt,
                    latency_ms=(self._clock() - started) * 1000,
                    headers={"Content-Type": "application/json", "Accept": "application/json", **self._default_headers},
                )
            except HTTPError as exc:
                error_body = _read_error_body(exc)
                error_type, error_message = _provider_error_info(error_body)
                error_class = classify_error_type(error_type) or classify_status(exc.code)
                outcome = TransportOutcome(
                    url=url,
                    path=path,
                    request_body=payload,
                    status=exc.code,
                    response_body=error_body if isinstance(error_body, dict) else None,
                    attempts=attempt,
                    latency_ms=(self._clock() - started) * 1000,
                    error_class=error_class,
                    error_message=error_message or str(exc.reason),
                    headers={"Content-Type": "application/json", "Accept": "application/json", **self._default_headers},
                )
                if self._is_retryable_status(exc.code) and attempt <= self.max_retries:
                    delay = self._retry_delay(exc.headers, attempt)
                    self._sleep(delay)
                    continue
                error = self._http_error(exc, error_type=error_type, error_message=error_message)
                error.outcome = outcome
                raise error from exc
            except (TimeoutError, URLError, OSError, json.JSONDecodeError) as exc:
                error_class = "protocol" if isinstance(exc, json.JSONDecodeError) else "network"
                message = str(exc) if error_class == "protocol" else describe_network_error(url, exc)
                outcome = TransportOutcome(
                    url=url,
                    path=path,
                    request_body=payload,
                    attempts=attempt,
                    latency_ms=(self._clock() - started) * 1000,
                    error_class=error_class,
                    error_message=message,
                )
                if outcome.error_class == "network" and attempt <= self.max_retries:
                    delay = min(self.backoff_max, self.backoff_initial * (2 ** (attempt - 1)))
                    self._sleep(delay)
                    continue
                error = ProviderHTTPError(message, error_class=outcome.error_class)
                error.outcome = outcome
                raise error from exc

    def post_sse(self, path: str, payload: dict, *, headers_extra: dict | None = None):
        """POST 并以生成器产出 SSE 事件（M4-T08 流式通道）。

        - Accept: text/event-stream；每个事件以 ``{"event": str|None, "data": str}`` 产出；
        - 流式不做重试（可能已交付部分数据，重放会重复副作用）；
        - 消费方提前停止迭代（GeneratorExit/close）即断流取消。
        """
        url = self.base_url + path.lstrip("/")
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            **self._default_headers,
            **(headers_extra or {}),
        }
        if self._api_key:
            if self._auth == "x-api-key":
                headers["x-api-key"] = self._api_key
            else:
                headers["Authorization"] = f"Bearer {self._api_key}"
        request = Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        response = self._opener(request, timeout=self.timeout)
        try:
            event_name: str | None = None
            data_lines: list[str] = []
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="strict").rstrip("\r\n") if isinstance(raw_line, bytes) else str(raw_line).rstrip("\r\n")
                if line == "":
                    if data_lines:
                        yield {"event": event_name, "data": "\n".join(data_lines)}
                    event_name = None
                    data_lines = []
                    continue
                if line.startswith(":"):
                    continue  # SSE 注释/心跳
                if line.startswith("event:"):
                    event_name = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:"):].strip())
            if data_lines:
                yield {"event": event_name, "data": "\n".join(data_lines)}
        finally:
            close = getattr(response, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:  # noqa: BLE001 - 断流关闭失败不影响已交付事件
                    pass

    def _retry_delay(self, headers: object, attempt: int) -> float:
        """Resolve Retry-After (delta or HTTP date), falling back to backoff."""
        retry_after = None
        if headers is not None and hasattr(headers, "get"):
            retry_after = headers.get("Retry-After")  # type: ignore[union-attr]
        delay: float | None = None
        if retry_after:
            try:
                delay = max(0.0, float(str(retry_after).strip()))
            except (TypeError, ValueError):
                try:
                    parsed = parsedate_to_datetime(str(retry_after))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    delay = max(0.0, parsed.timestamp() - self._wall_clock())
                except (TypeError, ValueError, OverflowError):
                    delay = None
        if delay is None:
            delay = self.backoff_initial * (2 ** (attempt - 1))
        return min(self.backoff_max, delay)

    @staticmethod
    def _is_retryable_status(status: int) -> bool:
        # 529：Anthropic overloaded（可重试的容量类错误）
        return status in (408, 429, 500, 502, 503, 504, 529)

    @staticmethod
    def _http_error(
        exc: HTTPError,
        *,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> ProviderHTTPError:
        message = f"provider HTTP {exc.code}: {exc.reason}"
        if error_type or error_message:
            detail = error_message or exc.reason
            message = f"provider HTTP {exc.code}: {detail}"
            if error_type:
                message += f" (type={error_type})"
        if exc.code in (401, 403):
            return ProviderAuthenticationError(message, status=exc.code)
        if exc.code == 429:
            return ProviderRateLimitError(message, status=exc.code)
        return ProviderHTTPError(message, status=exc.code)


def describe_network_error(url: str, exc: Exception) -> str:
    """网络层失败 → 可操作提示：点名主机与排查方向，原始异常原样附后（证据不丢）。

    urllib 把底层 socket 错误包进 URLError，根因在 exc.reason 上，因此两者都看。
    "getaddrinfo failed" / "[Errno 11001]" 这类裸文本对使用者毫无指向性，必须补出主机名。
    """
    host = urlsplit(url).hostname or url
    reason = getattr(exc, "reason", exc)
    detail = str(exc)
    lowered = detail.lower()
    if isinstance(reason, socket.gaierror) or "getaddrinfo" in lowered:
        return f"无法解析主机 {host}（DNS 查询失败）：请核对该连接的 Base URL 与网络/代理设置；原始错误：{detail}"
    if isinstance(reason, ssl.SSLError) or "certificate" in lowered:
        return f"TLS 握手失败（{host}）：证书校验未通过或协议不受支持；原始错误：{detail}"
    if isinstance(reason, ConnectionRefusedError) or "refused" in lowered:
        return f"连接被拒绝（{host} 上无服务监听该端口）：请核对 Base URL 的地址与端口；原始错误：{detail}"
    if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError) or "timed out" in lowered:
        return f"请求超时（{host} 未在超时时间内响应）：请核对该端点是否可达；原始错误：{detail}"
    return f"网络错误（无法连接 {host}）：原始错误：{detail}"


def _read_error_body(exc: HTTPError) -> dict | list | None:
    """尽力读取错误响应体并解析 JSON；失败返回 None（不影响错误传播）。"""
    try:
        raw = exc.read()
    except Exception:  # noqa: BLE001 — 证据读取绝不改变错误语义
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _provider_error_info(body: dict | list | None) -> tuple[str | None, str | None]:
    """从 OpenAI/Anthropic 通行错误形状 {error: {type, message}} 提取信息。"""
    if not isinstance(body, dict):
        return None, None
    error = body.get("error")
    if isinstance(error, dict):
        error_type = error.get("type")
        message = error.get("message")
        return (
            error_type if isinstance(error_type, str) else None,
            message if isinstance(message, str) else None,
        )
    if isinstance(error, str):
        return None, error
    return None, None
