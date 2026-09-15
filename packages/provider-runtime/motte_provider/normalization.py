"""跨协议归一化：finish_reason 与 usage 扩展键的统一映射。

canonical 约定（所有适配器的出口）：
  finish_reason ∈ stop / length / tool_calls / content_filter / refusal / …（未知值原样透传）
  usage         ∈ prompt_tokens / completion_tokens（int）
  usage_details ∈ reasoning_tokens / cache_read_input_tokens / cache_creation_input_tokens（int）
  tool_calls    ∈ [{id, name, arguments(JSON 字符串)}]
"""
from __future__ import annotations

# 各协议原生停止原因 → canonical；不在表内的值原样透传（保留证据）
FINISH_REASON_ALIASES = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "max_output_tokens": "length",
    "tool_use": "tool_calls",
    "function_call": "tool_calls",
    "pause_turn": "pause_turn",
}


def normalize_finish_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    return FINISH_REASON_ALIASES.get(reason, reason)


def normalize_usage_details(
    *,
    cached_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    cache_read_input_tokens: int | None = None,
    cache_creation_input_tokens: int | None = None,
) -> dict[str, int]:
    details: dict[str, int] = {}
    if cached_tokens:
        details["cached_tokens"] = int(cached_tokens)
    if reasoning_tokens:
        details["reasoning_tokens"] = int(reasoning_tokens)
    if cache_read_input_tokens:
        details["cache_read_input_tokens"] = int(cache_read_input_tokens)
    if cache_creation_input_tokens:
        details["cache_creation_input_tokens"] = int(cache_creation_input_tokens)
    return details
