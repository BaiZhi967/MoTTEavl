"""Small, dependency-free HTTP transport for OpenAI-compatible providers."""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import (
    ProviderAuthenticationError,
    ProviderHTTPError,
    ProviderRateLimitError,
)


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
        opener: Callable = urlopen,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self._api_key = api_key
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self._opener = opener
        self._sleep = sleep
        self._clock = clock

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
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._api_key:
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
                    headers={"Content-Type": headers["Content-Type"], "Accept": headers["Accept"]},
                )
            except HTTPError as exc:
                outcome = TransportOutcome(
                    url=url,
                    path=path,
                    request_body=payload,
                    status=exc.code,
                    attempts=attempt,
                    latency_ms=(self._clock() - started) * 1000,
                    error_class="rate_limit" if exc.code == 429 else None,
                    error_message=str(exc.reason),
                )
                if exc.code == 429 and attempt <= self.max_retries:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        delay = max(0.0, float(retry_after)) if retry_after else 0.0
                    except ValueError:
                        delay = 0.0
                    self._sleep(delay)
                    continue
                error = self._http_error(exc)
                error.outcome = outcome
                raise error from exc
            except (TimeoutError, URLError, OSError, json.JSONDecodeError) as exc:
                outcome = TransportOutcome(
                    url=url,
                    path=path,
                    request_body=payload,
                    attempts=attempt,
                    latency_ms=(self._clock() - started) * 1000,
                    error_class="protocol" if isinstance(exc, json.JSONDecodeError) else "network",
                    error_message=str(exc),
                )
                if outcome.error_class == "network" and attempt <= self.max_retries:
                    delay = min(30.0, 0.5 * (2 ** (attempt - 1)))
                    self._sleep(delay)
                    continue
                error = ProviderHTTPError(str(exc), error_class=outcome.error_class)
                error.outcome = outcome
                raise error from exc

    @staticmethod
    def _http_error(exc: HTTPError) -> ProviderHTTPError:
        message = f"provider HTTP {exc.code}: {exc.reason}"
        if exc.code in (401, 403):
            return ProviderAuthenticationError(message, status=exc.code)
        if exc.code == 429:
            return ProviderRateLimitError(message, status=exc.code)
        return ProviderHTTPError(message, status=exc.code)
