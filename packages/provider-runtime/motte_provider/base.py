"""HTTP provider 基类：complete 全流程与 envelope 证据的单一来源。

子类提供钩子：kind / request_path / SUPPORTED_PARAMETERS / _API_NAMES /
build_request_body(request) / normalize_response(body)。计量、成本快照、
canonical 脱敏与 ProviderCallError 证据携带全部由基类统一处理。
"""
from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy

from motte_contracts.reasoning import reasoning_patch
from typing import Any, ClassVar

from motte_contracts.messages import ModelRequest, ModelResponse
from motte_trace.redaction import redact

from .capabilities import validate_parameters
from .errors import ProviderError, ProviderHTTPError, classify_exception
from .identity import assess_identity, validate_identity_config
from .pricing import PriceTable, cost_detail, parse_price_table
from .transport import HTTPTransport, TransportOutcome


def validate_output_limit(value: Any) -> int:
    """max_output_tokens 必须是正整数（bool 不算）；adapter 参数表再约束上限区间。"""
    if type(value) is not int or value <= 0:
        raise ValueError("max_output_tokens must be a positive integer")
    return value


def validate_http_provider_config(config: dict[str, Any], supported_parameters: dict[str, Any]) -> None:
    """HTTP provider 通用 strict 预检：形状 + 各自参数表 + 价格表。"""
    for required in ("base_url", "model"):
        if not config.get(required):
            raise ValueError(f"provider config requires {required}")
    params = dict(config.get("parameters") or {})
    ceiling = config.get("max_output_tokens")
    if ceiling is not None:
        validate_output_limit(ceiling)
        params.setdefault("max_output_tokens", ceiling)
        if params["max_output_tokens"] > ceiling:
            raise ValueError(f"max_output_tokens exceeds model ceiling {ceiling}")
    validate_parameters(params, supported_parameters)
    parse_price_table(config.get("price_table"))
    reasoning_patch(config.get("reasoning"), config.get("reasoning_level"))
    validate_identity_config(config.get("identity_policy", "report_only"),
                             config.get("identity_aliases"), config.get("identity_alias_version"))


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
    IMPLEMENTATION_VERSION: ClassVar[str] = "unversioned"

    # M4-T08 流式事件类型（StreamEvent.type 的受控取值）
    STREAM_TEXT_DELTA = "text_delta"
    STREAM_TOOL_DELTA = "tool_delta"
    STREAM_USAGE = "usage"
    STREAM_FINISH = "finish"
    STREAM_ERROR = "error"

    def __init__(
        self,
        transport: HTTPTransport,
        model: str,
        *,
        parameters: dict[str, Any] | None = None,
        price_table: PriceTable | None = None,
        max_output_tokens: int | None = None,
        reasoning: dict[str, Any] | None = None,
        reasoning_level: str | None = None,
        identity_policy: str = "report_only",
        identity_aliases: dict[str, str] | None = None,
        identity_alias_version: str | None = None,
        redactor: Callable[[Any], Any] = redact,
    ) -> None:
        params = dict(parameters or {})
        validate_parameters(params, self.SUPPORTED_PARAMETERS)
        self.identity_policy, aliases, self.identity_alias_version = validate_identity_config(
            identity_policy, identity_aliases, identity_alias_version)
        self.identity_aliases = deepcopy(aliases)
        self.transport = transport
        self.model = model
        self.parameters = params
        self.max_output_tokens = validate_output_limit(max_output_tokens) if max_output_tokens is not None else None
        self.reasoning = deepcopy(reasoning or {})
        self.reasoning_level = reasoning_level
        self.price_table = price_table
        self._redactor = redactor
        self.calls: list[dict[str, Any]] = []

    # ----------------------------------------------------------------- 钩子

    def build_request_body(self, request: ModelRequest) -> dict[str, Any]:
        raise NotImplementedError

    def normalize_response(self, body: dict[str, Any]) -> ModelResponse:
        raise NotImplementedError

    def build_stream_request_body(self, request: ModelRequest) -> dict[str, Any]:
        """流式请求体：默认在完整请求体上加 stream=true（adapter 可覆写）。"""
        return {**self.build_request_body(request), "stream": True}

    def normalize_stream_event(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """把一条 SSE data JSON 归一化为 StreamEvent 列表（adapter 实现）。

        未知协议事件必须 fail closed（抛 ProviderHTTPError(protocol)），
        不允许静默忽略后冒充完整流。
        """
        raise NotImplementedError

    def _reset_stream_state(self) -> None:
        """每次 stream 调用前重置 adapter 内的跨事件累积状态（默认无状态）。"""

    # ----------------------------------------------------------------- 流式

    def stream(self, request: ModelRequest):
        """流式调用：产出 text/tool 增量与终态 usage/finish 事件（M4-T08）。

        - 增量事件：type=text_delta / tool_delta（delta 为文本增量）；
        - 终态事件：usage（payload.usage 原生计量）与 finish（payload 含
          finish_reason / 组装后的完整响应摘要）；
        - **单一 finish**（M4 review R25）：adapter 归一化的原生 terminal
          只登记 finish_reason，最终 finish 由基站在流末统一产出一次——
          正常流不会收到两个 finish；
        - 断流（消费方停止迭代）即取消：连接关闭，无重试；
        - EOF 而无原生 terminal → 显式失败（protocol），不冒充完整流；
        - OpenAI 风格的 ``data: [DONE]`` 帧是传输层哨兵，不是终态事件，
          静默略过（M4 review R24）；
        - 事件顺序：增量 ... usage? finish（usage 缺失时如实省略，不填 0）。
        """
        body = self.build_stream_request_body(request)
        body.update(reasoning_patch(self.reasoning, self.reasoning_level))
        self._reset_stream_state()
        started_any_text: list[str] = []
        tool_fragments: dict[int, dict[str, Any]] = {}
        terminal_usage: dict[str, Any] | None = None
        finish_reason: str | None = None
        chunks = 0
        terminal_seen = False
        try:
            for sse in self.transport.post_sse(self.request_path, body):
                data_text = sse["data"]
                if isinstance(data_text, str) and data_text.strip() == "[DONE]":
                    # 传输层终止哨兵：不是 JSON 事件，也不是 assistant 终态。
                    continue
                try:
                    data = json.loads(data_text)
                except json.JSONDecodeError as error:
                    raise ProviderHTTPError(
                        f"stream chunk is not JSON: {error}", error_class="protocol",
                    ) from error
                if not isinstance(data, dict):
                    raise ProviderHTTPError(
                        "stream chunk must be a JSON object", error_class="protocol",
                    )
                chunks += 1
                for event in self.normalize_stream_event(data):
                    if event["type"] == self.STREAM_TEXT_DELTA:
                        started_any_text.append(event.get("delta") or "")
                    elif event["type"] == self.STREAM_TOOL_DELTA:
                        index = int((event.get("payload") or {}).get("index") or 0)
                        fragment = tool_fragments.setdefault(
                            index, {"arguments": "", "name": None, "id": None},
                        )
                        fragment["arguments"] += event.get("delta") or ""
                        name = (event.get("payload") or {}).get("name")
                        if name:
                            fragment["name"] = name
                        call_id = (event.get("payload") or {}).get("id")
                        if call_id:
                            fragment["id"] = call_id
                    elif event["type"] == self.STREAM_USAGE:
                        terminal_usage = dict((event.get("payload") or {}).get("usage") or {})
                    elif event["type"] == self.STREAM_FINISH:
                        # 原生 terminal 只登记：finish 在流末统一产出一次。
                        terminal_seen = True
                        finish_reason = (event.get("payload") or {}).get("finish_reason")
                        continue
                    yield event
        except ProviderError:
            raise
        except Exception as error:  # noqa: BLE001 - 网络层断流等
            raise ProviderHTTPError(
                f"stream failed: {error}", error_class="network",
            ) from error
        if not terminal_seen:
            raise ProviderHTTPError(
                "stream ended without a native terminal event",
                error_class="protocol",
            )
        yield {
            "type": self.STREAM_FINISH,
            "delta": "",
            "payload": {
                "finish_reason": finish_reason,
                "usage": terminal_usage,
                "text_preview": "".join(started_any_text)[:256],
                "tool_calls": [
                    {
                        "index": index,
                        "id": fragment.get("id"),
                        "name": fragment.get("name"),
                        "arguments": fragment.get("arguments"),
                    }
                    for index, fragment in sorted(tool_fragments.items())
                ],
                "tool_call_count": len(tool_fragments),
                "chunks": chunks,
                "usage_reported": terminal_usage is not None,
            },
        }

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
        # 模型档案 ceiling：canonical top-level（resolve 快照）→ 请求级不得超过；
        # 请求级未给时以 ceiling 作为默认值落入合并参数。
        if self.max_output_tokens is not None:
            requested = merged.get("max_output_tokens")
            if requested is None:
                merged["max_output_tokens"] = self.max_output_tokens
            elif type(requested) is not int or requested <= 0 or requested > self.max_output_tokens:
                raise ValueError(
                    f"max_output_tokens must be a positive integer <= model ceiling {self.max_output_tokens}"
                )
        return merged

    def complete(self, request: ModelRequest) -> dict[str, Any]:
        body = self.build_request_body(request)
        # Final, nonrecursive top-level merge. Validate again immediately before network.
        body.update(reasoning_patch(self.reasoning, self.reasoning_level))
        try:
            outcome = self.transport.post_json_detailed(self.request_path, body)
        except ProviderError as error:
            outcome: TransportOutcome | None = getattr(error, "outcome", None)
            envelope = self._envelope(body, outcome, error=error)
            self.calls.append(envelope)
            raise ProviderCallError(envelope) from error
        envelope = self._envelope(body, outcome, error=None)
        if "error" in envelope:
            # Canonical response is redacted in _envelope; preserve token usage as evidence.
            envelope["tool_calls"] = self._redactor(envelope["tool_calls"])
            self.calls.append(envelope)
            raise ProviderCallError(envelope)
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
            "implementation_version": self.IMPLEMENTATION_VERSION,
            "model": self.model,  # Legacy field: the request model, never a reported identity.
            "requested_model": body["model"],
            "reported_model": None,
            "resolved_model_identity": None,
            "identity_evidence": {
                "source": None,
                "reported_value": None,
                "path": None,
                "alias_map_version": self.identity_alias_version,
                "matched_alias": None,
                "policy": self.identity_policy,
                "details": {},
            },
            "identity_policy": self.identity_policy,
            "identity_policy_result": "not_evaluated",
            "policy_passed": None,
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
        resolved, result, evidence, allowed = assess_identity(
            body["model"], response.model,
            policy=self.identity_policy, aliases=self.identity_aliases,
            alias_version=self.identity_alias_version,
        )
        envelope.update(
            reported_model=response.model if response.model.strip() else None,
            resolved_model_identity=resolved,
            identity_evidence=evidence,
            identity_policy_result=result,
            policy_passed=allowed,
        )
        if not allowed:
            envelope["metering"]["error_class"] = "model_identity"
            envelope["error"] = {
                "class": "model_identity",
                "message": "provider did not report a model identity" if result == "unreported"
                           else "provider model identity does not match the requested model",
            }
        return envelope
