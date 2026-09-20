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
