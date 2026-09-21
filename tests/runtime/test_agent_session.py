"""M5-T03a：case-owned Builtin ReAct 会话——多轮历史、累计预算与中断。

全部断言使用真实 ``BuiltinReActRuntime`` + 脚本化 ``complete``（不 mock 运行时）：

- 第一轮以确认问题结束（final answer）后会话仍可 send；第二轮看到第一轮消息，
  动作只执行一次；
- 关闭 / 终止后 send 拒绝，close 幂等，begin 只允许一次；
- 两个会话互不可见（历史 / call_id / 预算都不共享，无共享可变默认值）；
- 跨轮重复 call_id 不二次执行；步数 / 工具次数预算跨轮累计且不重新武装；
- interrupt 后待办写入不执行；legacy-json 与 native-tool 两种模式都覆盖；
- 暂态工具错误在下一轮可见并成功恢复，副作用不重复；
- 证据边界失败（AgentFatalError）仍然中止且不回灌。
"""
from __future__ import annotations

import json
from typing import Any, Callable

import pytest

from motte_agent.budget import ExecutionBudget
from motte_agent.builtin_react import BuiltinReActRuntime
from motte_agent.errors import AgentFatalError
from motte_agent.session import BuiltinSession, SessionStateError
from motte_contracts.messages import Message

MODES = ("legacy-json", "native-tool")


def scripted_complete(
    responses: list[dict[str, Any]],
    on_request: Callable[[Any], None] | None = None,
):
    """按顺序返回固定 envelope；记录全部请求供断言（脚本化 provider）。"""
    requests: list[Any] = []
    queue = list(responses)

    def complete(request):  # noqa: ANN001
        requests.append(request)
        if on_request is not None:
            on_request(request)
        if not queue:
            raise AssertionError("scripted provider exhausted")
        return dict(queue.pop(0))

    complete.requests = requests  # type: ignore[attr-defined]
    return complete


def final_response(mode: str, answer: Any) -> dict[str, Any]:
    """两种模式下的正常 final answer（结束当前 turn，不结束会话）。"""
    if mode == "native-tool":
        return {"content": answer, "tool_calls": []}
    return {"content": json.dumps({"action": "final", "answer": answer})}


def tool_response(
    mode: str, name: str, arguments: dict[str, Any], *, call_id: str = "call-1",
) -> dict[str, Any]:
    """两种模式下的工具调用响应。"""
    if mode == "native-tool":
        return {"content": "", "tool_calls": [
            {"id": call_id, "name": name, "arguments": json.dumps(arguments)},
        ]}
    return {"content": json.dumps({"action": "tool", "tool": name, "input": arguments})}


def build(mode: str, responses, tools, **kwargs):  # noqa: ANN001, ANN201
    """构造真实运行时（native 模式补齐工具声明），返回 (runtime, complete)。"""
    complete = scripted_complete(responses, on_request=kwargs.pop("on_request", None))
    if mode == "native-tool":
        kwargs.setdefault("declared_tools", tuple(sorted(tools)))
    runtime = BuiltinReActRuntime(complete, tools, mode=mode, **kwargs)
    return runtime, complete


# ---------------------------------------------------------------- 多轮历史


@pytest.mark.parametrize("mode", MODES)
def test_multi_turn_confirmation_then_action_executes_once(mode):
    """turn 1 只问确认并 final；turn 2 看到历史后执行动作且只执行一次。"""
    writes: list[str] = []

    def write_file(arguments):  # noqa: ANN001
        writes.append(arguments["path"])
        return f"wrote {arguments['path']}"

    runtime, complete = build(mode, [
        final_response(mode, "Confirm writing a.txt?"),
        tool_response(mode, "write_file", {"path": "a.txt", "content": "A"}),
        final_response(mode, "done"),
    ], {"write_file": write_file})
    session = runtime.begin()
    assert isinstance(session, BuiltinSession)

    first = runtime.send("please write a.txt")
    assert first["termination_reason"] == "final_answer"
    assert first["final_output"] == "Confirm writing a.txt?"
    assert first["turn"] == 1
    assert writes == []  # 第一轮没有副作用
    assert runtime.observe()["state"] == "active"  # final answer 只结束 turn
    assert session.send_refusal() is None
    assert len(complete.requests) == 1

    events_before = list(runtime.events)
    second = runtime.send("yes, go ahead")
    assert second["termination_reason"] == "final_answer"
    assert second["final_output"] == "done"
    assert second["turn"] == 2
    assert writes == ["a.txt"]  # 动作恰好一次
    assert session.send_refusal() is None

    # 第二轮模型确实收到第一轮的消息。native 模式下 final answer 按原算法
    # 不入史（one-shot 语义逐字保留），legacy 模式下 assistant 决策完整入史。
    history = complete.requests[1].messages
    if mode == "native-tool":
        assert [message.role for message in history] == ["user", "user"]
    else:
        assert [message.role for message in history] == ["user", "assistant", "user"]
        assert "Confirm writing a.txt?" in history[1].content
    assert history[0].content == "please write a.txt"
    assert history[-1].content == "yes, go ahead"

    # 工具执行后的请求仍带完整历史与回灌结果
    after_tool = complete.requests[2].messages
    if mode == "native-tool":
        assert [message.role for message in after_tool] == [
            "user", "user", "assistant", "tool",
        ]
        assert after_tool[2].tool_calls[0]["id"] == "call-1"
        assert after_tool[3].tool_call_id == "call-1"
        assert after_tool[3].content == "wrote a.txt"
    else:
        assert [message.role for message in after_tool] == [
            "user", "assistant", "user", "assistant", "user",
        ]
        assert "observation: wrote a.txt" in after_tool[-1].content

    observed = runtime.observe()
    assert observed["turns"] == 2
    assert observed["steps"] == 3  # turn1=1 步 + turn2=2 步（跨轮累计）
    assert observed["tool_calls"] == 1
    assert observed["event_seq"] == len(observed["events"]) == len(runtime.events)
    # send 的 events 是本次 turn 的切片；runtime.events 累积全会话
    assert second["events"] == runtime.events[len(events_before):]
    assert len(second["events"]) < len(runtime.events)


# ---------------------------------------------------------------- 状态机


def test_send_requires_begin_and_begin_is_allowed_once():
    runtime = BuiltinReActRuntime(scripted_complete([]))
    idle = runtime.observe()
    assert idle["state"] == "idle" and idle["session_id"] is None
    with pytest.raises(SessionStateError, match="has not begun"):
        runtime.send("hi")
    session = runtime.begin()
    assert runtime.session is session
    with pytest.raises(SessionStateError, match="already begun"):
        runtime.begin()


def test_close_is_idempotent_and_blocks_send():
    runtime, complete = build(
        "legacy-json", [final_response("legacy-json", "bye")], {},
    )
    runtime.begin()
    assert runtime.send("hello")["termination_reason"] == "final_answer"
    assert runtime.close() is True
    assert runtime.close() is False  # 幂等
    assert runtime.observe()["state"] == "closed"
    with pytest.raises(SessionStateError, match="closed"):
        runtime.send("again")
    assert len(complete.requests) == 1


@pytest.mark.parametrize("mode", MODES)
def test_budget_exhaustion_terminates_session_and_blocks_send(mode):
    budget = ExecutionBudget(max_steps=3, max_tool_calls=1)
    runtime, complete = build(mode, [
        tool_response(mode, "list_files", {}, call_id="call-1"),
        tool_response(mode, "list_files", {}, call_id="call-2"),
    ], {"list_files": lambda arguments: "(empty)"}, budget=budget)
    session = runtime.begin()
    first = runtime.send("go")
    assert first["termination_reason"] == "max_tool_calls"
    assert first["tool_calls"] == 1
    observed = runtime.observe()
    assert observed["state"] == "terminated" and session.send_refusal()
    with pytest.raises(SessionStateError, match="terminal"):
        runtime.send("more")
    assert len(complete.requests) == 2  # 终止后没有新的模型调用


def test_one_shot_run_agent_shape_is_unchanged_and_closes_session():
    """one-shot 包装：返回形状、事件与终止原因保持；结束后会话不可 send。"""
    runtime, _complete = build(
        "legacy-json", [final_response("legacy-json", 3)], {},
    )
    outcome = runtime.run_agent("x")
    assert set(outcome) == {
        "final_output", "termination_reason", "termination_detail", "steps",
        "tool_calls", "events", "messages", "usage", "budget", "prompt_version",
    }
    assert outcome["steps"] == 1 and outcome["final_output"] == 3
    assert [event["type"] for event in runtime.events] == [
        "step_started", "model_request", "model_response", "final_answer", "terminated",
    ]
    assert runtime.observe()["state"] == "closed"
    with pytest.raises(SessionStateError, match="closed"):
        runtime.send("again")


# ---------------------------------------------------------------- 会话隔离


def test_two_sessions_share_no_history_call_ids_or_budget():
    writes_a: list[str] = []
    writes_b: list[str] = []

    def handler(target, arguments):  # noqa: ANN001
        target.append(arguments["path"])
        return f"wrote {arguments['path']}"

    runtime_a, _complete_a = build("legacy-json", [
        tool_response("legacy-json", "write_file", {"path": "a.txt"}),
        final_response("legacy-json", "a done"),
    ], {"write_file": lambda arguments: handler(writes_a, arguments)})
    runtime_b, complete_b = build("legacy-json", [
        final_response("legacy-json", "b done"),
    ], {"write_file": lambda arguments: handler(writes_b, arguments)})

    session_a = runtime_a.begin()
    session_b = runtime_b.begin()
    assert session_a.session_id != session_b.session_id
    assert session_a.messages is not session_b.messages
    assert session_a.executed_call_ids is not session_b.executed_call_ids
    assert session_a.events is not session_b.events

    assert runtime_a.send("write a.txt")["termination_reason"] == "final_answer"
    assert runtime_b.send("just answer")["termination_reason"] == "final_answer"

    # B 的模型输入只有自己的历史：看不到 A 的消息与调用
    assert [message.content for message in complete_b.requests[0].messages] == [
        "just answer",
    ]
    # A 的调用与预算完全不落到 B
    assert writes_a == ["a.txt"] and writes_b == []
    observed_b = runtime_b.observe()
    assert observed_b["tool_calls"] == 0
    assert observed_b["executed_call_ids"] == []
    assert observed_b["steps"] == 1
    assert runtime_a.observe()["tool_calls"] == 1


def test_session_containers_have_no_shared_mutable_defaults():
    left, right = BuiltinSession(), BuiltinSession()
    left.messages.append(Message(role="user", content="x"))
    left.executed_call_ids.add("call-1")
    left.record_event("noop")
    assert right.messages == [] and right.executed_call_ids == set()
    assert right.events == [] and right.event_seq == 0
    assert right.steps == 0 and right.turns == 0
    assert left.budget is not right.budget


# ---------------------------------------------------------------- 重复 call_id


def test_duplicate_call_id_across_turns_executes_once():
    writes: list[str] = []

    def write_file(arguments):  # noqa: ANN001
        writes.append(arguments["content"])
        return f"wrote {arguments['path']}"

    runtime, complete = build("native-tool", [
        tool_response("native-tool", "write_file", {"path": "a.txt", "content": "A"},
                      call_id="call-7"),
        final_response("native-tool", "turn-1 done"),
        tool_response("native-tool", "write_file", {"path": "b.txt", "content": "B"},
                      call_id="call-7"),
        final_response("native-tool", "turn-2 done"),
    ], {"write_file": write_file})
    runtime.begin()
    assert runtime.send("write a.txt")["termination_reason"] == "final_answer"

    second = runtime.send("now write b.txt")
    assert second["termination_reason"] == "final_answer"
    assert writes == ["A"]  # 同 call_id 不执行第二次（即使参数不同）
    assert second["tool_calls"] == 1  # 拒绝不消耗工具预算
    denied = [event for event in second["events"] if event["type"] == "tool_denied"]
    assert [event["reason"] for event in denied] == ["duplicate_call_id"]
    denial = [message for message in complete.requests[3].messages
              if message.role == "tool"][-1]
    assert denial.tool_call_id == "call-7"
    assert "duplicate call_id call-7" in denial.content
    assert runtime.observe()["executed_call_ids"] == ["call-7"]


# ---------------------------------------------------------------- 累计预算


def test_cumulative_max_steps_is_not_re_armed_by_the_next_send():
    budget = ExecutionBudget(max_steps=2)
    runtime, complete = build("legacy-json", [
        tool_response("legacy-json", "list_files", {}),
        final_response("legacy-json", "first done"),
    ], {"list_files": lambda arguments: "(empty)"}, budget=budget)
    runtime.begin()

    first = runtime.send("one")
    assert first["termination_reason"] == "final_answer"
    assert first["steps"] == 2 and runtime.observe()["steps"] == 2

    second = runtime.send("two")
    assert second["termination_reason"] == "max_steps"
    assert second["termination_detail"] == "blocked before step 3"
    assert second["final_output"] is None
    # 步数计数器在检查前推进（沿用原算法语义）：本次 turn 没有模型调用
    assert runtime.observe()["steps"] == 3
    assert len(complete.requests) == 2
    with pytest.raises(SessionStateError, match="terminal"):
        runtime.send("three")


def test_cumulative_max_tool_calls_is_shared_across_turns():
    calls: list[str] = []

    def ping(arguments):  # noqa: ANN001
        calls.append("ping")
        return "pong"

    budget = ExecutionBudget(max_steps=8, max_tool_calls=2)
    runtime, complete = build("legacy-json", [
        tool_response("legacy-json", "ping", {}),
        final_response("legacy-json", "first done"),
        tool_response("legacy-json", "ping", {}),
        tool_response("legacy-json", "ping", {}),
    ], {"ping": ping}, budget=budget)
    runtime.begin()

    assert runtime.send("one")["termination_reason"] == "final_answer"
    assert len(complete.requests) == 2
    second = runtime.send("two")
    assert second["termination_reason"] == "max_tool_calls"
    assert second["tool_calls"] == 2  # 跨轮累计
    assert len(calls) == 2  # 第三次调用从未执行
    assert second["session"]["state"] == "terminated"


def test_cumulative_token_limit_is_shared_across_turns():
    budget = ExecutionBudget(max_steps=6, total_token_limit=100)
    runtime, complete = build("legacy-json", [
        {"content": json.dumps({"action": "final", "answer": "first"}),
         "usage": {"total_tokens": 60}},
        {"content": json.dumps({"action": "final", "answer": "second"}),
         "usage": {"total_tokens": 60}},
    ], {}, budget=budget)
    runtime.begin()

    assert runtime.send("one")["termination_reason"] == "final_answer"
    second = runtime.send("two")
    # usage 台账跨 turn 累计（60 + 60 > 100）：第二次 send 以具体原因终止
    assert second["termination_reason"] == "token_limit"
    assert second["termination_detail"] == "observed after model response"
    assert second["usage"]["total_tokens"] == 120
    assert runtime.observe()["usage"]["total_tokens"] == 120
    assert len(complete.requests) == 2


def test_cumulative_wall_time_is_global_to_the_session():
    budget = ExecutionBudget(max_steps=4, wall_time_sec=10.0)
    runtime, complete = build("legacy-json", [
        final_response("legacy-json", "first done"),
    ], {}, budget=budget)
    runtime.begin()
    assert runtime.send("one")["termination_reason"] == "final_answer"

    # 单调时钟属于会话预算：前移起点模拟同一会话已消耗的墙钟，不重新武装
    budget._started_monotonic -= 11.0
    second = runtime.send("two")
    assert second["termination_reason"] == "wall_time"
    assert second["termination_detail"] == "blocked before step 2"
    assert len(complete.requests) == 1  # 超时后没有新的模型调用
    with pytest.raises(SessionStateError, match="terminal"):
        runtime.send("three")


# ---------------------------------------------------------------- 中断


@pytest.mark.parametrize("mode", MODES)
def test_interrupt_before_write_tool_stops_the_write(mode):
    writes: list[str] = []
    runtime: BuiltinReActRuntime | None = None

    def write_file(arguments):  # noqa: ANN001
        writes.append(arguments["path"])
        return f"wrote {arguments['path']}"

    def interrupt_on_model_call(_request):  # noqa: ANN001
        # 模型调用中收到中断：模型已经决定写入，但写入不得执行
        assert runtime is not None
        runtime.interrupt()

    runtime, complete = build(mode, [
        tool_response(mode, "write_file", {"path": "a.txt", "content": "A"}),
        final_response(mode, "should never be reached"),
    ], {"write_file": write_file}, on_request=interrupt_on_model_call)
    runtime.begin()

    outcome = runtime.send("write a.txt")
    assert outcome["termination_reason"] == "cancelled"
    assert outcome["final_output"] is None
    assert writes == []  # 计数保持 0：待办写入被取消阻断
    assert "tool_call" not in [event["type"] for event in outcome["events"]]
    observed = runtime.observe()
    assert observed["state"] == "terminated" and observed["cancelled"] is True
    with pytest.raises(SessionStateError, match="interrupted"):
        runtime.send("retry")
    assert len(complete.requests) == 1


# ---------------------------------------------------------------- 暂态错误恢复


def test_transient_tool_error_in_turn_1_recovers_in_turn_2():
    attempts: list[str] = []
    writes: list[str] = []

    def flaky(arguments):  # noqa: ANN001
        attempts.append(arguments["path"])
        if len(attempts) == 1:
            raise RuntimeError("transient disk failure")
        writes.append(arguments["path"])
        return f"wrote {arguments['path']}"

    budget = ExecutionBudget(max_steps=6, max_tool_calls=4)
    runtime, complete = build("legacy-json", [
        tool_response("legacy-json", "flaky", {"path": "a.txt"}),
        final_response("legacy-json", "write failed; shall I retry?"),
        tool_response("legacy-json", "flaky", {"path": "a.txt"}),
        final_response("legacy-json", "written"),
    ], {"flaky": flaky}, budget=budget)
    runtime.begin()

    first = runtime.send("write a.txt")
    assert first["termination_reason"] == "final_answer"
    assert attempts == ["a.txt"] and writes == []
    errors = [event for event in first["events"] if event["type"] == "tool_error"]
    assert len(errors) == 1 and errors[0]["tool"] == "flaky"
    assert first["tool_calls"] == 1  # 失败的尝试也计入预算

    second = runtime.send("yes, retry")
    assert second["termination_reason"] == "final_answer"
    assert attempts == ["a.txt", "a.txt"]  # 每次尝试都被记录
    assert writes == ["a.txt"]  # 副作用只发生一次
    assert second["tool_calls"] == 2  # 累计计数仍受预算约束
    assert second["session"]["tool_calls"] == 2
    # 第一轮的错误观察确实进入第二轮模型输入（可恢复）
    history = complete.requests[2].messages
    assert any(
        "tool error: transient disk failure" in str(message.content) for message in history
    )


# ---------------------------------------------------------------- 证据边界


@pytest.mark.parametrize("mode", MODES)
def test_fatal_error_aborts_session_without_tool_error_feedback(mode):
    def explode(arguments):  # noqa: ANN001
        raise AgentFatalError("evidence boundary failed", code="TEST_BOUNDARY")

    runtime, complete = build(mode, [
        tool_response(mode, "list_files", {}),
        final_response(mode, "recovered"),
    ], {"list_files": explode})
    runtime.begin()

    with pytest.raises(AgentFatalError, match="evidence boundary failed"):
        runtime.send("go")
    assert len(complete.requests) == 1  # 中止后没有第二次模型调用
    observed = runtime.observe()
    assert observed["state"] == "terminated"
    assert observed["termination_reason"] == "error"
    assert observed["cancelled"] is False
    # 证据边界失败不回灌成工具错误观察，也不补写 terminated 事件
    assert not any(
        "tool error" in str(message["content"]) for message in observed["messages"]
    )
    assert observed["events"][-1]["type"] == "tool_call"
    with pytest.raises(SessionStateError, match="terminal"):
        runtime.send("again")
