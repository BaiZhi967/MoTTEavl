"""HTTP provider 基类：complete 全流程与 envelope 证据的单一来源。

子类提供钩子：kind / request_path / SUPPORTED_PARAMETERS / _API_NAMES /
build_request_body(request) / normalize_response(body)。计量、成本快照、
canonical 脱敏与 ProviderCallError 证据携带全部由基类统一处理。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from motte_contracts.messages import ModelRequest, ModelResponse
from motte_trace.redaction import redact

from .capabilities import validate_parameters
from .errors import ProviderError, classify_exception
from .pricing import PriceTable, cost_detail, parse_price_table
from .transport import HTTPTransport, TransportOutcome


def validate_http_provider_config(config: dict[str, Any], supported_parameters: dict[str, Any]) -> None:
    """HTTP provider 通用 strict 预检：形状 + 各自参数表 + 价格表。"""
    for required in ("base_url", "model"):
        if not config.get(required):
            raise ValueError(f"provider config requires {required}")
    validate_parameters(config.get("parameters") or {}, supported_parameters)
    parse_price_table(config.get("price_table"))


class ProviderCallError(ProviderError):
    """携带证据 envelope 的调用失败；RunService 会把 evidence 落入 run error。"""

    def __init__(self, envelope: dict[str, Any]) -> None:
        error = envelope["error"]
        super().__init__(error["message"])
        self.error_class = error["class"]
        self.evidence = envelope


class BaseHTTPProvider:
    kind: ClassVar[str] = ""
    request_path: ClassVar[str] = ""
    SUPPORTED_PARAMETERS: ClassVar[dict[str, Any]] = {}
    _API_NAMES: ClassVar[dict[str, str]] = {}

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
        validate_parameters(params, self.SUPPORTED_PARAMETERS)
        self.transport = transport
        self.model = model
        self.parameters = params
        self.price_table = price_table
        self._redactor = redactor
        self.calls: list[dict[str, Any]] = []

    # ----------------------------------------------------------------- 钩子

    def build_request_body(self, request: ModelRequest) -> dict[str, Any]:
        raise NotImplementedError

    def normalize_response(self, body: dict[str, Any]) -> ModelResponse:
        raise NotImplementedError

    # ----------------------------------------------------------------- 公共流程

    def merged_parameters(self, request: ModelRequest) -> dict[str, Any]:
        """provider 级参数与请求级参数合并（请求级优先），两级都做 strict 校验。"""
        merged: dict[str, Any] = {
            name: self.parameters[name] for name in self.parameters if self.parameters[name] is not None
        }
        request_params = {
            name: value
            for name in self._API_NAMES
            if (value := getattr(request, name, None)) is not None
        }
        validate_parameters(request_params, self.SUPPORTED_PARAMETERS)
        merged.update(request_params)
        return merged

    def complete(self, request: ModelRequest) -> dict[str, Any]:
        body = self.build_request_body(request)
        try:
            outcome = self.transport.post_json_detailed(self.request_path, body)
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
                "request": {"path": self.request_path, "body": self._redactor(body)},
                "response": None,
            },
            "metering": {
                "latency_ms": round(outcome.latency_ms, 3) if outcome else 0.0,
                "attempts": outcome.attempts if outcome else 0,
                "retry_count": max(0, (outcome.attempts if outcome else 1) - 1),
                "error_class": classify_exception(error) if error else None,
            },
        }
        if outcome is not None and isinstance(outcome.response_body, dict):
            envelope["canonical"]["response"] = {
                "status": outcome.status,
                "body": self._redactor(outcome.response_body),
            }
        if error is not None:
            envelope["error"] = {
                "class": classify_exception(error),
                "message": str(error),
            }
            return envelope
        response = self.normalize_response(outcome.response_body or {})
        envelope.update(
            content=response.content,
            finish_reason=response.finish_reason,
            usage=dict(response.usage),
            tool_calls=[dict(call) for call in response.tool_calls],
            response_id=response.response_id,
            usage_details=dict(response.usage_details),
        )
        envelope["cost"] = cost_detail(self.price_table, response.usage)
        return envelope
