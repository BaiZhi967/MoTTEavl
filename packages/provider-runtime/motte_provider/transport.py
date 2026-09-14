"""Small, dependency-free HTTP transport for OpenAI-compatible providers."""

import json
import time
from collections.abc import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import ProviderHTTPError


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
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self._api_key = api_key
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self._opener = opener
        self._sleep = sleep

    def __repr__(self) -> str:
        return (
            f"HTTPTransport(base_url={self.base_url!r}, timeout={self.timeout!r}, "
            f"max_retries={self.max_retries!r}, api_key='[redacted]')"
        )

    def post_json(self, path: str, payload: dict) -> dict:
        url = self.base_url + path.lstrip("/")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")

        attempt = 0
        while True:
            try:
                with self._opener(request, self.timeout) as response:
                    raw = response.read()
                return json.loads(raw)
            except HTTPError as exc:
                if exc.code == 429 and attempt < self.max_retries:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        delay = max(0.0, float(retry_after)) if retry_after else 0.0
                    except ValueError:
                        delay = 0.0
                    self._sleep(delay)
                    attempt += 1
                    continue
                raise ProviderHTTPError(f"provider HTTP {exc.code}: {exc.reason}") from exc
            except (TimeoutError, URLError, OSError, json.JSONDecodeError) as exc:
                raise ProviderHTTPError(str(exc)) from exc
