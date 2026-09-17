"""Bounded CEL mapping, using cel-python's parser and interpreter (never Python eval)."""
from __future__ import annotations

import json
import math
from functools import lru_cache
from typing import Any

from celpy import Environment, Runner, celtypes
from lark import Tree

# Old descriptive metadata is readable but is never executable.
LEGACY_CONTROLS = {"reasoning_effort", "reasoning.effort", "thinking"}
FORBIDDEN_KEYS = {
    "model", "messages", "input", "system", "instructions", "tools", "tool_choice", "stream",
    "api_key", "api-key", "x-api-key", "authorization", "auth", "authentication", "headers",
    "credentials", "password", "token", "secret", "cookie", "set-cookie", "proxy-authorization",
    "key", "access_token", "refresh_token", "client_secret", "base_url", "url",
    # Output limits cannot be bypassed by the final CEL merge (including protocol aliases).
    "max_tokens", "max_output_tokens", "max_completion_tokens",
}


@lru_cache(maxsize=128)
def _compile(source: str) -> Runner:
    if not isinstance(source, str) or not source.strip() or len(source.encode()) > 4096:
        raise ValueError("reasoning.control must be a nonempty CEL expression of at most 4096 bytes")
    try:
        env = Environment(annotations={"reasoningLevel": celtypes.StringType})
        tree = env.compile(source)
        stack = [(tree, 0)]
        count = 0
        while stack:
            node, depth = stack.pop()
            count += 1
            if count > 1024 or depth > 96:
                raise ValueError("CEL expression exceeds 1024 AST nodes or depth 96")
            if isinstance(node, Tree):
                # Exclude comprehensions/macros and arbitrary function calls. No external functions
                # are registered. This bounds execution to the finite, bounded expression tree.
                if node.data in {"ident_arg", "member_dot_arg", "member_object"}:
                    raise ValueError("CEL function/method calls and macros are not allowed in mappings")
                if node.data == "ident" and str(node.children[0]) != "reasoningLevel":
                    raise ValueError("only the CEL variable reasoningLevel is available")
                stack.extend((child, depth + 1) for child in node.children)
        return env.program(tree)
    except Exception as error:
        raise ValueError(f"invalid reasoning.control CEL: {error}") from error


def _json_value(value: Any, depth: int, budget: list[int]) -> Any:
    budget[0] -= 1
    if depth > 8 or budget[0] < 0:
        raise ValueError("CEL output exceeds depth 8 or 256 values")
    if isinstance(value, celtypes.MapType):
        result = {}
        for key, child in value.items():
            if not isinstance(key, (str, celtypes.StringType)) or len(key) > 128:
                raise ValueError("CEL output object keys must be strings of at most 128 characters")
            key_text = str(key)
            if key_text.lower() in FORBIDDEN_KEYS:
                raise ValueError(f"CEL patch field {key_text!r} is forbidden (structural/auth/output limit)")
            result[key_text] = _json_value(child, depth + 1, budget)
        return result
    if isinstance(value, celtypes.ListType):
        return [_json_value(child, depth + 1, budget) for child in value]
    if value is None:
        return None
    if isinstance(value, celtypes.BoolType):
        return bool(value)
    if isinstance(value, celtypes.StringType):
        if len(value) > 4096:
            raise ValueError("CEL output string exceeds 4096 characters")
        return str(value)
    if isinstance(value, (celtypes.IntType, celtypes.UintType)):
        return int(value)
    if isinstance(value, celtypes.DoubleType) and math.isfinite(value):
        return float(value)
    raise ValueError("CEL output must contain only JSON-compatible values")


def evaluate_control(source: str, level: str) -> dict[str, Any]:
    if not isinstance(level, str) or not level.strip() or len(level) > 64:
        raise ValueError("reasoningLevel must be a nonempty string of at most 64 characters")
    try:
        result = _compile(source).evaluate({"reasoningLevel": celtypes.StringType(level)})
        if not isinstance(result, celtypes.MapType):
            raise ValueError("CEL expression must return a JSON object")
        patch: dict[str, Any] = _json_value(result, 0, [256])
        if len(json.dumps(patch, allow_nan=False).encode()) > 8192:
            raise ValueError("CEL output exceeds 8192 bytes")
        return patch
    except Exception as error:
        raise ValueError(f"reasoning.control for level {level!r}: {error}") from error


def reasoning_patch(config: dict[str, Any] | None, level: str | None) -> dict[str, Any]:
    from .model import ReasoningProfile

    profile = ReasoningProfile.model_validate(config or {})
    selected = level if level is not None else profile.default_level
    if selected is None:
        return {}
    if not profile.supported or selected not in profile.levels:
        raise ValueError(f"reasoning_level {selected!r} is not supported; choose from {profile.levels}")
    if not profile.control or profile.control in LEGACY_CONTROLS:
        raise ValueError("selected reasoning_level requires a CEL reasoning.control expression, not legacy metadata")
    return evaluate_control(profile.control, selected)
