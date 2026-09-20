"""Codex batch（`exec --json`）原生 JSONL 事件 parser。

按 pinned 0.155.1 的 exec JSONL 形态解析 thread/turn/item 事件；partial
JSON、错 thread、turn.failed、exit=0 缺 terminal 都有明确结局，不冒充
成功（M4-A07）；usage 只取 turn.completed 原生回报，缺失保持 unknown
（M4-A10）。
"""
from __future__ import annotations

import json
from typing import Any

PARSER_VERSION = "codex-jsonl-v1"

_TERMINAL_MSG_TYPES = frozenset({"thread.completed", "thread.failed"})
_ITEM_TYPES = frozenset({"agent_message", "command_execution", "file_change", "mcp_tool_call", "reasoning", "web_search"})
_KNOWN_MSG_TYPES = frozenset({
    "thread.started", "turn.started", "turn.completed", "turn.failed",
    "item.started", "item.updated", "item.completed", "error",
    *_TERMINAL_MSG_TYPES,
})


def parse_codex_exec_events(stdout: str, *, raw_ref: str | None = None) -> dict[str, Any]:
    """解析 codex exec --json 的 JSONL 事件流。"""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return _insufficient("empty_output", raw_ref)
    events: list[dict[str, Any]] = []
    malformed = 0
    unknown_types: list[str] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            events.append({"_malformed": True, "raw": line[:512]})
            continue
        if isinstance(event, dict):
            events.append(event)
        else:
            malformed += 1
    thread_ids: set[str] = set()
    messages: list[str] = []
    commands: list[dict[str, Any]] = []
    usage: dict[str, Any] = {"reported": False, "input_tokens": None, "output_tokens": None}
    turn_failures: list[str] = []
    terminal: str | None = None
    seq = 0
    for event in events:
        if event.get("_malformed"):
            continue
        message = event.get("msg")
        if not isinstance(message, dict):
            malformed += 1
            continue
        seq += 1
        msg_type = message.get("type")
        if msg_type == "thread.started":
            thread_ids.add(str(message.get("thread_id") or ""))
        elif msg_type in ("item.completed", "item.updated"):
            item = message.get("item")
            if isinstance(item, dict) and item.get("type") in _ITEM_TYPES:
                if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                    messages.append(item["text"])
                elif item.get("type") == "command_execution":
                    commands.append({
                        "command": item.get("command"),
                        "cwd": item.get("cwd"),
                        "exit_code": item.get("exit_code"),
                        "status": item.get("status"),
                    })
        elif msg_type == "turn.completed":
            native_usage = message.get("usage")
            if isinstance(native_usage, dict):
                input_tokens = _int_or_none(native_usage.get("input_tokens"))
                output_tokens = _int_or_none(native_usage.get("output_tokens"))
                if input_tokens is not None or output_tokens is not None:
                    usage = {
                        "reported": True,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cache_read_input_tokens": _int_or_none(
                            native_usage.get("cache_read_input_tokens")
                        ),
                        "cache_write_input_tokens": _int_or_none(
                            native_usage.get("cache_write_input_tokens")
                        ),
                    }
        elif msg_type == "turn.failed":
            error = message.get("error")
            turn_failures.append(
                str(error) if isinstance(error, str) else json.dumps(error)[:512]
            )
        elif msg_type in _TERMINAL_MSG_TYPES:
            terminal = str(msg_type)
        elif msg_type == "error":
            turn_failures.append(str(message.get("message") or "codex error event")[:512])
        elif msg_type not in _KNOWN_MSG_TYPES:
            unknown_types.append(str(msg_type))

    status: str
    if terminal == "thread.failed":
        status = "error"
    elif turn_failures and terminal is None:
        status = "error"
    elif terminal == "thread.completed":
        status = "final"
    else:
        status = "insufficient"

    coverage = "complete"
    if malformed or unknown_types or status == "insufficient":
        coverage = "partial"

    return {
        "parser_version": PARSER_VERSION,
        "status": status,
        "thread_ids": sorted(thread_id for thread_id in thread_ids if thread_id),
        "final_output": messages[-1] if messages else None,
        "messages": messages,
        "commands": commands,
        "usage": usage,
        "cost_usd": None,  # exec JSONL 不回报费用：保持 unknown，不填 0
        "model": None,     # 模型来自配置，不在流中回报：不冒充 observed
        "turn_failures": turn_failures,
        "malformed_lines": malformed,
        "unknown_msg_types": unknown_types,
        "coverage": coverage,
        "raw_ref": raw_ref,
    }


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _insufficient(reason: str, raw_ref: str | None) -> dict[str, Any]:
    return {
        "parser_version": PARSER_VERSION,
        "status": "insufficient",
        "reason": reason,
        "thread_ids": [],
        "final_output": None,
        "messages": [],
        "commands": [],
        "usage": {"reported": False, "input_tokens": None, "output_tokens": None},
        "cost_usd": None,
        "model": None,
        "turn_failures": [],
        "malformed_lines": 0,
        "unknown_msg_types": [],
        "coverage": "partial",
        "raw_ref": raw_ref,
    }
