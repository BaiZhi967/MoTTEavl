"""M5-G06 / M5-A06：阻塞 Target 必须被**真实终止**，不是线程 join 超时返回。

这些用例用 M4 的受控进程监督（SupervisedProcess）跑一个真的会阻塞并持续
产生副作用的子进程：引擎的期限到期后必须确认进程已停止，无法确认时保留
证据并转 needs_review。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from motte_scenario.compiler import compile_workflow
from motte_scenario.engine import STATUS_NEEDS_REVIEW, WorkflowEngine
from motte_scenario.process_target import ProcessTarget, ProcessTargetError

PROGRAM = str(Path(__file__).resolve().parents[1] / "fixtures" / "scenario" / "blocking_target.py")


class NoopFixture:
    def __init__(self) -> None:
        self._state = {"order": {"status": "active", "cancellation_count": 0}}

    @property
    def state(self):
        return self._state

    def snapshot(self):
        import hashlib
        import json

        payload = json.dumps(self._state, sort_keys=True)
        return dict(self._state), "sha256:" + hashlib.sha256(payload.encode()).hexdigest()

    def invoke_tool(self, name, arguments, mode):
        return {"ok": True}

    def apply_event(self, event, payload):
        return {"event": event}


def make_target(ready_timeout=30.0):
    return ProcessTarget([sys.executable, "-u", PROGRAM], ready_timeout=ready_timeout)


def test_confirmed_stop_after_a_blocking_send():
    target = make_target()
    target.begin()
    try:
        started = time.monotonic()
        result = target.send("please block now", deadline=time.monotonic() + 1.5)
        elapsed = time.monotonic() - started
        assert result["termination_reason"] == "per_call_timeout"
        assert result["timeout"] is True
        # 真实停止：不是"等它自己结束"，而是确认进程树已经退出。
        assert result["stopped"] is True
        assert result["interrupt"]["residual_pids"] == []
        assert elapsed < 20.0
    finally:
        target.close()


def test_a_normal_send_round_trips_before_the_deadline():
    target = make_target()
    target.begin()
    try:
        first = target.send("请确认", deadline=time.monotonic() + 10)
        assert first["output"] == "请确认"
        second = target.send("cancel order-1", deadline=time.monotonic() + 10)
        assert second["output"] == "已取消"
    finally:
        target.close()


def test_close_reports_residual_processes_as_unconfirmed():
    target = make_target()
    target.begin()
    evidence = target.interrupt("operator")
    assert evidence["confirmed"] is True
    closed = target.close()
    assert closed["state"] == "closed"
    with pytest.raises(ProcessTargetError):
        target.send("after close", deadline=time.monotonic() + 1)


def test_engine_deadline_stops_the_blocking_target_and_fails_the_case():
    workflow = compile_workflow({
        "workflow_id": "blocking-target",
        "version": "1",
        "published_at": "2026-09-21T00:00:00Z",
        "steps": [
            {"step_id": "block", "kind": "send_message", "message": "block",
             "timeout_sec": 1.5},
        ],
        "limits": {"max_total_steps": 5, "max_turns": 2, "wall_time_sec": 30},
    })
    target = make_target()
    engine = WorkflowEngine(workflow, NoopFixture(), target)
    outcome = engine.run()
    assert outcome.status == "failed"
    assert outcome.needs_review is False
    assert outcome.interrupt is not None
    assert outcome.interrupt["confirmed"] is True
    assert outcome.interrupt["residual_pids"] == []


def test_engine_reports_needs_review_when_the_stop_cannot_be_confirmed():
    class Unconfirmable(ProcessTarget):
        def interrupt(self, reason):
            return {"reason": reason, "confirmed": False, "residual_pids": [999999]}

    workflow = compile_workflow({
        "workflow_id": "blocking-target",
        "version": "1",
        "published_at": "2026-09-21T00:00:00Z",
        "steps": [
            {"step_id": "block", "kind": "send_message", "message": "block",
             "timeout_sec": 1.0},
        ],
        "limits": {"max_total_steps": 5, "max_turns": 2, "wall_time_sec": 30},
    })
    target = Unconfirmable([sys.executable, "-u", PROGRAM])
    engine = WorkflowEngine(workflow, NoopFixture(), target)
    try:
        outcome = engine.run()
    finally:
        # 清理：绕过被覆写的 interrupt，直接让真实监督者停掉子进程。
        ProcessTarget.interrupt(target, "cleanup")
    assert outcome.status == STATUS_NEEDS_REVIEW
    assert outcome.needs_review is True
    assert outcome.interrupt["residual_pids"] == [999999]
