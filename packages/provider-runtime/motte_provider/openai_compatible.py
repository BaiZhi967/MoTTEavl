"""openai_compatible 适配器：ModelRequest → /chat/completions → 带计量与 canonical 证据的 envelope。

envelope 是所有入口（Run/CLI/SDK）共享的结果形状：
  content / finish_reason / usage / tool_calls / response_id / usage_details /
  cost / metering / canonical / error；canonical 在落盘前统一脱敏，凭据头永不持久化。
公共流程（计量、成本快照、脱敏、错误证据）在 BaseHTTPProvider。
"""
from __future__ import annotations

from typing import Any

from motte_contracts.messages import Message, ModelRequest, ModelResponse

from .base import BaseHTTPProvider, ProviderCallError, validate_http_provider_config
from .normalization import normalize_finish_reason, normalize_usage_details

__all__ = [
    "BaseHTTPProvider",
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
        messages.extend(message.model_dump(exclude_none=True) for message in request.messages)
        body: dict[str, Any] = {"model": self.model, "messages": messages}
        for name, value in merged.items():
            if value is not None:
                body[_API_NAMES[name]] = value
        if request.tools:
            body["tools"] = list(request.tools)
            if request.tool_choice is not None:
                body["tool_choice"] = request.tool_choice
        return body

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
