"""OpenAI Responses 适配器：ModelRequest → POST /responses → canonical envelope。

协议差异的处理都在这里：instructions 承载 system、input 消息数组、扁平 function
工具形状、output items 归一化（文本 / function_call）、reasoning 与 cached usage、
status/incomplete_details → finish_reason。seed / stop 不被支持 → strict 拒绝。
"""
from __future__ import annotations

import json
from typing import Any

from motte_contracts.messages import Message, ModelRequest, ModelResponse

from .base import BaseHTTPProvider, validate_http_provider_config
from .normalization import normalize_finish_reason, normalize_usage_details

SUPPORTED_PARAMETERS = {
    "temperature": (0.0, 2.0),
    "top_p": (0.0, 1.0),
    "max_output_tokens": (16, 100_000),
}

_API_NAMES = {
    "temperature": "temperature",
    "top_p": "top_p",
    "max_output_tokens": "max_output_tokens",
}


class OpenAIResponsesProvider(BaseHTTPProvider):
    kind = "openai_responses"
    IMPLEMENTATION_VERSION = "1"
    request_path = "/responses"
    SUPPORTED_PARAMETERS = SUPPORTED_PARAMETERS
    _API_NAMES = _API_NAMES

    _KNOWN_STREAM_EVENTS = frozenset({
        "response.output_text.delta", "response.output_text.done",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.completed", "response.failed", "response.incomplete",
        "response.created", "response.in_progress", "response.output_item.added",
        "response.output_item.done", "response.content_part.added",
        "response.content_part.done",
    })

    def normalize_stream_event(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Responses API 流事件 → 归一化；未知事件类型 fail closed。"""
        from .errors import ProviderHTTPError

        event_type = data.get("type")
        if event_type == "response.output_text.delta":
            return [{
                "type": self.STREAM_TEXT_DELTA,
                "delta": str(data.get("delta") or ""), "payload": {},
            }]
        if event_type == "response.function_call_arguments.delta":
            return [{
                "type": self.STREAM_TOOL_DELTA,
                "delta": str(data.get("delta") or ""),
                "payload": {"index": int(data.get("output_index") or 0),
                            "name": (data.get("item") or {}).get("name")
                            if isinstance(data.get("item"), dict) else None},
            }]
        if event_type == "response.output_item.added":
            # R26：function_call item 的身份（call_id/name）在此事件携带，
            # 参数增量里未必重复——登记为空 delta 的工具身份事件。
            item = data.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                return [{
                    "type": self.STREAM_TOOL_DELTA,
                    "delta": "",
                    "payload": {
                        "index": int(data.get("output_index") or 0),
                        "id": item.get("call_id"),
                        "name": item.get("name"),
                    },
                }]
            return []
        if event_type == "response.completed":
            response = data.get("response") or {}
            usage = response.get("usage") if isinstance(response, dict) else None
            events: list[dict[str, Any]] = []
            if isinstance(usage, dict) and usage:
                events.append({
                    "type": self.STREAM_USAGE, "delta": "",
                    "payload": {"usage": dict(usage)},
                })
            events.append({
                "type": self.STREAM_FINISH, "delta": "",
                "payload": {"finish_reason": response.get("status")
                            if isinstance(response, dict) else "completed"},
            })
            return events
        if event_type == "response.failed":
            raise ProviderHTTPError(
                "responses stream reported response.failed",
                error_class="provider",
            )
        if event_type in self._KNOWN_STREAM_EVENTS:
            return []
        raise ProviderHTTPError(
            f"responses stream event has unknown type: {event_type!r}",
            error_class="protocol",
        )

    def build_request_body(self, request: ModelRequest) -> dict[str, Any]:
        merged = self.merged_parameters(request)
        body: dict[str, Any] = {"model": self.model}
        if request.system:
            body["instructions"] = request.system
        input_items: list[dict[str, Any]] = []
        for message in request.messages:
            input_items.extend(self._convert_message(message))
        body["input"] = input_items
        for name, value in merged.items():
            if value is not None:
                body[_API_NAMES[name]] = value
        if request.tools:
            body["tools"] = [_to_responses_tool(tool) for tool in request.tools]
            if request.tool_choice is not None:
                body["tool_choice"] = _to_responses_tool_choice(request.tool_choice)
        return body

    @staticmethod
    def _convert_message(message: Message) -> list[dict[str, Any]]:
        """canonical（OpenAI chat 形状）→ Responses input items。

        Responses 的 assistant 工具调用是独立的 function_call item，tool 结果是
        function_call_output item；一条 canonical 消息可能展平成多个 items。
        """
        if message.role == "tool":
            content = message.content if isinstance(message.content, str) else json.dumps(message.content, ensure_ascii=False)
            return [{
                "type": "function_call_output",
                "call_id": message.tool_call_id or "",
                "output": content,
            }]
        if message.tool_calls:
            items: list[dict[str, Any]] = []
            if isinstance(message.content, str) and message.content:
                items.append({"role": "assistant", "content": message.content})
            for call in message.tool_calls:
                function = call.get("function") or {}
                items.append({
                    "type": "function_call",
                    "call_id": call.get("id"),
                    "name": function.get("name"),
                    "arguments": function.get("arguments") or "",
                })
            return items
        return [{"role": message.role, "content": message.content}]

    def normalize_response(self, data: dict[str, Any]) -> ModelResponse:
        texts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for output in data.get("output") or []:
            if not isinstance(output, dict):
                continue
            output_type = output.get("type")
            if output_type == "message":
                for part in output.get("content") or []:
                    if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                        texts.append(part.get("text") or "")
            elif output_type == "function_call":
                tool_calls.append({
                    "id": output.get("call_id") or output.get("id"),
                    "name": output.get("name"),
                    "arguments": output.get("arguments") or "",
                })
            elif output_type == "reasoning":
                continue  # reasoning summary 不进入 content；token 计量走 usage_details
        usage = data.get("usage") or {}
        canonical_usage = {
            canonical: usage[provider_key]
            for canonical, provider_key in (
                ("prompt_tokens", "input_tokens"),
                ("completion_tokens", "output_tokens"),
                ("total_tokens", "total_tokens"),
            )
            if provider_key in usage
        }
        input_details = usage.get("input_tokens_details") or {}
        output_details = usage.get("output_tokens_details") or {}
        details = normalize_usage_details(
            cached_tokens=input_details.get("cached_tokens"),
            reasoning_tokens=output_details.get("reasoning_tokens"),
        )
        return ModelResponse(
            model=data["model"] if isinstance(data.get("model"), str) else "",
            content="".join(texts),
            finish_reason=self._finish_reason(data),
            usage=canonical_usage,
            tool_calls=tool_calls,
            response_id=data.get("id"),
            usage_details=details,
        )

    @staticmethod
    def _finish_reason(data: dict[str, Any]) -> str | None:
        if data.get("status") == "incomplete":
            reason = (data.get("incomplete_details") or {}).get("reason")
            if reason == "max_output_tokens":
                return "length"
            return normalize_finish_reason(reason)
        return "stop" if data.get("status") == "completed" else normalize_finish_reason(data.get("status"))


def _to_responses_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """canonical（OpenAI chat 嵌套 function 形状）→ Responses 扁平 function 形状。"""
    if "function" in tool:
        function = tool.get("function") or {}
        return {
            "type": "function",
            "name": function.get("name"),
            "description": function.get("description"),
            "parameters": function.get("parameters") or {"type": "object"},
        }
    return {
        "type": tool.get("type", "function"),
        "name": tool.get("name"),
        "description": tool.get("description"),
        "parameters": tool.get("parameters") or {"type": "object"},
    }


def _to_responses_tool_choice(tool_choice: Any) -> Any:
    if isinstance(tool_choice, str):
        return tool_choice  # auto / none / required 在 Responses 同名
    if isinstance(tool_choice, dict):
        function = tool_choice.get("function") or {}
        if tool_choice.get("type") == "function" and function.get("name"):
            return {"type": "function", "name": function["name"]}
    return tool_choice


def validate_config(config: dict[str, Any]) -> None:
    """strict 预检（openai_responses）：形状 + 参数表（seed / stop 不支持）。"""
    validate_http_provider_config(config, SUPPORTED_PARAMETERS)
