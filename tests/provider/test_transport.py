import json
import socket
import threading
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from motte_provider.errors import ProviderHTTPError, ProviderRedirectError
from motte_provider.transport import HTTPTransport, _SafeRedirectHandler


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


class _RedirectHandler(BaseHTTPRequestHandler):
    location = ""

    def do_POST(self):  # noqa: N802
        self.send_response(302)
        self.send_header("Location", self.location)
        self.end_headers()

    def log_message(self, *_args):
        return


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
    def opener(_request, *, timeout):
        assert timeout == 30.0
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

    transport = HTTPTransport("https://example.test", max_retries=1, opener=opener, sleep=lambda _: None, default_headers={"Idempotency-Key": "test-key"})
    assert transport.post_json("chat", {}) == {"done": True}
    assert len(calls) == 2


def test_opener_receives_timeout_as_keyword_argument():
    def opener(request, *, timeout):
        assert timeout == 30.0
        return FakeResponse({"ok": True})

    transport = HTTPTransport("https://example.test", opener=opener)
    assert transport.post_json("chat", {}) == {"ok": True}


def test_transient_network_error_is_retried_with_backoff():
    calls = []
    delays = []

    def opener(request, *, timeout):
        calls.append((request, timeout))
        if len(calls) == 1:
            raise URLError("temporary connection reset")
        return FakeResponse({"done": True})

    transport = HTTPTransport(
        "https://example.test", max_retries=1, opener=opener, sleep=delays.append,
        default_headers={"Idempotency-Key": "test-key"},
    )
    assert transport.post_json("chat", {}) == {"done": True}
    assert len(calls) == 2
    assert delays == [0.5]


def test_dns_failure_message_names_host_and_points_at_base_url():
    """裸的 getaddrinfo failed 对使用者没有指向性：提示必须点名主机与 Base URL。"""

    def opener(request, *, timeout):
        raise URLError(socket.gaierror(11001, "getaddrinfo failed"))

    transport = HTTPTransport("https://6a-g-bits.com/v1", max_retries=0, opener=opener)
    with pytest.raises(ProviderHTTPError) as failure:
        transport.post_json("chat", {})

    error = failure.value
    assert error.error_class == "network"
    assert "6a-g-bits.com" in str(error)
    assert "DNS" in str(error)
    assert "Base URL" in str(error)
    assert "getaddrinfo failed" in str(error)  # 原始证据保留
    assert error.outcome.error_message == str(error)


def test_connection_refused_message_names_host_and_port_hint():
    def opener(request, *, timeout):
        raise URLError(ConnectionRefusedError(10061, "No connection could be made"))

    transport = HTTPTransport("http://gateway.internal:9000/v1", max_retries=0, opener=opener)
    with pytest.raises(ProviderHTTPError) as failure:
        transport.post_json("chat", {})

    message = str(failure.value)
    assert "gateway.internal" in message
    assert "端口" in message


def test_retry_after_http_date_is_honored_for_transient_status():
    delays = []
    headers = {"Retry-After": formatdate(1_000.0, usegmt=True)}
    attempts = []

    def opener(request, *, timeout):
        attempts.append(1)
        if len(attempts) == 1:
            raise HTTPError(request.full_url, 503, "busy", headers, None)
        return FakeResponse({"ok": True})

    transport = HTTPTransport(
        "https://example.test",
        max_retries=1,
        opener=opener,
        sleep=delays.append,
        wall_clock=lambda: 990.0,
        default_headers={"Idempotency-Key": "test-key"},
    )
    assert transport.post_json("chat", {}) == {"ok": True}
    assert delays == [10.0]


def test_local_http_server_retries_503_with_retry_after():
    state = {"calls": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            state["calls"] += 1
            if state["calls"] == 1:
                self.send_response(503)
                self.send_header("Retry-After", "0")
                self.end_headers()
                return
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        transport = HTTPTransport(
            f"http://127.0.0.1:{server.server_port}",
            max_retries=1,
            sleep=lambda _: None,
            default_headers={"Idempotency-Key": "test-key"},
        )
        assert transport.post_json("chat", {}) == {"ok": True}
        assert state["calls"] == 2
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_post_json_does_not_retry_without_idempotency_key():
    calls = []

    def opener(request, *, timeout):
        calls.append(request)
        raise URLError("connection reset after dispatch")

    with pytest.raises(ProviderHTTPError) as failure:
        HTTPTransport("https://example.test", max_retries=3, opener=opener).post_json("chat", {})
    assert failure.value.error_class == "network"
    assert len(calls) == 1


def test_post_json_does_not_replay_server_error_without_idempotency_key():
    calls = []

    def opener(request, *, timeout):
        calls.append(request)
        raise HTTPError(request.full_url, 503, "busy", {}, None)

    with pytest.raises(ProviderHTTPError) as failure:
        HTTPTransport("https://example.test", max_retries=3, opener=opener).post_json("chat", {})
    assert failure.value.error_class == "server"
    assert len(calls) == 1


def test_https_to_http_redirect_is_refused():
    request = Request("https://provider.example/v1/messages", method="POST")
    with pytest.raises(ProviderRedirectError, match="redirect refused"):
        _SafeRedirectHandler().redirect_request(
            request, None, 302, "Found", {}, "http://provider.example/v1/messages"
        )


def test_post_json_redirect_refuses_cross_origin_without_forwarding_credentials():
    first = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
    second_state = {"authorization": None}

    class CaptureHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            second_state["authorization"] = self.headers.get("Authorization")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_args):
            return

    second = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
    first.RequestHandlerClass.location = f"http://127.0.0.1:{second.server_port}/json"
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (first, second)]
    for thread in threads:
        thread.start()
    try:
        transport = HTTPTransport(f"http://127.0.0.1:{first.server_port}", "TOPSECRET")
        with pytest.raises(ProviderRedirectError, match="redirect refused"):
            transport.post_json("json", {})
        assert second_state["authorization"] is None
    finally:
        for server in (first, second):
            server.shutdown()
        for thread in threads:
            thread.join(timeout=2)


def test_post_sse_redirect_refuses_cross_origin_without_forwarding_credentials():
    first = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
    second_state = {"authorization": None}

    class CaptureHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            second_state["authorization"] = self.headers.get("Authorization")
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            return

    second = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
    first.RequestHandlerClass.location = f"http://127.0.0.1:{second.server_port}/events"
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (first, second)]
    for thread in threads:
        thread.start()
    try:
        transport = HTTPTransport(
            f"http://127.0.0.1:{first.server_port}", "TOPSECRET", max_retries=0,
        )
        with pytest.raises(Exception, match="redirect refused"):
            list(transport.post_sse("events", {}))
        assert second_state["authorization"] is None
    finally:
        for server in (first, second):
            server.shutdown()
        for thread in threads:
            thread.join(timeout=2)
