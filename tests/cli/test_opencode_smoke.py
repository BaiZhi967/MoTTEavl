"""The quick coding smoke is dry by default and cannot expand into a benchmark."""
import importlib
import json
from urllib.error import HTTPError

import pytest

from tests.provider.test_openai_compatible import FakeResponse


def smoke():
    # Import inside tests so the RED phase asserts the missing feature explicitly.
    assert importlib.util.find_spec("motte_cli.opencode_smoke") is not None
    return importlib.import_module("motte_cli.opencode_smoke")


def test_default_command_is_dry_and_never_resolves_credentials(monkeypatch, capsys):
    mod = smoke()
    monkeypatch.setattr(mod, "run_quick_smoke", lambda **kw: pytest.fail("live execution"))
    assert mod.main([]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["model"] == "space-bunny-free"
    assert len(plan["cases"]) == 3
    assert plan["max_http_requests"] <= 10
    assert plan["max_retries"] == 0
    assert plan["paid_fallback"] is False


def test_live_requires_explicit_free_confirmation_and_key(monkeypatch):
    mod = smoke()
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
    assert mod.main(["--live"]) == 2
    assert mod.main(["--live", "--confirm-free"]) == 2


def test_three_coding_cases_keep_tool_history_and_independent_sessions():
    mod = smoke()
    captured = []
    def opener(req, **kwargs):
        body = json.loads(req.data)
        captured.append((req, body))
        case = mod.CASES[(len(captured) - 1) // 3]
        stage = (len(captured) - 1) % 3
        if stage == 0:
            message = {"tool_calls": [{"id": "read", "type": "function", "function": {
                "name": "read_file", "arguments": json.dumps({"path": "solution.py"})}}]}
        elif stage == 1:
            message = {"tool_calls": [{"id": "write", "type": "function", "function": {
                "name": "write_file", "arguments": json.dumps({
                    "path": "solution.py", "content": case["expected"]})}}]}
        else:
            message = {"content": "Fixed solution.py"}
        return FakeResponse({"model": "space-bunny-free", "choices": [{"message": message,
                             "finish_reason": "tool_calls" if stage < 2 else "stop"}]})
    report = mod.run_quick_smoke(api_key="test-secret", opener=opener)
    assert report["status"] == "passed"
    assert report["http_requests"] == 9
    assert all(case["passed"] for case in report["cases"])
    sessions = [req.get_header("X-opencode-session") for req, _ in captured]
    assert sessions[0] == sessions[1] == sessions[2]
    assert len(set(sessions)) == 3
    for req, body in captured:
        assert req.full_url == mod.GO_BASE_URL + "/chat/completions"
        assert body["model"] == "space-bunny-free"
        assert body["max_tokens"] == 512
        assert req.get_header("User-agent") == "MoTTEavl/0.1.0"
    assert captured[1][1]["messages"][-1]["tool_call_id"] == "read"
    assert captured[2][1]["messages"][-1]["tool_call_id"] == "write"
    assert "test-secret" not in json.dumps(report)


@pytest.mark.parametrize("status", [401, 403, 429, 503, 302])
def test_failure_stops_without_retry_or_fallback(status):
    mod = smoke()
    seen = []
    def opener(req, **kwargs):
        seen.append(req)
        raise HTTPError(req.full_url, status, "test-secret must not appear", {}, None)
    report = mod.run_quick_smoke(api_key="test-secret", opener=opener)
    assert report["status"] == "failed"
    assert len(seen) == report["http_requests"] == 1
    assert len(report["cases"]) == 1
    assert "test-secret" not in json.dumps(report)


def test_send_budget_counts_actual_attempts_and_blocks_eleventh():
    mod = smoke()
    from urllib.request import Request
    seen = []
    sender = mod.BoundedSender(opener=lambda req, **kw: seen.append(req))
    req = Request(mod.GO_BASE_URL + "/chat/completions", method="POST", data=json.dumps({
        "model": "space-bunny-free", "max_tokens": 512}).encode())
    for _ in range(10):
        sender(req, timeout=20)
    with pytest.raises(ValueError, match="request budget"):
        sender(req, timeout=20)
    assert len(seen) == sender.requests == 10


@pytest.mark.parametrize("payload,url", [
    ({"model": "paid-model", "max_tokens": 512}, "https://opencode.ai/zen/go/v1/chat/completions"),
    ({"model": "space-bunny-free", "max_tokens": 9999}, "https://opencode.ai/zen/go/v1/chat/completions"),
    ({"model": "space-bunny-free", "max_tokens": 512}, "https://other.invalid/chat/completions"),
])
def test_sender_rejects_model_endpoint_or_output_expansion(payload, url):
    mod = smoke()
    from urllib.request import Request
    sender = mod.BoundedSender(opener=lambda *a, **k: pytest.fail("sent unsafe request"))
    with pytest.raises(ValueError):
        sender(Request(url, method="POST", data=json.dumps(payload).encode()), timeout=20)
    assert sender.requests == 0


def test_expired_deadline_prevents_any_http_send():
    mod = smoke()
    from urllib.request import Request
    now = [10.0]
    sender = mod.BoundedSender(clock=lambda: now[0],
                               opener=lambda *a, **k: pytest.fail("expired request was sent"))
    now[0] += mod.TOTAL_TIMEOUT
    req = Request(mod.GO_BASE_URL + "/chat/completions", method="POST", data=json.dumps({
        "model": "space-bunny-free", "max_tokens": 512}).encode())
    with pytest.raises(ValueError, match="time budget"):
        sender(req, timeout=20)
    assert sender.requests == 0


def test_socket_timeout_is_clamped_to_remaining_total_budget():
    mod = smoke()
    from urllib.request import Request
    now = [10.0]
    timeouts = []
    sender = mod.BoundedSender(clock=lambda: now[0],
                               opener=lambda req, **kw: timeouts.append(kw["timeout"]))
    now[0] += mod.TOTAL_TIMEOUT - 2
    req = Request(mod.GO_BASE_URL + "/chat/completions", method="POST", data=json.dumps({
        "model": "space-bunny-free", "max_tokens": 512}).encode())
    sender(req, timeout=20)
    assert timeouts == [2.0]


def test_timeout_aborts_following_cases_and_does_not_execute_late_tool(monkeypatch):
    mod = smoke()
    import threading
    release = threading.Event()
    finished = threading.Event()
    seen = []
    def opener(req, **kwargs):
        seen.append(req)
        release.wait(2)
        finished.set()
        return FakeResponse({"model": "space-bunny-free", "choices": [{"message": {
            "tool_calls": [{"id": "late", "type": "function", "function": {
                "name": "write_file", "arguments": json.dumps({
                    "path": "solution.py", "content": mod.CASES[0]["expected"]})}}]},
            "finish_reason": "tool_calls"}]})
    monkeypatch.setattr(mod, "CALL_TIMEOUT", 0.01)
    try:
        report = mod.run_quick_smoke(api_key="test-secret", opener=opener)
        assert report["status"] == "failed"
        assert len(seen) == report["http_requests"] == 1
        assert len(report["cases"]) == 1
        assert report["cases"][0]["termination_reason"] == "per_call_timeout"
        assert report["cases"][0]["tools"] == []
    finally:
        release.set()
        assert finished.wait(2)
    assert report["cases"][0]["tools"] == []


def test_explicit_credentials_profile_uses_existing_resolver_without_environment_fallback(monkeypatch):
    mod = smoke()
    from motte_provider import credentials
    calls = []
    def resolve(profile, *, env_name=None):
        calls.append((profile, env_name))
        return "mock-profile-key"
    monkeypatch.setattr(credentials, "resolve_api_key", resolve)
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "must-not-use-env")
    received = []
    monkeypatch.setattr(mod, "run_quick_smoke", lambda **kw:
                        received.append(kw["api_key"]) or {"status": "passed"})
    assert mod.main(["--live", "--confirm-free", "--credentials", "opencode-go"]) == 0
    assert calls == [("opencode-go", None)]
    assert received == ["mock-profile-key"]


def test_dry_run_never_reads_explicit_credential_profile(monkeypatch):
    mod = smoke()
    from motte_provider import credentials
    monkeypatch.setattr(credentials, "resolve_api_key", lambda *a, **kw:
                        pytest.fail("dry plan accessed credential file"))
    assert mod.main(["--credentials", "opencode-go"]) == 0


def test_missing_explicit_profile_does_not_fallback_or_run(monkeypatch):
    mod = smoke()
    from motte_provider import credentials
    monkeypatch.setattr(credentials, "resolve_api_key", lambda *a, **kw: None)
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "must-not-use-env")
    monkeypatch.setattr(mod, "run_quick_smoke", lambda **kw: pytest.fail("missing profile ran"))
    assert mod.main(["--live", "--confirm-free", "--credentials", "missing"]) == 2


def test_corrupt_profile_error_cannot_echo_credentials(monkeypatch, capsys):
    mod = smoke()
    from motte_provider import credentials
    def fail(*args, **kwargs):
        raise ValueError("malformed private-key-value")
    monkeypatch.setattr(credentials, "resolve_api_key", fail)
    assert mod.main(["--live", "--confirm-free", "--credentials", "opencode-go"]) == 2
    assert "private-key-value" not in capsys.readouterr().err
