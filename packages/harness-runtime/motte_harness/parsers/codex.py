"""Codex batch（`exec --json`）原生 JSONL 事件 parser（codex-jsonl-v2）。

按 pinned 0.155.1 的 exec JSONL 官方形态解析：事件是顶层 ``type`` 字段
（thread.started / turn.started / item.* / turn.completed / turn.failed /
turn.aborted / error），任务正常以 ``turn.completed`` 终结。partial JSON、
错 thread、turn.failed、exit 后缺 terminal 都有明确结局，不冒充成功
（M4-A07）；usage 只取 turn.completed 原生回报，缺失保持 unknown
（M4-A10）。工具轨迹完整度如实报告（M4 review R13）：事件流完整且无
malformed/unknown 行才标记 complete。
"""
from __future__ import annotations

import json
from typing import Any

PARSER_VERSION = "codex-jsonl-v2"

_TERMINAL_TYPES = frozenset({"turn.completed", "turn.failed", "turn.aborted"})
_ITEM_TYPES = frozenset({
    "agent_message", "command_execution", "file_change", "mcp_tool_call",
    "reasoning", "web_search", "todo_list", "error",
})
_KNOWN_TYPES = frozenset({
    "thread.started", "turn.started", "item.started", "item.updated",
    "item.completed", "error", *_TERMINAL_TYPES,
})


def parse_codex_exec_events(stdout: str, *, raw_ref: str | None = None) -> dict[str, Any]:
    """解析 codex exec --json 的 JSONL 事件流（官方顶层 type 形态）。"""
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
    for event in events:
        msg_type = event.get("type")
        if not isinstance(msg_type, str):
            malformed += 1
            continue
        if msg_type == "thread.started":
            thread_ids.add(str(event.get("thread_id") or ""))
        elif msg_type in ("item.completed", "item.updated"):
            item = event.get("item")
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
            native_usage = event.get("usage")
            if isinstance(native_usage, dict):
                input_tokens = _int_or_none(native_usage.get("input_tokens"))
                output_tokens = _int_or_none(native_usage.get("output_tokens"))
                if input_tokens is not None or output_tokens is not None:
                    usage = {
                        "reported": True,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cache_read_input_tokens": _int_or_none(
                            native_usage.get("cached_input_tokens")
                        ),
                        "cache_write_input_tokens": None,
                    }
            terminal = msg_type
        elif msg_type == "turn.failed":
            error = event.get("error")
            turn_failures.append(
                str(error) if isinstance(error, str) else json.dumps(error)[:512]
            )
            terminal = msg_type
        elif msg_type == "turn.aborted":
            terminal = msg_type
        elif msg_type == "error":
            turn_failures.append(str(event.get("message") or "codex error event")[:512])
        elif msg_type not in _KNOWN_TYPES:
            unknown_types.append(msg_type)

    status: str
    if terminal == "turn.failed":
        status = "error"
    elif turn_failures and terminal is None:
        status = "error"
    elif terminal == "turn.aborted":
        status = "cancelled"
    elif terminal == "turn.completed":
        status = "final"
    else:
        status = "insufficient"

    coverage = "complete"
    if malformed or unknown_types or status == "insufficient":
        coverage = "partial"
    # 工具轨迹域的完整度：完整事件流（无 malformed/unknown）才声称轨迹完整。
    tool_trajectory = "complete" if not malformed and not unknown_types else "partial"

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
        "tool_trajectory": tool_trajectory,
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
        "tool_trajectory": "partial",
        "raw_ref": raw_ref,
    }
