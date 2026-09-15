from motte_trace.redaction import redact
from motte_trace.sink import TraceSink


def test_redaction_removes_secret():
    assert redact({"api_key": "secret", "x": 1})["api_key"] == "[REDACTED]"


def test_redaction_covers_headers_and_passwords():
    value = redact({"Cookie": "session", "proxy-authorization": "basic", "password": "secret"})
    assert value == {
        "Cookie": "[REDACTED]",
        "proxy-authorization": "[REDACTED]",
        "password": "[REDACTED]",
    }


def test_sink_assigns_monotonic_sequence():
    s = TraceSink("r")
    assert [s.emit("a"), s.emit("b")] == [1, 2]
