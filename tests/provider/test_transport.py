import json
from urllib.error import HTTPError

import pytest

from motte_provider.errors import ProviderHTTPError
from motte_provider.transport import HTTPTransport


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_post_json_joins_base_url_and_injects_redacted_authorization():
    requests = []

    def opener(request, timeout):
        requests.append((request, timeout))
        return FakeResponse({"ok": True})

    transport = HTTPTransport("https://api.example.test/v1/", "secret-key", opener=opener)
    assert transport.post_json("/chat/completions", {"model": "m"}) == {"ok": True}
    request, timeout = requests[0]
    assert request.full_url == "https://api.example.test/v1/chat/completions"
    assert request.get_header("Authorization") == "Bearer secret-key"
    assert "secret-key" not in repr(transport)
    assert timeout == 30.0


def test_timeout_is_classified_as_provider_http_error():
    def opener(_request, _timeout):
        raise TimeoutError("timed out")

    with pytest.raises(ProviderHTTPError, match="timed out"):
        HTTPTransport("https://example.test", opener=opener).post_json("chat", {})


def test_429_is_retried_and_succeeds_without_network():
    calls = []

    def opener(request, timeout):
        calls.append((request, timeout))
        if len(calls) == 1:
            raise HTTPError(request.full_url, 429, "busy", {"Retry-After": "0"}, None)
        return FakeResponse({"done": True})

    transport = HTTPTransport("https://example.test", max_retries=1, opener=opener, sleep=lambda _: None)
    assert transport.post_json("chat", {}) == {"done": True}
    assert len(calls) == 2
