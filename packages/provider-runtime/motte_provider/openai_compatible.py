"""openai_compatible 适配器：ModelRequest → /chat/completions → 带计量与 canonical 证据的 envelope。

envelope 是所有入口（Run/CLI/SDK）共享的结果形状：
  content / finish_reason / usage / tool_calls / response_id / usage_details /
  cost / metering / canonical / error；canonical 在落盘前统一脱敏，凭据头永不持久化。
公共流程（计量、成本快照、脱敏、错误证据）在 BaseHTTPProvider。
"""
from __future__ import annotations

import json
from typing import Any

from motte_contracts.messages import Message, ModelRequest, ModelResponse

from .base import BaseHTTPProvider, ProviderCallError, validate_http_provider_config
from .normalization import normalize_finish_reason, normalize_usage_details

__all__ = [
    "BaseHTTPProvider",
    "wire_tool_calls",
    "CaseDrivenProvider",
    "OpenAICompatibleProvider",
    "ProviderCallError",
    "SUPPORTED_PARAMETERS",
    "validate_config",
]

SUPPORTED_PARAMETERS = {
    "temperature": (0.0, 2.0),
    "top_p": (0.0, 1.0),
    "max_output_tokens": (1, 1_000_000),
    "seed": (0, 2**31 - 1),
    "stop": None,
}

_API_NAMES = {
    "temperature": "temperature",
    "top_p": "top_p",
    "max_output_tokens": "max_tokens",
    "seed": "seed",
    "stop": "stop",
}


def wire_tool_calls(tool_calls: Any) -> list[dict[str, Any]]:
    """canonical 工具调用 → /chat/completions 的 wire 形状（验收 F-11）。

    canonical 与 wire 不是同一个东西：归一化层把响应里的
    function.name / function.arguments 拍平成 name / arguments
    （normalization.py），而请求体要求 tool_calls[].function.{name,arguments}。
    把 canonical 原样塞回历史会被严格网关以 422 invalid_request_error
    "missing field name" 拒绝——实测第二次模型调用即失败，真实 Agent 循环根本
    走不完一轮工具调用。

    已经是 wire 形状的项原样通过，因此对已转换过的历史是幂等的。
    """
    wire: list[dict[str, Any]] = []
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        if isinstance(call.get("function"), dict):
            wire.append(dict(call))
            continue
        arguments = call.get("arguments")
        if not isinstance(arguments, str):
            arguments = json.dumps(
                {} if arguments is None else arguments, ensure_ascii=False,
            )
        wire.append({
            "id": call.get("id"),
            "type": call.get("type") or "function",
            "function": {"name": call.get("name"), "arguments": arguments},
        })
    return wire


class OpenAICompatibleProvider(BaseHTTPProvider):
    kind = "openai_compatible"
    IMPLEMENTATION_VERSION = "1"
    request_path = "/chat/completions"
    SUPPORTED_PARAMETERS = SUPPORTED_PARAMETERS
    _API_NAMES = _API_NAMES

    def build_request_body(self, request: ModelRequest) -> dict[str, Any]:
        """provider 级参数与请求级参数合并（请求级优先），构造 /chat/completions body。"""
        merged = self.merged_parameters(request)
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        for message in request.messages:
            payload = message.model_dump(exclude_none=True)
            if payload.get("tool_calls"):
                payload["tool_calls"] = wire_tool_calls(payload["tool_calls"])
            messages.append(payload)
        body: dict[str, Any] = {"model": self.model, "messages": messages}
        for name, value in merged.items():
            if value is not None:
                body[_API_NAMES[name]] = value
        if request.tools:
            body["tools"] = list(request.tools)
            if request.tool_choice is not None:
                body["tool_choice"] = request.tool_choice
        return body

    def normalize_stream_event(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """chat.completions 流块 → 归一化事件；未知形状 fail closed（M4-T08）。"""
        from .errors import ProviderHTTPError

        if not isinstance(data.get("choices"), list):
            # 终态 usage 块：choices 为空数组，usage 在顶层（include_usage）
            if isinstance(data.get("usage"), dict) and data["usage"]:
                return [{
                    "type": self.STREAM_USAGE, "delta": "",
                    "payload": {"usage": dict(data["usage"])},
                }]
            raise ProviderHTTPError(
                "openai chat stream chunk has unknown shape", error_class="protocol",
            )
        events: list[dict[str, Any]] = []
        if isinstance(data.get("usage"), dict) and data["usage"]:
            events.append({
                "type": self.STREAM_USAGE, "delta": "",
                "payload": {"usage": dict(data["usage"])},
            })
        choice = data["choices"][0] if data["choices"] else {}
        if not isinstance(choice, dict):
            raise ProviderHTTPError(
                "openai chat stream choice has unknown shape", error_class="protocol",
            )
        delta = choice.get("delta") or {}
        if isinstance(delta.get("content"), str) and delta["content"]:
            events.append({
                "type": self.STREAM_TEXT_DELTA, "delta": delta["content"], "payload": {},
            })
        for index, call in enumerate(delta.get("tool_calls") or []):
            if not isinstance(call, dict):
                raise ProviderHTTPError(
                    "openai chat tool delta has unknown shape", error_class="protocol",
                )
            function = call.get("function") or {}
            events.append({
                "type": self.STREAM_TOOL_DELTA,
                "delta": str(function.get("arguments") or ""),
                "payload": {
                    "index": int(call.get("index") or index),
                    "id": call.get("id"),
                    "name": function.get("name"),
                },
            })
        if choice.get("finish_reason"):
            events.append({
                "type": self.STREAM_FINISH, "delta": "",
                "payload": {"finish_reason": choice.get("finish_reason")},
            })
        return events

    def normalize_response(self, data: dict[str, Any]) -> ModelResponse:
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        tool_calls = [
            {
                "id": call.get("id"),
                "name": (call.get("function") or {}).get("name"),
                "arguments": (call.get("function") or {}).get("arguments") or "",
            }
            for call in message.get("tool_calls") or []
        ]
        usage = data.get("usage") or {}
        canonical_usage = {
            key: usage[key]
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if key in usage
        }
        details = _usage_details(usage)
        return ModelResponse(
            model=data["model"] if isinstance(data.get("model"), str) else "",
            content=message.get("content") or "",
            finish_reason=normalize_finish_reason(choice.get("finish_reason")),
            usage=canonical_usage,
            tool_calls=tool_calls,
            response_id=data.get("id"),
            usage_details=details,
        )


def _usage_details(usage: dict[str, Any]) -> dict[str, int]:
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    return normalize_usage_details(
        cached_tokens=prompt_details.get("cached_tokens"),
        reasoning_tokens=completion_details.get("reasoning_tokens"),
    )


class CaseDrivenProvider:
    """把 manifest 中的 case 映射为 ModelRequest 的服务端 provider。

    case 形状：prompt（或完整 messages 历史，含工具回合）、system、tools（覆盖
    manifest 级）、tool_choice、expected。
    """

    def __init__(
        self,
        provider: BaseHTTPProvider,
        cases: dict[str, dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> None:
        self.provider = provider
        self.cases = cases
        self.tools = list(tools or [])

    def invoke(self, case_id: str) -> dict[str, Any]:
        case = self.cases.get(case_id)
        if case is None:
            raise KeyError(case_id)
        if case.get("messages"):
            messages = [Message.model_validate(message) for message in case["messages"]]
        else:
            messages = [Message(role="user", content=case.get("prompt", ""))]
        request = ModelRequest(
            model=self.provider.model,
            messages=messages,
            system=case.get("system"),
            tools=list(case.get("tools") or self.tools),
            tool_choice=case.get("tool_choice"),
        )
        return self.provider.complete(request)

    def expected_for(self, case_id: str) -> Any:
        return self.cases.get(case_id, {}).get("expected")


def validate_config(config: dict[str, Any]) -> None:
    """strict 预检（openai_compatible）：形状 + 参数表 + 价格表。"""
    validate_http_provider_config(config, SUPPORTED_PARAMETERS)
