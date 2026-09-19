"""native-tool 模式的工具声明与 canonical 工具回合协议。

工具声明使用 OpenAI 兼容 function 形状；消息历史完整保留 assistant
``tool_calls`` 与 tool ``tool_call_id``（canonical 约定见
``motte_contracts.messages``）。
"""
from __future__ import annotations

import json
from typing import Any

READ_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Relative path inside the workspace"},
    },
    "required": ["path"],
    "additionalProperties": False,
}

WRITE_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Relative path inside the workspace"},
        "content": {"type": "string", "description": "Full file content to write"},
    },
    "required": ["path", "content"],
    "additionalProperties": False,
}

LIST_FILES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "prefix": {"type": "string", "description": "Optional relative directory prefix"},
    },
    "additionalProperties": False,
}

TOOL_DECLARATIONS: dict[str, dict[str, Any]] = {
    "read_file": {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file from the case workspace.",
            "parameters": READ_FILE_SCHEMA,
        },
    },
    "write_file": {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a text file in the case workspace.",
            "parameters": WRITE_FILE_SCHEMA,
        },
    },
    "list_files": {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List workspace files under an optional prefix.",
            "parameters": LIST_FILES_SCHEMA,
        },
    },
}

NATIVE_TOOL_NAMES = tuple(sorted(TOOL_DECLARATIONS))


def parse_tool_arguments(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """解析工具参数 JSON；返回 (arguments, error)。畸形参数不抛出，回灌给模型。"""
    if raw is None or raw == "":
        return {}, None
    if isinstance(raw, dict):
        return raw, None
    if not isinstance(raw, str):
        return None, "tool arguments must be a JSON object or string"
    try:
        value = json.loads(raw)
    except ValueError as error:
        return None, f"tool arguments are not valid JSON: {error}"
    if not isinstance(value, dict):
        return None, "tool arguments must decode to a JSON object"
    return value, None


def validate_tool_arguments(
    arguments: dict[str, Any], schema: dict[str, Any],
) -> list[str]:
    """按声明的 parameters 做受限校验（required / additionalProperties / 类型）。"""
    violations: list[str] = []
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    required = schema.get("required")
    required = required if isinstance(required, list) else []
    for name in required:
        if name not in arguments:
            violations.append(f"missing required argument: {name}")
    if schema.get("additionalProperties") is False:
        for name in arguments:
            if name not in properties:
                violations.append(f"unknown argument: {name}")
    type_map = {
        "string": str, "number": (int, float), "integer": int, "boolean": bool,
        "object": dict, "array": list,
    }
    for name, value in arguments.items():
        declaration = properties.get(name)
        if not isinstance(declaration, dict):
            continue
        expected = declaration.get("type")
        if expected not in type_map:
            continue
        ok = isinstance(value, type_map[expected])
        if expected != "boolean" and isinstance(value, bool):
            ok = False  # bool 是 int 子类：数值字段不接受 true/false
        if not ok:
            violations.append(f"argument {name} must be {expected}")
    return violations
