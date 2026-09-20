"""Anthropic Messages 适配器：ModelRequest → POST /messages → canonical envelope。

协议差异的处理都在这里：x-api-key + anthropic-version 头（经 transport_kwargs）、
system 顶层参数、content blocks、tool_use/tool_result、必填 max_tokens、
stop_reason 与 usage 的归一化。seed 不被支持 → strict 拒绝。
"""
from __future__ import annotations

import json
from typing import Any

from motte_contracts.messages import Message, ModelRequest, ModelResponse

from .base import BaseHTTPProvider, validate_http_provider_config
from .normalization import normalize_finish_reason, normalize_usage_details

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_OUTPUT_TOKENS = 4096

SUPPORTED_PARAMETERS = {
    "temperature": (0.0, 1.0),
    "top_p": (0.0, 1.0),
    "max_output_tokens": (1, 64_000),
    "stop": None,
}

_API_NAMES = {
    "temperature": "temperature",
    "top_p": "top_p",
    "max_output_tokens": "max_tokens",
    "stop": "stop_sequences",
}

_TOOL_CHOICE_MAP = {
    "auto": {"type": "auto"},
    "none": {"type": "none"},
    "required": {"type": "any"},
}


class AnthropicMessagesProvider(BaseHTTPProvider):
    kind = "anthropic_messages"
    IMPLEMENTATION_VERSION = "1"
    request_path = "/messages"
    SUPPORTED_PARAMETERS = SUPPORTED_PARAMETERS
    _API_NAMES = _API_NAMES

    def build_stream_request_body(self, request: ModelRequest) -> dict[str, Any]:
        body = self.build_request_body(request)
        body["stream"] = True
        return body

    def normalize_stream_event(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Anthropic Messages SSE 事件 → 归一化；未知事件类型 fail closed。

        计量跨事件累积（M4 review R26）：``message_start`` 携带 input/
        cache 计量，``message_delta`` 携带 output 计量——逐事件合并后在
        usage 事件里上报累计值。工具身份（id/name）来自
        ``content_block_start`` 的 tool_use block；真实 stop_reason 来自
        ``message_delta``（``message_stop`` 只是流终止帧，不是停止原因）。
        """
        from .errors import ProviderHTTPError

        event_type = data.get("type")
        if event_type == "content_block_delta":
            delta = data.get("delta") or {}
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                return [{
                    "type": self.STREAM_TEXT_DELTA,
                    "delta": str(delta.get("text") or ""), "payload": {},
                }]
            if delta_type == "input_json_delta":
                return [{
                    "type": self.STREAM_TOOL_DELTA,
                    "delta": str(delta.get("partial_json") or ""),
                    "payload": {"index": int(data.get("index") or 0)},
                }]
            raise ProviderHTTPError(
                f"anthropic stream delta has unknown type: {delta_type!r}",
                error_class="protocol",
            )
        if event_type == "content_block_start":
            block = data.get("content_block") or {}
            if block.get("type") == "tool_use":
                return [{
                    "type": self.STREAM_TOOL_DELTA,
                    "delta": "",
                    "payload": {
                        "index": int(data.get("index") or 0),
                        "id": block.get("id"),
                        "name": block.get("name"),
                    },
                }]
            return []
        if event_type == "message_start":
            message = data.get("message") or {}
            usage = message.get("usage")
            if isinstance(usage, dict) and usage:
                merged = self._merge_stream_usage(usage)
                if merged:
                    return [{
                        "type": self.STREAM_USAGE, "delta": "",
                        "payload": {"usage": dict(merged)},
                    }]
            return []
        if event_type == "message_delta":
            usage = data.get("usage")
            stop_reason = data.get("stop_reason")
            if isinstance(stop_reason, str) and stop_reason:
                self._stream_stop_reason = stop_reason
            events: list[dict[str, Any]] = []
            if isinstance(usage, dict) and usage:
                merged = self._merge_stream_usage(usage)
                if merged:
                    events.append({
                        "type": self.STREAM_USAGE, "delta": "",
                        "payload": {"usage": dict(merged)},
                    })
            return events
        if event_type in ("content_block_stop", "ping"):
            # 已知生命周期/心跳事件：无增量语义，静默通过
            return []
        if event_type == "message_stop":
            return [{
                "type": self.STREAM_FINISH, "delta": "",
                "payload": {"finish_reason": self._stream_stop_reason or "stop"},
            }]
        raise ProviderHTTPError(
            f"anthropic stream event has unknown type: {event_type!r}",
            error_class="protocol",
        )

    def _reset_stream_state(self) -> None:
        self._stream_usage_state = {}
        self._stream_stop_reason = None

    def _merge_stream_usage(self, usage: dict[str, Any]) -> dict[str, Any]:
        """按事件累积计量：input（message_start）+ output（message_delta）。"""
        merged = dict(getattr(self, "_stream_usage_state", None) or {})
        for key in (
            "input_tokens", "output_tokens",
            "cache_read_input_tokens", "cache_creation_input_tokens",
        ):
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                merged[key] = value
        self._stream_usage_state = merged
        return merged

    def build_request_body(self, request: ModelRequest) -> dict[str, Any]:
        merged = self.merged_parameters(request)
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": merged.pop("max_output_tokens", None) or DEFAULT_MAX_OUTPUT_TOKENS,
        }
        if request.system:
            body["system"] = request.system
        body["messages"] = [self._convert_message(message) for message in request.messages]
        for name, value in merged.items():
            if value is None:
                continue
            if name == "stop":
                body["stop_sequences"] = [value] if isinstance(value, str) else list(value)
            else:
                body[_API_NAMES[name]] = value
        if request.tools:
            body["tools"] = [_to_anthropic_tool(tool) for tool in request.tools]
            if request.tool_choice is not None:
                body["tool_choice"] = _to_anthropic_tool_choice(request.tool_choice)
        return body

    @staticmethod
    def _convert_message(message: Message) -> dict[str, Any]:
        """canonical（OpenAI 形状）→ Anthropic 消息；工具回合转 content blocks。"""
        if message.role == "tool":
            result: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id or "",
                "content": message.content if isinstance(message.content, str) else json.dumps(message.content, ensure_ascii=False),
            }
            return {"role": "user", "content": [result]}
        if message.tool_calls:
            blocks: list[dict[str, Any]] = []
            if isinstance(message.content, str) and message.content:
                blocks.append({"type": "text", "text": message.content})
            for call in message.tool_calls:
                function = call.get("function") or {}
                arguments = function.get("arguments") or "{}"
                try:
                    inputs = json.loads(arguments) if isinstance(arguments, str) else arguments
                except ValueError:
                    inputs = {"_raw": arguments}
                blocks.append({"type": "tool_use", "id": call.get("id"), "name": function.get("name"), "input": inputs})
            return {"role": "assistant", "content": blocks}
        return {"role": message.role, "content": message.content}

    def normalize_response(self, data: dict[str, Any]) -> ModelResponse:
        texts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in data.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                texts.append(block.get("text") or "")
            elif block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                })
        usage = data.get("usage") or {}
        canonical_usage = {
            canonical: usage[provider_key]
            for canonical, provider_key in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens"))
            if provider_key in usage
        }
        details = normalize_usage_details(
            cache_read_input_tokens=usage.get("cache_read_input_tokens"),
            cache_creation_input_tokens=_cache_creation_tokens(usage),
        )
        return ModelResponse(
            model=data["model"] if isinstance(data.get("model"), str) else "",
            content="".join(texts),
            finish_reason=normalize_finish_reason(data.get("stop_reason")),
            usage=canonical_usage,
            tool_calls=tool_calls,
            response_id=data.get("id"),
            usage_details=details,
        )


def _cache_creation_tokens(usage: dict[str, Any]) -> int | None:
    """cache_creation 兼容两种形状：整数（旧）或 {ephemeral_5m_input_tokens}（新）。"""
    value = usage.get("cache_creation_input_tokens")
    if value is not None:
        return value
    nested = usage.get("cache_creation")
    if isinstance(nested, dict):
        return nested.get("ephemeral_5m_input_tokens")
    return None


def _to_anthropic_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """canonical（OpenAI function 形状）→ Anthropic {name, description, input_schema}。"""
    if "function" in tool:
        function = tool.get("function") or {}
        return {
            "name": function.get("name"),
            "description": function.get("description"),
            "input_schema": function.get("parameters") or {"type": "object"},
        }
    return {
        "name": tool.get("name"),
        "description": tool.get("description"),
        "input_schema": tool.get("parameters") or tool.get("input_schema") or {"type": "object"},
    }


def _to_anthropic_tool_choice(tool_choice: Any) -> dict[str, Any]:
    if isinstance(tool_choice, str):
        return _TOOL_CHOICE_MAP.get(tool_choice, {"type": "auto"})
    if isinstance(tool_choice, dict):
        function = tool_choice.get("function") or {}
        if tool_choice.get("type") == "function" and function.get("name"):
            return {"type": "tool", "name": function["name"]}
        if "name" in tool_choice:
            return {"type": "tool", "name": tool_choice["name"]}
    return {"type": "auto"}


def validate_config(config: dict[str, Any]) -> None:
    """strict 预检（anthropic_messages）：形状 + 参数表（seed 不支持）。"""
    validate_http_provider_config(config, SUPPORTED_PARAMETERS)
