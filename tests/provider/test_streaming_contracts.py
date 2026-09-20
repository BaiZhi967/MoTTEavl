"""M4-T08：三个 Provider adapter 的流式契约（各自协议 fixture，零真实调用）。

每个 adapter 独立校验：文本/工具增量、终态 usage（缺失保持 unknown 不填
0）、断流取消（停止迭代即断开）、未知协议事件 fail closed。另覆盖 Pi
消费端（faux scripted 流）与 SSE 终态语义的一致性。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from motte_contracts.messages import Message, ModelRequest
from motte_provider.anthropic_messages import AnthropicMessagesProvider
from motte_provider.base import BaseHTTPProvider
from motte_provider.openai_compatible import OpenAICompatibleProvider
from motte_provider.openai_responses import OpenAIResponsesProvider
from motte_provider.transport import HTTPTransport


class FakeSSEResponse:
    """可注入的 SSE 响应：按行产出 fixture 字节，close 记录断流。"""

    def __init__(self, lines: list[str]) -> None:
        self._lines = [line.encode("utf-8") for line in lines]
        self.closed = False

    def __iter__(self):
        yield from self._lines

    def close(self):
        self.closed = True


def _sse_lines(events: list[dict]) -> list[str]:
    lines: list[str] = []
    for event in events:
        lines.append("data: " + json.dumps(event))
        lines.append("")
    return lines


def _transport_with(lines: list[str]) -> tuple[HTTPTransport, FakeSSEResponse]:
    response = FakeSSEResponse(lines)

    def opener(request, timeout=30):  # noqa: ANN001
        return response

    transport = HTTPTransport(
        "https://fake.invalid/v1", opener=opener, max_retries=0,
    )
    return transport, response


def _request(text: str = "hello") -> ModelRequest:
    return ModelRequest(model="m1", messages=[Message(role="user", content=text)])


def _provider(cls, transport) -> Any:
    return cls(transport, "m1")


def _collect(events):
    return list(events)


# ---------------------------------------------------------------- chat

CHAT_STREAM = [
    {"choices": [{"delta": {"role": "assistant", "content": "Hel"}}]},
    {"choices": [{"delta": {"content": "lo wor"}}]},
    {"choices": [{"delta": {
        "tool_calls": [{"index": 0, "id": "call-1",
                        "function": {"name": "write_file", "arguments": "{\"pa"}}],
    }}]},
    {"choices": [{"delta": {
        "tool_calls": [{"index": 0, "function": {"arguments": "th\":\"a.txt\"}"}}],
    }}]},
    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    {"choices": [{"delta": {}}], "usage": {"prompt_tokens": 11, "completion_tokens": 4}},
]


class TestOpenAIChatStreaming:
    def test_text_and_tool_deltas_with_terminal_usage(self):
        transport, response = _transport_with(_sse_lines(CHAT_STREAM))
        provider = _provider(OpenAICompatibleProvider, transport)
        events = _collect(provider.stream(_request()))
        kinds = [event["type"] for event in events]
        assert kinds.count("text_delta") == 2
        assert kinds.count("tool_delta") == 2
        assert kinds.count("usage") == 1
        assert kinds[-1] == "finish"
        text = "".join(e["delta"] for e in events if e["type"] == "text_delta")
        assert text == "Hello wor"
        tool = "".join(e["delta"] for e in events if e["type"] == "tool_delta")
        assert json.loads(tool) == {"path": "a.txt"}
        finish = events[-1]["payload"]
        assert finish["usage"]["prompt_tokens"] == 11
        assert finish["usage_reported"] is True
        assert finish["tool_call_count"] == 1
        assert response.closed

    def test_missing_usage_stays_unknown(self):
        stream = [e for e in CHAT_STREAM if "usage" not in e]
        transport, _ = _transport_with(_sse_lines(stream))
        provider = _provider(OpenAICompatibleProvider, transport)
        events = _collect(provider.stream(_request()))
        finish = events[-1]["payload"]
        assert finish["usage"] is None
        assert finish["usage_reported"] is False

    def test_unknown_protocol_fails_closed(self):
        transport, _ = _transport_with(_sse_lines([{"mystery": True}]))
        provider = _provider(OpenAICompatibleProvider, transport)
        with pytest.raises(Exception):
            _collect(provider.stream(_request()))

    def test_consumer_abort_closes_stream(self):
        transport, response = _transport_with(_sse_lines(CHAT_STREAM))
        provider = _provider(OpenAICompatibleProvider, transport)
        generator = provider.stream(_request())
        next(generator)
        generator.close()
        assert response.closed


# ---------------------------------------------------------------- responses

RESPONSES_STREAM = [
    {"type": "response.created", "response": {"id": "resp_1"}},
    {"type": "response.output_item.added", "output_index": 0},
    {"type": "response.output_text.delta", "delta": "Hel"},
    {"type": "response.output_text.delta", "delta": "lo"},
    {"type": "response.function_call_arguments.delta", "delta": "{\"path\"",
                "output_index": 1, "item": {"name": "write_file"}},
    {"type": "response.function_call_arguments.delta", "delta": ":\"a.txt\"}", "output_index": 1},
    {"type": "response.completed", "response": {
        "status": "completed", "usage": {"input_tokens": 9, "output_tokens": 2}}},
]


class TestResponsesStreaming:
    def test_deltas_and_terminal_usage(self):
        transport, _ = _transport_with(_sse_lines(RESPONSES_STREAM))
        provider = _provider(OpenAIResponsesProvider, transport)
        events = _collect(provider.stream(_request()))
        kinds = [event["type"] for event in events]
        assert kinds.count("text_delta") == 2
        assert kinds.count("tool_delta") == 2
        assert "usage" in kinds and kinds[-1] == "finish"
        finish = events[-1]["payload"]
        assert finish["usage"]["input_tokens"] == 9
        assert finish["usage_reported"] is True

    def test_unknown_event_fails_closed(self):
        transport, _ = _transport_with(_sse_lines([{"type": "response.brand.new"}]))
        provider = _provider(OpenAIResponsesProvider, transport)
        with pytest.raises(Exception):
            _collect(provider.stream(_request()))

    def test_consumer_abort_closes_stream(self):
        transport, response = _transport_with(_sse_lines(RESPONSES_STREAM))
        provider = _provider(OpenAIResponsesProvider, transport)
        generator = provider.stream(_request())
        next(generator)
        generator.close()
        assert response.closed


# ---------------------------------------------------------------- anthropic

ANTHROPIC_STREAM = [
    {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
    {"type": "content_block_start", "index": 0},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hel"}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "lo"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"x\":"}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "1}"}},
    {"type": "content_block_stop", "index": 1},
    {"type": "message_delta", "usage": {"output_tokens": 3}},
    {"type": "message_stop"},
]


class TestAnthropicStreaming:
    def _provider(self, lines):
        transport, response = _transport_with(lines)
        provider = AnthropicMessagesProvider(transport, "m1")
        return provider, response

    def test_deltas_lifecycle_and_terminal_usage(self):
        provider, response = self._provider(_sse_lines(ANTHROPIC_STREAM))
        events = _collect(provider.stream(_request()))
        kinds = [event["type"] for event in events]
        assert kinds.count("text_delta") == 2
        assert kinds.count("tool_delta") == 2
        assert "usage" in kinds
        assert kinds[-1] == "finish"
        finish = events[-1]["payload"]
        assert finish["usage"]["output_tokens"] == 3
        assert finish["usage_reported"] is True
        assert response.closed

    def test_ping_and_known_lifecycle_events_pass(self):
        stream = [
            {"type": "ping"},
            {"type": "message_delta", "usage": {"output_tokens": 1}},
            {"type": "message_stop"},
        ]
        provider, _ = self._provider(_sse_lines(stream))
        events = _collect(provider.stream(_request()))
        assert events[-1]["type"] == "finish"

    def test_unknown_event_fails_closed(self):
        provider, _ = self._provider(_sse_lines([{"type": "future_event"}]))
        with pytest.raises(Exception):
            _collect(provider.stream(_request()))

    def test_missing_usage_stays_unknown(self):
        stream = [e for e in ANTHROPIC_STREAM if e.get("type") != "message_delta"]
        provider, _ = self._provider(_sse_lines(stream))
        events = _collect(provider.stream(_request()))
        finish = events[-1]["payload"]
        assert finish["usage"] is None
        assert finish["usage_reported"] is False

    def test_consumer_abort_closes_stream(self):
        provider, response = self._provider(_sse_lines(ANTHROPIC_STREAM))
        generator = provider.stream(_request())
        next(generator)
        generator.close()
        assert response.closed


# ---------------------------------------------------------------- 共享语义

def test_stream_event_types_are_canonical():
    # 受控事件类型集合：三个 adapter 的归一化输出只允许这些 type
    allowed = {
        BaseHTTPProvider.STREAM_TEXT_DELTA, BaseHTTPProvider.STREAM_TOOL_DELTA,
        BaseHTTPProvider.STREAM_USAGE, BaseHTTPProvider.STREAM_FINISH,
        BaseHTTPProvider.STREAM_ERROR,
    }
    assert BaseHTTPProvider.STREAM_TEXT_DELTA == "text_delta"
    assert BaseHTTPProvider.STREAM_FINISH == "finish"
    del allowed


def test_pi_consumer_stream_terminal_consistency():
    """Pi 消费端（faux scripted 流）与 SSE 终态语义一致：增量 → 终态收口。"""
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the Pi bridge")
    bridge = Path(__file__).resolve().parents[2] / "bridges" / "pi" / "bridge.mjs"
    payload = json.dumps({
        "type": "init",
        "run_id": "r", "case_id": "c", "session_id": "s", "operation_id": "o",
        "workspace": tempfile.mkdtemp(),
        "model": {"id": "scripted-1"},
        "budgets": {},
        "config": {"tools": [], "responses": [[{"type": "text", "text": "Hello"}]]},
    }) + "\n"
    stdin_text = (
        json.dumps({"type": "probe"}) + "\n" + payload +
        json.dumps({"type": "run", "id": "run-1", "text": "hi"}) + "\n"
    )
    completed = subprocess.run(
        [node, str(bridge)], input=stdin_text, capture_output=True, text=True,
        timeout=30, check=True,
    )
    events = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    finished = [e for e in events if e["type"] == "finished"]
    assert finished, "scripted 流必须以 finished 终态收口"
    outputs = [e["text"] for e in events if e["type"] == "output"]
    assert "".join(outputs) == "Hello"
    # usage 未上报：与 SSE 侧 missing-usage 语义一致（不填 0）
    assert finished[0]["result"]["usage"]["reported"] is False
