"""M1-T05：消息/工具循环——完整历史、call_id、参数校验、预算与取消。

验收对应：A02（错误回灌恢复）、A03（未知工具拒绝）、A05（预算阻断）、
A13（无 usage 时不虚构硬预算）。
"""
from __future__ import annotations

import json

import pytest

from motte_agent.budget import ExecutionBudget
from motte_agent.builtin_react import BuiltinReActRuntime, PROMPT_VERSIONS


def scripted_complete(responses):
    requests = []
    queue = list(responses)

    def complete(request):
        requests.append(request)
        if not queue:
            raise AssertionError("scripted provider exhausted")
        return dict(queue.pop(0))

    complete.requests = requests
    return complete


def native_call(call_id, name, arguments):
    return {"id": call_id, "name": name, "arguments": json.dumps(arguments)}


def test_assistant_tool_history_and_duplicate_ids():
    """native 模式：第二轮请求必须包含第一轮 assistant(tool_calls)+tool 记录；
    重复 call_id 不产生二次副作用。"""
    side_effects = []

    def write_file(arguments):
        side_effects.append(arguments["path"])
        return f"wrote {arguments['path']}"

    complete = scripted_complete([
        {"content": "", "tool_calls": [
            native_call("call-1", "write_file", {"path": "a.txt", "content": "A"}),
            native_call("call-1", "write_file", {"path": "b.txt", "content": "B"}),  # 重复 ID
        ]},
        {"content": "done", "tool_calls": []},
    ])
    runtime = BuiltinReActRuntime(
        complete, {"write_file": write_file}, mode="native-tool", model="test-model",
        declared_tools=("write_file",),
    )
    outcome = runtime.run_agent("write files")
    assert outcome["termination_reason"] == "final_answer"
    # 重复 call_id 只执行一次
    assert side_effects == ["a.txt"]
    assert outcome["tool_calls"] == 1

    second_round = complete.requests[1].messages
    roles = [(message.role, message.tool_call_id) for message in second_round]
    # assistant 发起两次同 ID 调用：执行一次 + 拒绝一次，两个回合都需回应
    assert roles == [
        ("user", None), ("assistant", None), ("tool", "call-1"), ("tool", "call-1"),
    ]
    assistant = second_round[1]
    assert assistant.tool_calls and assistant.tool_calls[0]["name"] == "write_file"
    assert "wrote a.txt" in second_round[2].content
    assert "duplicate call_id" in second_round[3].content
    # 拒绝事件可区分
    denied = [event for event in outcome["events"] if event["type"] == "tool_denied"]
    assert any(event.get("reason") == "duplicate_call_id" for event in denied)


def test_native_multi_tool_calls_execute_in_order():
    order = []

    def tool_a(arguments):  # noqa: ANN001
        order.append("a")
        return "a-ok"

    def tool_b(arguments):  # noqa: ANN001
        order.append("b")
        return "b-ok"

    complete = scripted_complete([
        {"content": "", "tool_calls": [
            native_call("c1", "tool_a", {}), native_call("c2", "tool_b", {}),
        ]},
        {"content": "final", "tool_calls": []},
    ])
    runtime = BuiltinReActRuntime(
        complete, {"tool_a": tool_a, "tool_b": tool_b},
        mode="native-tool", declared_tools=("tool_a", "tool_b"),
    )
    outcome = runtime.run_agent("x")
    assert outcome["termination_reason"] == "final_answer"
    assert order == ["a", "b"]
    tool_messages = [m for m in complete.requests[1].messages if m.role == "tool"]
    assert [m.tool_call_id for m in tool_messages] == ["c1", "c2"]


def test_native_mode_rejects_invalid_arguments_and_unknown_tool():
    calls = []
    complete = scripted_complete([
        {"content": "", "tool_calls": [
            {"id": "c1", "name": "write_file", "arguments": "{not json"},
            {"id": "c2", "name": "ghost_tool", "arguments": "{}"},
            {"id": "c3", "name": "write_file", "arguments": json.dumps({"path": "x"})},
        ]},
        {"content": "final", "tool_calls": []},
    ])
    runtime = BuiltinReActRuntime(
        complete, {"write_file": lambda args: calls.append(args) or "ok"},
        mode="native-tool", declared_tools=("write_file",),
    )
    outcome = runtime.run_agent("x")
    assert outcome["termination_reason"] == "final_answer"
    assert calls == []  # 参数不完整（缺 content）→ 拒绝执行
    denied = [event for event in outcome["events"] if event["type"] == "tool_denied"]
    reasons = {event.get("reason") for event in denied}
    assert "invalid_arguments" in reasons
    assert "unknown_tool" in reasons
    assert "arguments_schema_mismatch" in reasons


def test_legacy_mode_keeps_assistant_history():
    complete = scripted_complete([
        {"content": json.dumps({"action": "tool", "tool": "add", "input": {"a": 1}})},
        {"content": json.dumps({"action": "final", "answer": 2})},
    ])
    runtime = BuiltinReActRuntime(complete, {"add": lambda arg: arg["a"] * 2})
    outcome = runtime.run_agent("double it")
    assert outcome["termination_reason"] == "final_answer"
    second_round = complete.requests[1].messages
    roles = [message.role for message in second_round]
    assert roles == ["user", "assistant", "user"]
    assert second_round[1].content.startswith('{"action": "tool"')
    assert "observation: 2" in second_round[2].content
    assert outcome["prompt_version"] == PROMPT_VERSIONS["legacy-json"]


def test_tool_error_recovery_within_budget():
    attempts = []

    def flaky(arguments):  # noqa: ANN001
        attempts.append(arguments)
        if len(attempts) == 1:
            raise RuntimeError("first try fails")
        return "recovered"

    complete = scripted_complete([
        {"content": json.dumps({"action": "tool", "tool": "flaky", "input": {}})},
        {"content": json.dumps({"action": "tool", "tool": "flaky", "input": {}})},
        {"content": json.dumps({"action": "final", "answer": "ok"})},
    ])
    budget = ExecutionBudget(max_steps=4, max_tool_calls=4)
    runtime = BuiltinReActRuntime(complete, {"flaky": flaky}, budget=budget)
    outcome = runtime.run_agent("retry")
    assert outcome["termination_reason"] == "final_answer"
    assert len(attempts) == 2
    assert outcome["tool_calls"] == 2  # 额外调用计入预算
    assert any(event["type"] == "tool_error" for event in outcome["events"])


def test_budget_blocks_runaway_tool_calls_with_reason():
    responses = [
        {"content": json.dumps({"action": "tool", "tool": "spam", "input": {}})}
        for _ in range(10)
    ]
    complete = scripted_complete(responses)
    budget = ExecutionBudget(max_steps=8, max_tool_calls=3)
    runtime = BuiltinReActRuntime(
        complete, {"spam": lambda args: "again"}, budget=budget
    )
    outcome = runtime.run_agent("loop forever")
    assert outcome["termination_reason"] == "max_tool_calls"
    assert outcome["tool_calls"] == 3
    terminated = [event for event in outcome["events"] if event["type"] == "terminated"][0]
    assert terminated["reason"] == "max_tool_calls"
    # 步数维度 enforced / 未配置的维度不冒充
    report = outcome["budget"]
    assert report["max_steps"]["enforcement"] == "enforced"
    assert report["max_tool_calls"]["enforcement"] == "enforced"
    assert report["observed_cost_limit"]["enforcement"] == "not_configured"


def test_missing_usage_is_not_a_hard_budget():
    complete = scripted_complete([
        {"content": json.dumps({"action": "final", "answer": "x"})},
    ])
    budget = ExecutionBudget(max_steps=2, total_token_limit=100)
    runtime = BuiltinReActRuntime(complete, budget=budget)
    outcome = runtime.run_agent("x")
    assert outcome["termination_reason"] == "final_answer"
    assert outcome["usage"] == {"reported": False, "total_tokens": None, "observed_cost": 0.0}
    assert outcome["budget"]["total_token_limit"]["enforcement"] == "unknown"

    # Provider 报告 usage 后转为 enforced，超限给出具体原因
    complete = scripted_complete([
        {"content": json.dumps({"action": "final", "answer": "x"}),
         "usage": {"total_tokens": 200}},
    ])
    budget = ExecutionBudget(max_steps=2, total_token_limit=100)
    runtime = BuiltinReActRuntime(complete, budget=budget)
    outcome = runtime.run_agent("x")
    assert outcome["termination_reason"] == "token_limit"
    assert outcome["usage"]["reported"] is True
    assert outcome["budget"]["total_token_limit"]["enforcement"] == "enforced"


def test_cancellation_blocks_subsequent_calls():
    tool_calls = []
    cancelled = {"flag": False}

    def work(arguments):  # noqa: ANN001
        tool_calls.append(1)
        cancelled["flag"] = True  # 第一次工具执行后请求取消
        return "ok"

    complete = scripted_complete([
        {"content": json.dumps({"action": "tool", "tool": "work", "input": {}})},
        {"content": json.dumps({"action": "tool", "tool": "work", "input": {}})},
    ])

    runtime = BuiltinReActRuntime(
        complete, {"work": work},
        should_cancel=lambda: cancelled["flag"], max_steps=4,
    )
    outcome = runtime.run_agent("x")
    assert outcome["termination_reason"] == "cancelled"
    assert len(tool_calls) == 1  # 取消后无后续工具执行
    assert len(complete.requests) == 1  # 也无后续模型调用


def test_invalid_budget_config_rejected():
    import pytest

    with pytest.raises(ValueError, match="positive"):
        ExecutionBudget.from_config({"max_steps": 0})
    with pytest.raises(ValueError, match="ceiling"):
        ExecutionBudget.from_config({"max_steps": 10000})
    with pytest.raises(ValueError, match="unknown budget fields"):
        ExecutionBudget.from_config({"max_unicorns": 1})
    budget = ExecutionBudget.from_config(
        {"max_steps": 3, "wall_time_sec": 10.5, "max_tool_calls": 2}
    )
    assert budget.max_steps == 3 and budget.wall_time_sec == 10.5


def test_unknown_mode_rejected_at_construction():
    import pytest

    with pytest.raises(ValueError, match="mode"):
        BuiltinReActRuntime(scripted_complete([]), mode="auto-magic")



def test_cancellation_after_model_response_rejects_final_answer():
    cancelled = {"value": False}

    def complete(_request):
        cancelled["value"] = True
        return {"content": json.dumps({"action": "final", "answer": "late"})}

    runtime = BuiltinReActRuntime(complete, should_cancel=lambda: cancelled["value"])
    result = runtime.run_agent("x")
    assert result["termination_reason"] == "cancelled"
    assert result["final_output"] is None
    assert result["termination_detail"] == "cancelled after model response"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 1.5])
def test_invalid_integer_budget_values_are_rejected(value):
    with pytest.raises(ValueError):
        ExecutionBudget.from_config({"max_tool_calls": value})


def test_invalid_budget_observations_are_rejected():
    budget = ExecutionBudget(max_steps=2)
    with pytest.raises(ValueError):
        budget.record_usage({"total_tokens": -1}, None)
    with pytest.raises(ValueError):
        budget.record_usage({"total_tokens": 1.5}, None)
    with pytest.raises(ValueError):
        budget.record_usage(None, {"total": float("nan")})
    with pytest.raises(ValueError):
        budget.record_usage(None, {"total": -0.1})


def test_event_sink_failure_aborts_builtin_execution():
    complete = scripted_complete([json.dumps({"action": "final", "answer": "x"})])

    def broken_sink(_event):
        raise RuntimeError("event store unavailable")

    runtime = BuiltinReActRuntime(complete, event_sink=broken_sink)
    with pytest.raises(RuntimeError, match="event store unavailable"):
        runtime.run_agent("x")
