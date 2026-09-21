"""M4 review R01：Pi 真实模型传输的离线验证（本地 fake HTTP，无外网）。

bridge 的 http 传输模式经 pi-ai 的真实 provider 分发（``model.api`` +
``baseUrl`` + 环境名传入的 key）。测试在本机 127.0.0.1 起一个 OpenAI
chat-completions 兼容的 SSE 服务器：第一回合回 tool_calls（写文件），
第二回合回文本——验证真实请求/响应/凭据链路确实接通（scripted 只是
另一种显式传输，不再是唯一路径）。
"""
from __future__ import annotations

import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from motte_agent.pi import PiBridgeSession

BRIDGE = Path(__file__).resolve().parents[2] / "bridges" / "pi" / "bridge.mjs"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required for the Pi bridge")


def _sse(*payloads: dict | str) -> bytes:
    chunks = []
    for payload in payloads:
        body = payload if isinstance(payload, str) else json.dumps(payload)
        chunks.append(f"data: {body}\n\n")
    chunks.append("data: [DONE]\n\n")
    return "".join(chunks).encode("utf-8")


_TOOL_CHUNK = {
    "id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 0,
    "model": "fake-model",
    "choices": [{
        "index": 0,
        "delta": {
            "role": "assistant",
            "tool_calls": [{
                "index": 0, "id": "call_1", "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps(
                        {"path": "http-out.txt", "content": "via-real-transport"},
                    ),
                },
            }],
        },
        "finish_reason": None,
    }],
}
_TOOL_DONE = {
    "id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 0,
    "model": "fake-model",
    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
}
_TEXT_CHUNK = {
    "id": "chatcmpl-2", "object": "chat.completion.chunk", "created": 0,
    "model": "fake-model",
    "choices": [{"index": 0, "delta": {"role": "assistant", "content": "all done"},
                 "finish_reason": None}],
}
_TEXT_DONE = {
    "id": "chatcmpl-2", "object": "chat.completion.chunk", "created": 0,
    "model": "fake-model",
    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
}


class _FakeOpenAI(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def do_POST(self):  # noqa: N802 - http.server 接口
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.calls.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": body,
        })
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        # 对话里已出现工具结果（role=tool 或 anthropic 形态 tool_result
        # block）→ 回文本；否则发工具调用。
        has_tool_result = any(
            message.get("role") == "tool" or message.get("tool_call_id")
            or (
                isinstance(message.get("content"), list)
                and any(block.get("type") == "tool_result"
                        for block in message["content"])
            )
            for message in body.get("messages", [])
        )
        payload = _sse(_TEXT_CHUNK, _TEXT_DONE) if has_tool_result \
            else _sse(_TOOL_CHUNK, _TOOL_DONE)
        self.wfile.write(payload)

    def log_message(self, *args):  # noqa: N802 - 静默
        pass


@pytest.fixture()
def fake_openai(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAI)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("MOTTE_PI_TEST_KEY", "test-key-not-a-secret")
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_provider_transport_end_to_end(tmp_path, fake_openai):
    """R01：真实模型传输经本地 fake HTTP 验证——请求、凭据、工具、输出。"""
    session = PiBridgeSession(
        run_id="run-http", case_id="case-http", session_id="sess-http",
        operation_id="op-http", workspace=str(tmp_path / "ws"),
        model_config={"id": "fake-model", "name": "fake-model"},
        responses=None,
        provider_config={
            "api": "openai-completions",
            "provider": "fake",
            "base_url": fake_openai,
            "api_key_env": "MOTTE_PI_TEST_KEY",
        },
        tools=["read_file", "write_file", "list_files"],
        bridge_path=BRIDGE, node_binary=NODE,
        idle_timeout=30, total_timeout=60,
    )
    session.start()
    try:
        assert session.sdk_version, "bridge should report the SDK version"
        result = session.run("write the file")
        assert result["status"] == "completed", result.get("failure")
        assert result["transport"] == "http"
        assert result["final_output"] == "all done"
        # 工具经真实传输往返后落盘。
        assert (tmp_path / "ws" / "http-out.txt").read_text(encoding="utf-8") \
            == "via-real-transport"
        # 请求确实打到 fake 服务器，且携带环境名下发的凭据。
        calls = _FakeOpenAI.calls
        assert len(calls) >= 2, "expected a tool round-trip"
        assert all(call["path"].endswith("/chat/completions") for call in calls)
        assert all(call["auth"] == "Bearer test-key-not-a-secret" for call in calls)
    finally:
        session.close()


def test_http_provider_without_script_or_env_key_fails_at_init(tmp_path, fake_openai, monkeypatch):
    """凭据环境缺失：init 即失败（fail fast），不是 run 中途。"""
    monkeypatch.delenv("MOTTE_PI_TEST_KEY", raising=False)
    from motte_agent.pi import PiBridgeError

    session = PiBridgeSession(
        run_id="run-nokey", case_id="case-nokey", session_id="sess-nokey",
        operation_id="op-nokey", workspace=str(tmp_path / "ws"),
        model_config={"id": "fake-model"},
        provider_config={
            "api": "openai-completions",
            "base_url": fake_openai,
            "api_key_env": "MOTTE_PI_TEST_KEY",
        },
        bridge_path=BRIDGE, node_binary=NODE,
        idle_timeout=10, total_timeout=20,
    )
    with pytest.raises(PiBridgeError) as raised:
        session.start()
    assert raised.value.code == "SESSION_INIT_FAILED"

@pytest.fixture()
def http_case_server(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        replies = []
        calls = 0

        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            index = type(self).calls
            type(self).calls += 1
            status, payload = self.replies[min(index, len(self.replies) - 1)]
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream" if status == 200 else "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setenv("MOTTE_PI_TEST_KEY", "offline-fixture")
    try:
        yield Handler, f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def _http_result(tmp_path, server, replies):
    handler, url = server
    handler.replies = replies
    with PiBridgeSession(
        run_id="r", case_id="c", session_id="s", operation_id="o",
        workspace=tmp_path / "workspace", model_config={"id": "fake-model"},
        provider_config={"api": "openai-completions", "provider": "fake",
                         "base_url": url, "api_key_env": "MOTTE_PI_TEST_KEY"},
        bridge_path=BRIDGE, node_binary=NODE, total_timeout=30,
    ) as session:
        return session.run("offline test")


def _usage_chunk(input_tokens, output_tokens):
    return {"id": "usage", "object": "chat.completion.chunk", "choices": [],
            "usage": {"prompt_tokens": input_tokens, "completion_tokens": output_tokens,
                      "total_tokens": input_tokens + output_tokens}}


def test_native_usage_accumulates_all_model_calls(tmp_path, http_case_server):
    result = _http_result(tmp_path, http_case_server, [
        (200, _sse(_TOOL_CHUNK, _TOOL_DONE, _usage_chunk(11, 3))),
        (200, _sse(_TEXT_CHUNK, _TEXT_DONE, _usage_chunk(17, 5))),
    ])
    assert result["status"] == "completed", result
    assert result["final_output"] == "all done"
    assert result["usage"]["reported"] is True
    assert result["usage"]["input_tokens"] == 28
    assert result["usage"]["output_tokens"] == 8
    assert result["usage"]["total_tokens"] == 36


@pytest.mark.parametrize("usage", [None, (0, 0)])
def test_native_usage_distinguishes_missing_and_reported_zero(tmp_path, http_case_server, usage):
    chunks = [_TEXT_CHUNK, _TEXT_DONE]
    if usage is not None:
        chunks.append(_usage_chunk(*usage))
    result = _http_result(tmp_path, http_case_server, [(200, _sse(*chunks))])
    assert result["status"] == "completed", result
    assert result["final_output"] == "all done"
    assert result["usage"]["reported"] is (usage is not None)
    assert result["usage"]["total_tokens"] == (None if usage is None else 0)


@pytest.mark.parametrize("partial", [False, True])
def test_sdk_error_terminal_is_not_success(tmp_path, http_case_server, partial):
    replies = []
    if partial:
        replies.append((200, _sse(_TEXT_CHUNK, _TOOL_CHUNK, _TOOL_DONE)))
    replies.append((401, json.dumps({"error": {"type": "authentication_error", "message": "invalid test credential"}}).encode()))
    result = _http_result(tmp_path, http_case_server, replies)
    assert result["status"] == "error", result
    assert "401" in result["failure"] or "credential" in result["failure"]
    assert result["usage"]["reported"] is False
    if partial:
        assert result["final_output"] == "all done"


def test_native_usage_does_not_invent_missing_token_fields(tmp_path, http_case_server):
    usage = {"id": "partial-usage", "choices": [], "usage": {"prompt_tokens": 11}}
    result = _http_result(tmp_path, http_case_server, [(200, _sse(_TEXT_CHUNK, _TEXT_DONE, usage))])
    assert result["status"] == "completed"
    assert result["usage"]["reported"] is True
    assert result["usage"]["input_tokens"] == 11
    assert result["usage"]["output_tokens"] is None
    assert result["usage"]["total_tokens"] is None


def test_native_total_only_usage_preserves_observed_value(tmp_path, http_case_server):
    usage = {"id": "total-only", "choices": [], "usage": {"total_tokens": 17}}
    result = _http_result(tmp_path, http_case_server, [(200, _sse(_TEXT_CHUNK, _TEXT_DONE, usage))])
    assert result["status"] == "completed"
    assert result["usage"]["reported"] is True
    assert result["usage"]["total_tokens"] == 17
    assert result["usage"]["input_tokens"] is None
    assert result["usage"]["output_tokens"] is None
