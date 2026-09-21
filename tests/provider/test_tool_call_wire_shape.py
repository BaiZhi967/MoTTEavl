"""验收 F-11：canonical 工具调用必须翻译成 /chat/completions 的 wire 形状。

真实事故（实时模型验收）：M1 的 native-tool Agent 第 1 步模型返回两个工具调用、
两个工具都执行成功，第 2 步把 assistant 的工具调用历史发回 provider 时被严格网关
以 422 invalid_request_error 拒绝：

    Failed to deserialize the JSON body into the target type:
    messages[2]: missing field name at line 1 column 664

原因：归一化层把响应里的 function.name/function.arguments 拍平成 name/arguments
（normalization.py 的 canonical 约定），而请求体要求
tool_calls[].function.{name, arguments}。build_request_body 直接
message.model_dump() 落盘，于是 canonical 形状被原样发回。
"""
from __future__ import annotations

import json

from motte_contracts.messages import Message, ModelRequest
from motte_provider.openai_compatible import OpenAICompatibleProvider, wire_tool_calls
from motte_provider.transport import HTTPTransport


def provider() -> OpenAICompatibleProvider:
    transport = HTTPTransport("https://api.example.test/v1", "sk-test", opener=lambda *a, **k: None)
    return OpenAICompatibleProvider(transport, "test-model")


def canonical_call():
    return {"id": "call-1", "name": "read_file", "arguments": '{"path": "input.json"}'}


def test_canonical_tool_calls_become_the_provider_wire_shape():
    body = provider().build_request_body(ModelRequest(
        model="test-model",
        messages=[
            Message(role="user", content="read input.json"),
            Message(role="assistant", content="", tool_calls=[canonical_call()]),
            Message(role="tool", content="[]", tool_call_id="call-1"),
        ],
    ))

    assistant = body["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["tool_calls"] == [{
        "id": "call-1",
        "type": "function",
        "function": {"name": "read_file", "arguments": '{"path": "input.json"}'},
    }]
    # 工具结果消息的关联键保持原样（OpenAI 用 tool_call_id）
    assert body["messages"][2]["tool_call_id"] == "call-1"


def test_wire_shaped_tool_calls_pass_through_unchanged():
    already = {
        "id": "call-1", "type": "function",
        "function": {"name": "read_file", "arguments": "{}"},
    }
    assert wire_tool_calls([already]) == [already]
    # 幂等：转换过的结果再转换一次不变
    assert wire_tool_calls(wire_tool_calls([canonical_call()])) == wire_tool_calls([canonical_call()])


def test_non_string_arguments_are_serialized_as_json_text():
    """OpenAI 要求 arguments 是字符串；canonical 里可能是对象。"""
    body = provider().build_request_body(ModelRequest(
        model="test-model",
        messages=[Message(role="assistant", content="",
                          tool_calls=[{"id": "c", "name": "list_files",
                                       "arguments": {"prefix": "a"}}])],
    ))

    arguments = body["messages"][0]["tool_calls"][0]["function"]["arguments"]
    assert isinstance(arguments, str)
    assert json.loads(arguments) == {"prefix": "a"}


def test_messages_without_tool_calls_are_untouched():
    body = provider().build_request_body(ModelRequest(
        model="test-model",
        messages=[Message(role="user", content="ping")],
    ))
    assert body["messages"] == [{"role": "user", "content": "ping"}]
