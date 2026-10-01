"""The bounded integration witnesses use real API/Worker paths, fake HTTP only."""
from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest


class FakeResponse:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps({
            "model": "space-bunny-free", "choices": [{"message": self.message,
            "finish_reason": "tool_calls" if self.message.get("tool_calls") else "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }).encode()


@pytest.fixture
def wire(tmp_path, monkeypatch):
    import socket
    import urllib.request

    unexpected_network = []

    def forbid_network(*args, **kwargs):
        unexpected_network.append(True)
        raise AssertionError("offline_test_attempted_unmocked_network")

    # Fail before DNS/connect even if a new transport route bypasses both HTTP mocks.
    monkeypatch.setattr(socket, "create_connection", forbid_network)
    monkeypatch.setattr(socket.socket, "connect", forbid_network)
    monkeypatch.setattr(socket.socket, "connect_ex", forbid_network)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", forbid_network)
    # These are synthetic credentials. Never consult any file or environment resolver.
    monkeypatch.setattr("motte_provider.config.resolve_api_key", lambda *a, **kw: "fake-key")
    calls = []

    def responder(request, **kwargs):
        body = json.loads(request.data)
        calls.append((request, body))
        assert request.full_url == "https://opencode.ai/zen/go/v1/chat/completions"
        assert request.get_header("X-opencode-session")
        assert request.get_header("User-agent") == "MoTTEavl/0.1.0"
        assert body["model"] == "space-bunny-free" and body["max_tokens"] <= 512
        tools = body.get("tools") or []
        if tools:
            prior = [m for m in body["messages"] if m["role"] == "tool"]
            if len(prior) < 2:
                name = "read_file" if not prior else "write_file"
                arguments = {"path": "solution.py"}
                if prior:
                    arguments["content"] = "def add(a, b):\n    return a + b\n"
                return FakeResponse({"content": "", "tool_calls": [{
                    "id": f"call-{len(prior)}", "type": "function", "function": {
                        "name": name, "arguments": json.dumps(arguments)},
                }]})
        return FakeResponse({"content": "return a + b"})

    monkeypatch.setattr("motte_provider.transport._safe_urlopen", responder)
    monkeypatch.setattr("motte_provider.transport._bounded_urlopen", responder)
    ctx = SimpleNamespace(root=tmp_path, provider_config=lambda: {
        "kind": "openai_compatible", "base_url": "https://opencode.ai/zen/go/v1",
        "model": "space-bunny-free", "credentials": "offline-test",
        "timeout": 20, "max_retries": 0, "max_output_tokens": 512,
        "identity_policy": "require_match",
    })
    yield ctx, calls
    assert unexpected_network == []


@pytest.mark.parametrize(("name", "calls_expected"), [
    ("check_api_model", 1),
    ("check_queued_direct", 1),
    ("check_native_agent", 3),
    ("check_scenario_multiturn", 2),
])
def test_witness_real_path_and_no_extra_report_rescore_calls(wire, name, calls_expected):
    ctx, calls = wire
    module = importlib.import_module("scripts.opencode_go_integration_checks")
    result = getattr(module, name)(ctx)
    assert result["ok"] is True
    assert len(calls) == calls_expected
    assert "fake-key" not in json.dumps(result)
    if name != "check_api_model":
        assert result["wait_terminal"] == "completed"
        assert result["sse_reconciled"] is True
        assert result["sse_event_count"] > 0
        assert result["cli_report_equal"] is True
    if name == "check_native_agent":
        assert result["artifact_ast_match"] is True
        assert result["tools"] == ["read_file", "write_file"]
    if name == "check_scenario_multiturn":
        assert result["turns"] == 2
        assert result["persisted_steps"] == ["review", "recall"]
        headers = [request.get_header("X-opencode-session") for request, _ in calls]
        assert len(set(headers)) == 1
        assert len([m for m in calls[1][1]["messages"] if m["role"] == "user"]) == 2
    if name == "check_queued_direct":
        assert result["reopened_queued"] is True
        assert result["replay_completed"] is True
        assert result["replay_source"] == "persisted_live_result"
    if name == "check_api_model":
        assert calls[0][1]["max_tokens"] == 16


def test_api_failure_is_safe_and_not_a_pass(wire, monkeypatch):
    from urllib.error import HTTPError
    ctx, _ = wire

    def fail(request, **kwargs):
        raise HTTPError(request.full_url, 401, "sensitive-provider-body", {}, None)

    monkeypatch.setattr("motte_provider.transport._safe_urlopen", fail)
    monkeypatch.setattr("motte_provider.transport._bounded_urlopen", fail)
    module = importlib.import_module("scripts.opencode_go_integration_checks")
    with pytest.raises(RuntimeError, match="api_model_failed") as error:
        module.check_api_model(ctx)
    assert "sensitive-provider-body" not in str(error.value)


def test_environment_isolates_default_stores_and_restores_them(wire, monkeypatch):
    import os
    ctx, _ = wire
    monkeypatch.setenv("MOTTE_DB_PATH", "original.db")
    module = importlib.import_module("scripts.opencode_go_integration_checks")
    with module._environment(ctx, "isolation") as path:
        assert os.environ["MOTTE_DB_PATH"] == str(path)
        assert os.environ["MOTTE_STORAGE"] == "sqlite"
        for key in ("MOTTE_SKILL_CONTENT_ROOT", "MOTTE_DATASET_DIR"):
            assert os.environ[key].startswith(str(ctx.root))
    assert os.environ["MOTTE_DB_PATH"] == "original.db"
