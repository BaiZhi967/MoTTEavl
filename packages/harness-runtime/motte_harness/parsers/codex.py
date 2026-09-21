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


def _valid_tool_result(kind: str, item: dict[str, Any]) -> bool:
    if kind == "web_search":
        return isinstance(item.get("query"), str)
    if not isinstance(item.get("status"), str) or item["status"] not in {"completed", "failed"}:
        return False
    if kind == "command_execution":
        return isinstance(item.get("command"), str) and type(item.get("exit_code")) is int
    if kind == "file_change":
        changes = item.get("changes")
        return isinstance(changes, list) and all(
            isinstance(change, dict) and isinstance(change.get("path"), str)
            and isinstance(change.get("kind"), str)
            and change["kind"] in {"add", "delete", "update"} for change in changes
        )
    return (
        isinstance(item.get("server"), str) and isinstance(item.get("tool"), str)
        and "arguments" in item
    )


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
    tools: dict[str, dict[str, Any]] = {}
    unfinished_tools: set[str] = set()
    usage: dict[str, Any] = {"reported": False, "input_tokens": None, "output_tokens": None}
    turn_failures: list[str] = []
    terminal: str | None = None
    lifecycle_errors: list[str] = []
    turns_started = 0
    for event in events:
        msg_type = event.get("type")
        if not isinstance(msg_type, str):
            malformed += 1
            continue
        if terminal is not None:
            lifecycle_errors.append("event_after_terminal")
        event_thread = event.get("thread_id")
        if event_thread is not None:
            if not isinstance(event_thread, str) or not event_thread:
                lifecycle_errors.append("invalid_thread_identity")
            else:
                thread_ids.add(event_thread)
        if msg_type == "thread.started":
            if not isinstance(event_thread, str) or not event_thread:
                lifecycle_errors.append("missing_thread_identity")
        elif msg_type == "turn.started":
            turns_started += 1
            if turns_started > 1:
                lifecycle_errors.append("multiple_batch_turns")
        elif msg_type in ("item.started", "item.completed", "item.updated"):
            item = event.get("item")
            if (not isinstance(item, dict) or not isinstance(item.get("type"), str)
                    or item["type"] not in _ITEM_TYPES):
                unknown_types.append(f"item:{item.get('type') if isinstance(item, dict) else 'invalid'}")
                continue
            kind = item["type"]
            if kind == "agent_message" and isinstance(item.get("text"), str):
                messages.append(item["text"])
            elif kind in {"command_execution", "file_change", "mcp_tool_call", "web_search"}:
                call_id = item.get("id")
                if not isinstance(call_id, str) or not call_id:
                    malformed += 1
                    call_id = f"unidentified-{len(tools)}"
                previous = tools.get(call_id) or {}
                native = {**previous.get("native_item", {}), **item}
                complete = msg_type == "item.completed"
                if complete:
                    unfinished_tools.discard(call_id)
                else:
                    unfinished_tools.add(call_id)
                fields = {
                    "command_execution": ("command", "cwd"),
                    "file_change": ("changes",),
                    "mcp_tool_call": ("server", "tool", "arguments"),
                    "web_search": ("query",),
                }[kind]
                status = "unknown"
                valid_result = _valid_tool_result(kind, native) if complete else False
                if complete and not valid_result:
                    malformed += 1
                if native.get("status") == "failed" or (
                    kind == "command_execution" and native.get("exit_code") not in (None, 0)
                ):
                    status = "failed"
                elif complete and valid_result:
                    status = "succeeded"
                tools[call_id] = {
                    "call_id": call_id, "tool_name": kind,
                    "arguments": {key: native.get(key) for key in fields},
                    "status": status, "step": previous.get("step", len(tools) + 1),
                    "native_item": native,
                }
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
    if len(thread_ids) > 1:
        lifecycle_errors.append("crossed_thread_identity")
    if turn_failures or terminal == "turn.failed":
        status = "error"
    elif lifecycle_errors:
        status = "insufficient"
    elif terminal == "turn.aborted":
        status = "cancelled"
    elif terminal == "turn.completed":
        status = "final"
    else:
        status = "insufficient"

    commands = [
        {key: call["native_item"].get(key) for key in ("command", "cwd", "exit_code", "status")}
        for call in tools.values() if call["tool_name"] == "command_execution"
    ]
    coverage = "complete"
    if malformed or unknown_types or unfinished_tools or lifecycle_errors or status == "insufficient":
        coverage = "partial"
    # 工具轨迹域的完整度：完整事件流（无 malformed/unknown）才声称轨迹完整。
    tool_trajectory = coverage

    return {
        "parser_version": PARSER_VERSION,
        "status": status,
        "thread_ids": sorted(thread_id for thread_id in thread_ids if thread_id),
        "final_output": messages[-1] if messages else None,
        "messages": messages,
        "commands": commands,
        "tool_calls": list(tools.values()),
        "usage": usage,
        "cost_usd": None,  # exec JSONL 不回报费用：保持 unknown，不填 0
        "model": None,     # 模型来自配置，不在流中回报：不冒充 observed
        "turn_failures": turn_failures,
        "lifecycle_errors": sorted(set(lifecycle_errors)),
        "malformed_lines": malformed,
        "unknown_msg_types": unknown_types,
        "coverage": coverage,
        "tool_trajectory": tool_trajectory,
        "raw_ref": raw_ref,
    }


def _int_or_none(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _insufficient(reason: str, raw_ref: str | None) -> dict[str, Any]:
    return {
        "parser_version": PARSER_VERSION,
        "status": "insufficient",
        "reason": reason,
        "thread_ids": [],
        "final_output": None,
        "messages": [],
        "commands": [],
        "tool_calls": [],
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
