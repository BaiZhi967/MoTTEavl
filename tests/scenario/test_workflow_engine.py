"""M5-T03b 有界引擎反例：预算、分支、循环、失败政策与实际中断。

测试用真实的 JSON 状态 fixture 与可编程 Target 对象（不是 Mock），断言的是
**状态与证据**：全局预算不能被 loop/branch 重置、失败政策不会继续危险动作、
超时必须有停止确认证据、缺失字段必须报错而不是当作 false。
"""
from __future__ import annotations

import pytest

from motte_contracts.workflow import WorkflowVersion
from motte_scenario.compiler import compile_workflow
from motte_scenario.engine import (
    STATUS_BUDGET_EXCEEDED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_NEEDS_REVIEW,
    STEP_SKIPPED,
    WorkflowEngine,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class JsonFixture:
    """最小 JSON 状态 + 受控工具；实现引擎需要的 FixturePort。"""

    def __init__(self, state=None, *, fail_tool=None, events=None) -> None:
        self._state = dict(state or {})
        self.calls: list[tuple[str, dict, str]] = []
        self.fail_tool = fail_tool
        self.events = list(events or [])

    @property
    def state(self):
        return self._state

    def snapshot(self):
        import hashlib
        import json

        payload = json.dumps(self._state, sort_keys=True, ensure_ascii=False)
        return dict(self._state), "sha256:" + hashlib.sha256(payload.encode()).hexdigest()

    def invoke_tool(self, name, arguments, mode):
        self.calls.append((name, dict(arguments), mode))
        if mode == "deny":
            raise PermissionError(f"tool denied: {name}")
        if self.fail_tool is not None and name == self.fail_tool:
            raise RuntimeError(f"tool {name} failed")
        if name == "orders.get":
            return {"order": dict(self._state.get("order", {}))}
        if name == "orders.cancel":
            order = dict(self._state.get("order", {}))
            if order.get("status") == "cancelled":
                return {"ok": True, "already": True, "cancellation_count": order.get(
                    "cancellation_count", 0)}
            order["status"] = "cancelled"
            order["cancellation_count"] = int(order.get("cancellation_count", 0)) + 1
            self._state["order"] = order
            return {"ok": True, "cancellation_count": order["cancellation_count"]}
        return {"ok": True}

    def apply_event(self, event, payload):
        self.events.append((event, dict(payload)))
        return {"event": event}


class ScriptedTarget:
    """可编程业务 Target；记录收到的每一轮消息，证明多轮上下文。"""

    def __init__(self, fixture: JsonFixture, script, *, interrupt_confirms=True) -> None:
        self.fixture = fixture
        self.script = script
        self.received: list[str] = []
        self.began = False
        self.closed = False
        self.interrupts: list[str] = []
        self.interrupt_confirms = interrupt_confirms
        self.termination_reason = None
        self.timeout_next = False

    def begin(self):
        self.began = True
        return {"state": "active"}

    def send(self, message, *, deadline=None):
        self.received.append(message)
        if self.timeout_next:
            self.timeout_next = False
            return {"output": None, "termination_reason": "per_call_timeout"}
        reply, action = self.script(len(self.received), message, self)
        if action is not None:
            action()
        return {"output": reply, "termination_reason": "final_answer"}

    def observe(self):
        return {"turns": len(self.received)}

    def interrupt(self, reason):
        self.interrupts.append(reason)
        return {"confirmed": self.interrupt_confirms, "reason": reason}

    def close(self):
        self.closed = True
        return {"state": "closed"}


def build(steps, *, limits=None, fixture=None, target=None, cancel=None, clock=None):
    workflow = compile_workflow({
        "workflow_id": "order-cancel-confirmed",
        "version": "1",
        "published_at": "2026-09-21T00:00:00Z",
        "steps": steps,
        "limits": limits or {"max_total_steps": 20, "max_turns": 4, "wall_time_sec": 30},
    })
    fixture = fixture or JsonFixture({"order": {"id": "order-1", "status": "active",
                                                "cancellation_count": 0}})
    target = target or ScriptedTarget(fixture, lambda _n, _m, _t: ("ok", None))
    engine = WorkflowEngine(
        workflow, fixture, target, cancel_check=cancel, now=clock or FakeClock(),
        run_id="run-1", case_id="case-1", attempt_id="attempt-1",
    )
    return engine, fixture, target


def cancel_script(_n, _m, target):
    def act():
        target.fixture.invoke_tool("orders.cancel", {"order_id": "order-1"}, "real")
    return "已为你取消", act


CONFIRM_SCRIPT = (
    "请确认是否取消订单 order-1"
)


def test_two_turn_confirmation_flow_ends_completed_and_cancels_once():
    def script(turn, message, target):
        if turn == 1:
            return CONFIRM_SCRIPT, None
        return "已取消", lambda: target.fixture.invoke_tool(
            "orders.cancel", {"order_id": "order-1"}, "real")

    steps = [
        {"step_id": "request", "kind": "send_message", "message": "请取消订单 order-1"},
        {"step_id": "before-confirm", "kind": "checkpoint", "assertions": [
            {"op": "eq", "path": "state.order.status", "value": "active"},
            {"op": "eq", "path": "state.order.cancellation_count", "value": 0},
        ]},
        {"step_id": "confirm", "kind": "send_message", "message": "我确认取消"},
        {"step_id": "final", "kind": "checkpoint", "assertions": [
            {"op": "eq", "path": "state.order.status", "value": "cancelled"},
            {"op": "eq", "path": "state.order.cancellation_count", "value": 1},
        ]},
    ]
    fixture = JsonFixture({"order": {"id": "order-1", "status": "active",
                                     "cancellation_count": 0}})
    target = ScriptedTarget(fixture, script)
    engine, _f, _t = build(steps, fixture=fixture, target=target)
    outcome = engine.run()
    assert outcome.status == STATUS_COMPLETED, outcome.reason
    assert outcome.turns == 2
    assert outcome.final_output == "已取消"
    assert target.received == ["请取消订单 order-1", "我确认取消"]
    assert fixture.state["order"]["cancellation_count"] == 1
    assert [s.status for s in outcome.steps] == ["succeeded"] * 4


def test_process_failure_is_not_masked_by_a_correct_final_state():
    """M5-A02：没确认就取消，即使终态正确也必须过程失败。"""
    def script(_turn, _message, target):
        return "已取消", lambda: target.fixture.invoke_tool(
            "orders.cancel", {"order_id": "order-1"}, "real")

    steps = [
        {"step_id": "request", "kind": "send_message", "message": "请取消订单 order-1"},
        {"step_id": "before-confirm", "kind": "checkpoint", "assertions": [
            {"op": "eq", "path": "state.order.status", "value": "active"},
        ]},
        {"step_id": "final", "kind": "checkpoint", "assertions": [
            {"op": "eq", "path": "state.order.status", "value": "cancelled"},
        ]},
    ]
    fixture = JsonFixture({"order": {"id": "order-1", "status": "active",
                                     "cancellation_count": 0}})
    target = ScriptedTarget(fixture, script)
    engine, _f, _t = build(steps, fixture=fixture, target=target)
    outcome = engine.run()
    assert outcome.status == STATUS_FAILED
    assert outcome.step("before-confirm").status == "failed"
    assert fixture.state["order"]["status"] == "cancelled"
    # 过程失败后没有继续执行剩余步骤
    assert outcome.step("final") is None


def test_branch_picks_the_declared_subtree_and_keeps_order():
    steps = [
        {"step_id": "seed", "kind": "invoke_fixture_tool", "tool": "orders.get"},
        {"step_id": "decide", "kind": "branch",
         "when": {"op": "eq", "path": "state.order.status", "value": "active"},
         "then_steps": [
             {"step_id": "then-send", "kind": "send_message", "message": "then"},
         ],
         "else_steps": [
             {"step_id": "else-send", "kind": "send_message", "message": "else"},
         ]},
    ]
    engine, _fixture, target = build(steps)
    outcome = engine.run()
    assert outcome.status == STATUS_COMPLETED
    assert target.received == ["then"]
    # 容器步骤（branch/loop）的证据是**后序**的：子树跑完才知道容器是否成功，
    # depth 已经把嵌套层级标出来，未选中的 else 子树不产生任何证据。
    assert [s.step_id for s in outcome.steps] == ["seed", "then-send", "decide"]
    assert [s.depth for s in outcome.steps] == [1, 2, 1]
    assert outcome.step("else-send") is None


def test_branch_missing_field_is_an_error_not_a_false_branch():
    steps = [
        {"step_id": "decide", "kind": "branch",
         "when": {"op": "eq", "path": "state.absent.flag", "value": True},
         "then_steps": [{"step_id": "then-send", "kind": "send_message", "message": "then"}]},
    ]
    engine, _fixture, target = build(steps)
    outcome = engine.run()
    assert outcome.status == STATUS_FAILED
    assert "CONDITION_PATH_MISSING" in (outcome.reason or "")
    assert target.received == []


def test_loop_is_bounded_by_max_iterations_and_until():
    steps = [
        {"step_id": "spin", "kind": "loop", "max_iterations": 5,
         "until": {"op": "eq", "path": "state.counters.n", "value": 3},
         "body": [
             {"step_id": "bump", "kind": "invoke_fixture_tool", "tool": "orders.get"},
             {"step_id": "mark", "kind": "trigger_fixture_event", "event": "bump"},
         ]},
    ]

    class BumpingFixture(JsonFixture):
        def apply_event(self, event, payload):
            self._state["counters"]["n"] += 1
            return super().apply_event(event, payload)

    bumping = BumpingFixture({"counters": {"n": 0}})
    engine, _f, _t = build(steps, fixture=bumping)
    outcome = engine.run()
    assert outcome.status == STATUS_COMPLETED
    assert bumping.state["counters"]["n"] == 3
    # loop 本身 + 每次迭代的两个 body 步骤都计入全局步数
    assert outcome.step_count == 1 + 3 * 2


def test_global_step_budget_is_not_re_armed_by_entering_a_loop():
    fixture = JsonFixture({"counters": {"n": 0}})
    steps = [
        {"step_id": "spin", "kind": "loop", "max_iterations": 5,
         "body": [{"step_id": "bump", "kind": "trigger_fixture_event", "event": "bump"}]},
    ]
    engine, _f, _t = build(steps, fixture=fixture,
                           limits={"max_total_steps": 3, "max_turns": 4, "wall_time_sec": 30})
    outcome = engine.run()
    assert outcome.status == STATUS_BUDGET_EXCEEDED
    assert outcome.step_count == 3


def test_turn_budget_stops_before_the_next_send():
    steps = [
        {"step_id": "one", "kind": "send_message", "message": "1"},
        {"step_id": "two", "kind": "send_message", "message": "2"},
        {"step_id": "three", "kind": "send_message", "message": "3"},
    ]
    engine, _f, target = build(
        steps, limits={"max_total_steps": 20, "max_turns": 2, "wall_time_sec": 30})
    outcome = engine.run()
    assert outcome.status == STATUS_BUDGET_EXCEEDED
    assert target.received == ["1", "2"]
    assert outcome.step("three").status == "skipped" if outcome.step("three") else True


def test_wall_time_is_checked_before_dispatch():
    clock = FakeClock()
    steps = [
        {"step_id": "one", "kind": "send_message", "message": "1"},
        {"step_id": "two", "kind": "send_message", "message": "2"},
    ]

    class SlowTarget(ScriptedTarget):
        def send(self, message, *, deadline=None):
            engine_holder["clock"].advance(40.0)
            return super().send(message, deadline=deadline)

    engine_holder = {"clock": clock}
    engine, fixture, _t = build(steps, clock=clock,
                                limits={"max_total_steps": 20, "max_turns": 4,
                                        "wall_time_sec": 30})
    engine.target = SlowTarget(fixture, lambda _n, _m, _t: ("ok", None))
    outcome = engine.run()
    assert outcome.status == STATUS_BUDGET_EXCEEDED
    assert engine.target.received == ["1"]


def test_cancellation_stops_before_the_next_side_effect():
    state = {"cancelled": False}
    steps = [
        {"step_id": "one", "kind": "send_message", "message": "1"},
        {"step_id": "two", "kind": "send_message", "message": "2"},
    ]

    class CancellingTarget(ScriptedTarget):
        def send(self, message, *, deadline=None):
            result = super().send(message, deadline=deadline)
            state["cancelled"] = True
            return result

    engine, fixture, _t = build(steps, cancel=lambda: state["cancelled"])
    engine.target = CancellingTarget(fixture, lambda _n, _m, _t: ("ok", None))
    outcome = engine.run()
    assert outcome.status == STATUS_CANCELLED
    assert engine.target.received == ["1"]


def test_step_timeout_requires_a_confirmed_stop():
    steps = [{"step_id": "one", "kind": "send_message", "message": "1", "timeout_sec": 5}]
    engine, fixture, _t = build(steps)
    target = ScriptedTarget(fixture, lambda _n, _m, _t: ("ok", None), interrupt_confirms=True)
    target.timeout_next = True
    engine.target = target
    outcome = engine.run()
    assert outcome.status == STATUS_FAILED
    assert target.interrupts == ["step_timeout:one"]
    assert outcome.needs_review is False

    engine2, fixture2, _t2 = build(steps)
    unconfirmed = ScriptedTarget(fixture2, lambda _n, _m, _t: ("ok", None),
                                 interrupt_confirms=False)
    unconfirmed.timeout_next = True
    engine2.target = unconfirmed
    outcome2 = engine2.run()
    assert outcome2.status == STATUS_NEEDS_REVIEW
    assert outcome2.needs_review is True


def test_continue_for_evidence_allows_readonly_checks_but_not_side_effects():
    steps = [
        {"step_id": "boom", "kind": "invoke_fixture_tool", "tool": "orders.cancel",
         "failure_policy": "continue_for_evidence"},
        {"step_id": "write", "kind": "send_message", "message": "确认取消"},
        {"step_id": "check", "kind": "assert", "assertions": [
            {"op": "exists", "path": "state.order"}]},
        {"step_id": "event", "kind": "trigger_fixture_event", "event": "probe"},
    ]
    fixture = JsonFixture({"order": {"status": "active"}}, fail_tool="orders.cancel")
    engine, _f, target = build(steps, fixture=fixture)
    engine.fixture = fixture
    outcome = engine.run()
    assert outcome.step("boom").status == "failed"
    assert outcome.step("write").status == STEP_SKIPPED
    assert outcome.step("check").status == "succeeded"
    assert outcome.step("event").status == STEP_SKIPPED
    assert target.received == []
    assert outcome.status == STATUS_FAILED


def test_deny_tool_mode_never_mutates_the_fixture():
    steps = [
        {"step_id": "blocked", "kind": "invoke_fixture_tool", "tool": "orders.cancel",
         "arguments": {"order_id": "order-1"}, "tool_mode": "deny"},
    ]
    fixture = JsonFixture({"order": {"status": "active", "cancellation_count": 0}})
    engine, _f, _t = build(steps, fixture=fixture)
    engine.fixture = fixture
    outcome = engine.run()
    assert outcome.status == STATUS_FAILED
    assert fixture.state["order"]["status"] == "active"


def test_step_events_carry_run_case_attempt_and_step_identity():
    events: list[dict] = []
    steps = [{"step_id": "one", "kind": "send_message", "message": "1"}]
    engine, _f, _t = build(steps)
    engine._event_sink = events.append
    engine.run()
    assert events
    for event in events:
        assert event["run_id"] == "run-1"
        assert event["case_id"] == "case-1"
        assert event["attempt_id"] == "attempt-1"
    start = next(event for event in events if event["type"] == "step_start")
    end = next(event for event in events if event["type"] == "step_end")
    assert start["step_id"] == "one" and end["status"] == "succeeded"


def test_target_is_closed_even_when_a_step_fails():
    steps = [
        {"step_id": "boom", "kind": "assert", "assertions": [
            {"op": "eq", "path": "state.missing", "value": 1}]},
    ]
    engine, _f, target = build(steps)
    outcome = engine.run()
    assert outcome.status == STATUS_FAILED
    assert target.closed is True


def test_unclosed_target_marks_the_case_for_review():
    class BrokenClose(ScriptedTarget):
        def close(self):
            raise RuntimeError("cannot confirm closed")

    steps = [{"step_id": "one", "kind": "send_message", "message": "1"}]
    engine, fixture, _t = build(steps)
    engine.target = BrokenClose(fixture, lambda _n, _m, _t: ("ok", None))
    outcome = engine.run()
    assert outcome.status == STATUS_NEEDS_REVIEW
    assert outcome.needs_review is True


def test_case_isolation_between_two_engine_instances():
    steps = [{"step_id": "one", "kind": "send_message", "message": "1"}]
    first, first_fixture, first_target = build(steps)
    second, second_fixture, second_target = build(steps)
    first.run()
    assert second.run().status == STATUS_COMPLETED
    assert first_target.received == ["1"]
    assert second_target.received == ["1"]
    first_fixture.state["order"]["status"] = "cancelled"
    assert second_fixture.state["order"]["status"] == "active"


@pytest.mark.parametrize("step", [
    {"step_id": "x", "kind": "invoke_fixture_tool", "tool": "orders.get", "timeout_sec": -1},
    {"step_id": "x", "kind": "send_message", "message": "m", "timeout_sec": True},
])
def test_invalid_step_timeouts_are_rejected_at_compile_time(step):
    with pytest.raises(Exception):
        WorkflowVersion.model_validate({
            "workflow_id": "wf", "version": "1", "published_at": "2026-09-21T00:00:00Z",
            "steps": [step],
            "limits": {"max_total_steps": 5, "max_turns": 2, "wall_time_sec": 10},
        })
