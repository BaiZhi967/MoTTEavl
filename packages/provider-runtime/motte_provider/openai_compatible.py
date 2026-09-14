"""openai_compatible 真适配器：ModelRequest → /chat/completions → 带计量与 canonical 证据的 envelope。

envelope 是所有入口（Run/CLI/SDK）共享的结果形状：
  content / finish_reason / usage / cost / metering / canonical / error
canonical 在落盘前统一脱敏，Authorization 头永不持久化。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from motte_contracts.messages import Message, ModelRequest
from motte_trace.redaction import redact

from .capabilities import validate_parameters
from .errors import ProviderError, classify_exception
from .openai_chat import normalize_response
from .pricing import PriceTable, cost_detail
from .transport import HTTPTransport, TransportOutcome

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


class ProviderCallError(ProviderError):
    """携带证据 envelope 的调用失败；RunService 会把 evidence 落入 run error。"""

    def __init__(self, envelope: dict[str, Any]) -> None:
        error = envelope["error"]
        super().__init__(error["message"])
        self.error_class = error["class"]
        self.evidence = envelope


class OpenAICompatibleProvider:
    kind = "openai_compatible"

    def __init__(
        self,
        transport: HTTPTransport,
        model: str,
        *,
        parameters: dict[str, Any] | None = None,
        price_table: PriceTable | None = None,
        redactor: Callable[[Any], Any] = redact,
    ) -> None:
        params = dict(parameters or {})
        validate_parameters(params, SUPPORTED_PARAMETERS)
        self.transport = transport
        self.model = model
        self.parameters = params
        self.price_table = price_table
        self._redactor = redactor
        self.calls: list[dict[str, Any]] = []

    def build_request_body(self, request: ModelRequest) -> dict[str, Any]:
        """provider 级参数与请求级参数合并（请求级优先），构造 /chat/completions body。"""
        merged: dict[str, Any] = {
            name: self.parameters[name] for name in self.parameters if self.parameters[name] is not None
        }
        request_params = {
            name: value
            for name in _API_NAMES
            if (value := getattr(request, name, None)) is not None
        }
        validate_parameters(request_params, SUPPORTED_PARAMETERS)
        merged.update(request_params)
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.extend(message.model_dump() for message in request.messages)
        body: dict[str, Any] = {"model": self.model, "messages": messages}
        for name, value in merged.items():
            if value is not None:
                body[_API_NAMES[name]] = value
        return body

    def complete(self, request: ModelRequest) -> dict[str, Any]:
        body = self.build_request_body(request)
        try:
            outcome = self.transport.post_json_detailed("/chat/completions", body)
        except ProviderError as error:
            outcome: TransportOutcome | None = getattr(error, "outcome", None)
            envelope = self._envelope(body, outcome, error=error)
            self.calls.append(envelope)
            raise ProviderCallError(envelope) from error
        envelope = self._envelope(body, outcome, error=None)
        self.calls.append(envelope)
        return envelope

    def _envelope(
        self,
        body: dict[str, Any],
        outcome: TransportOutcome | None,
        *,
        error: Exception | None,
    ) -> dict[str, Any]:
        envelope: dict[str, Any] = {
            "provider": self.kind,
            "model": self.model,
            "canonical": {
                "request": {"path": "/chat/completions", "body": self._redactor(body)},
                "response": None,
            },
            "metering": {
                "latency_ms": round(outcome.latency_ms, 3) if outcome else 0.0,
                "attempts": outcome.attempts if outcome else 0,
                "retry_count": max(0, (outcome.attempts if outcome else 1) - 1),
                "error_class": classify_exception(error) if error else None,
            },
        }
        if error is not None:
            envelope["error"] = {
                "class": classify_exception(error),
                "message": str(error),
            }
            return envelope
        response = normalize_response(outcome.response_body or {})
        envelope.update(
            content=response.content,
            finish_reason=response.finish_reason,
            usage=dict(response.usage),
        )
        envelope["cost"] = cost_detail(self.price_table, response.usage)
        envelope["canonical"]["response"] = {
            "status": outcome.status,
            "body": self._redactor(outcome.response_body),
        }
        return envelope


class CaseDrivenProvider:
    """把 manifest 中的 case（prompt/system/expected）映射为 ModelRequest 的服务端 provider。"""

    def __init__(self, provider: OpenAICompatibleProvider, cases: dict[str, dict[str, Any]]) -> None:
        self.provider = provider
        self.cases = cases

    def invoke(self, case_id: str) -> dict[str, Any]:
        case = self.cases.get(case_id)
        if case is None:
            raise KeyError(case_id)
        request = ModelRequest(
            model=self.provider.model,
            messages=[Message(role="user", content=case.get("prompt", ""))],
            system=case.get("system"),
        )
        return self.provider.complete(request)

    def expected_for(self, case_id: str) -> Any:
        return self.cases.get(case_id, {}).get("expected")
