"""M5-T05 纵向闭环：公共 Run 形状 → CaseAttempt 装配 → 引擎 → 冻结证据 → 清理。

这些用例走的是执行器与引擎的真实装配路径（真实 FixtureRuntime、真实
ArtifactStore、真实条件求值），只有一个**脚本化目标**替代模型驱动，以便在
零模型调用下证明链路。步骤不产生子 Run，也不产生伪 Trial。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from motte_contracts.workflow import WorkflowVersion, workflow_content_hash
from motte_scenario.executor import ScenarioCaseExecutor, ScenarioToolError
from motte_scenario.targets import TargetAdapter, register_target_adapter, unregister_target_adapter

PUBLISHED_AT = "2026-09-21T00:00:00Z"


def fixture_record(**overrides):
    payload = {
        "fixture_id": "order-state",
        "version": 1,
        "kind": "json",
        "initial_data": {"order": {"id": "order-1", "status": "active",
                                   "cancellation_count": 0}},
        "allowed_tools": ["orders.get", "orders.cancel"],
        "visible_fields": ["order"],
        "isolation": "per_case",
        "cleanup": "delete_owned",
        "published_at": PUBLISHED_AT,
        "lifecycle": "published",
    }
    payload.update(overrides)
    return payload


def workflow_record(**overrides):
    payload = {
        "workflow_id": "order-cancel-confirmed",
        "version": "1",
        "published_at": PUBLISHED_AT,
        "fixture_refs": [{"fixture_id": "order-state", "version": 1, "kind": "json"}],
        "steps": [
            {"step_id": "request", "kind": "send_message", "message": "请取消订单 order-1"},
            {"step_id": "before-confirm", "kind": "checkpoint", "assertions": [
                {"op": "eq", "path": "state.order.status", "value": "active"},
                {"op": "eq", "path": "state.order.cancellation_count", "value": 0},
            ]},
            {"step_id": "confirm", "kind": "send_message", "message": "我确认取消"},
            {"step_id": "final-check", "kind": "checkpoint", "assertions": [
                {"op": "eq", "path": "state.order.status", "value": "cancelled"},
                {"op": "eq", "path": "state.order.cancellation_count", "value": 1},
            ]},
        ],
        "limits": {"max_total_steps": 20, "max_turns": 4, "wall_time_sec": 60},
    }
    payload.update(overrides)
    return payload


def run_record(tmp_path, *, workflow=None, fixture=None, case_input=None):
    spec = fixture_record(**(fixture or {}))
    workflow_payload = workflow or workflow_record()
    compiled = WorkflowVersion.model_validate(workflow_payload)
    # manifest.workflow_snapshot 就是已发布 WorkflowVersion 的冻结 dump：解析器
    # 会重新校验它，因此不能在快照里塞额外的展示字段。
    snapshot = compiled.model_dump(mode="json")
    snapshot_hash = workflow_content_hash(compiled)
    spec_hash = "sha256:" + "b" * 64
    return {
        "id": "run-scenario-1",
        "scenario_version": "scenario@1",
        "manifest": {
            "workflow": "order-cancel-confirmed@1",
            "workflow_snapshot": {**snapshot, "content_hash": snapshot_hash},
            "target_snapshot": {"multi_turn": True, "min_turns": 2, "required_tools": [],
                                "tool_modes": ["real"], "interrupt": False,
                                "skill_injection": False, "evidence": ["events"]},
            "fixture_snapshot": {
                "order-state@1": {
                    "fixture_id": "order-state", "version": "1", "kind": "json",
                    "content_hash": spec_hash,
                    "record": {**spec, "content_hash": spec_hash},
                },
            },
            "agent": "scripted-target@1",
            "cases": {"case-1": case_input or {"business_id": "order-1"}},
        },
        "case_ids": ["case-1"],
    }


class ScriptedSession:
    """脚本化目标：第 1 轮询问确认，第 2 轮通过受控工具桥真正取消。"""

    def __init__(self, tools):
        self.tools = tools
        self.received: list[str] = []
        self.began = False
        self.closed = False

    def begin(self):
        if self.began:
            raise RuntimeError("already begun")
        self.began = True
        return {"state": "active"}

    def send(self, message, *, deadline=None):
        self.received.append(message)
        if len(self.received) == 1:
            return {"output": "请确认是否取消 order-1", "termination_reason": "final_answer"}
        result = self.tools["orders.cancel"]({"order_id": "order-1"})
        return {"output": "已取消", "termination_reason": "final_answer", "tool_result": result}

    def observe(self):
        return {"turns": len(self.received)}

    def interrupt(self, reason):
        return {"reason": reason, "confirmed": True}

    def close(self):
        self.closed = True
        return {"state": "closed"}


@pytest.fixture
def scripted_target():
    created: list[ScriptedSession] = []

    def open_session(context):
        session = ScriptedSession(context.get("tools") or {})
        created.append(session)
        return session

    register_target_adapter(TargetAdapter(
        kind="scripted-target",
        capabilities=lambda _manifest: __import__(
            "motte_scenario.targets", fromlist=["TargetCapabilities"]
        ).TargetCapabilities(
            kind="scripted-target", multi_turn=True,
            tool_modes=("real", "mock", "replay", "deny"),
            tools=("orders.get", "orders.cancel"), interrupt=True,
            evidence=("events", "invocations", "artifacts"),
        ),
        open_session=open_session,
    ), replace=True)
    yield created
    unregister_target_adapter("scripted-target")


def cancel_handler(state, arguments):
    order = dict(state["order"])
    if order.get("status") == "cancelled":
        return None, {"ok": True, "already": True,
                      "cancellation_count": order["cancellation_count"]}
    order["status"] = "cancelled"
    order["cancellation_count"] = int(order.get("cancellation_count", 0)) + 1
    return {**state, "order": order}, {
        "ok": True, "status": order["status"],
        "cancellation_count": order["cancellation_count"],
        "checker_truth": "must never leak",
    }


def handler_table():
    return {"orders.cancel": cancel_handler}


def test_confirmed_cancellation_flow_reaches_the_public_envelope(tmp_path, scripted_target):
    run = run_record(tmp_path)
    executor = ScenarioCaseExecutor(
        run, fixture_anchor=str(tmp_path / "fx"), tool_handlers=handler_table(),
    )
    envelope = executor.invoke("case-1")

    assert envelope["scenario"]["status"] == "completed"
    assert envelope["scenario"]["turns"] == 2
    assert envelope["scenario"]["target_kind"] == "scripted-target"
    assert envelope["scenario"]["needs_review"] is False

    observation = envelope["observation"]
    assert observation["schema"] == "workflow-observation@1"
    assert observation["workflow_ref"] == "order-cancel-confirmed@1"
    assert [step["step_id"] for step in observation["steps"]] == [
        "request", "before-confirm", "confirm", "final-check",
    ]
    assert observation["final_state"]["order"]["cancellation_count"] == 1
    assert observation["final_state"]["order"]["status"] == "cancelled"
    assert observation["final_output"] == "已取消"
    # checkpoint 以 Artifact + 内容 hash 落盘，并带 run/case 归属。
    assert len(envelope["observation"]["checkpoints"]) == 2
    assert envelope["artifacts_captured"] == 2
    for record in observation["checkpoints"].values():
        assert record["content_hash"].startswith("sha256:")

    # 清理：受控根被删除，且证据显示成功。
    assert envelope["cleanup"] and all(
        item["status"] == "success" for item in envelope["cleanup"]
    )
    assert not (tmp_path / "fx" / "instances").exists() or not any(
        (tmp_path / "fx" / "instances").iterdir()
    )
    # 目标没有看到 checker 真值（工具结果里的隐藏字段被剔除）。
    assert "checker_truth" not in json.dumps(envelope, ensure_ascii=False)


def test_two_cases_with_the_same_business_id_do_not_share_state(tmp_path, scripted_target):
    run = run_record(tmp_path)
    executor = ScenarioCaseExecutor(
        run, fixture_anchor=str(tmp_path / "fx"), tool_handlers=handler_table(),
    )
    first = executor.invoke("case-1")
    second = executor.invoke("case-1")
    assert first["scenario"]["status"] == "completed"
    assert second["scenario"]["status"] == "completed"
    # 两个 Case 各自从未取消的初态开始：第二次仍然是 count=1，而不是 2。
    assert first["observation"]["final_state"]["order"]["cancellation_count"] == 1
    assert second["observation"]["final_state"]["order"]["cancellation_count"] == 1
    assert first["observation"]["final_state"] == second["observation"]["final_state"]
    assert len(scripted_target) == 2


def test_process_failure_is_visible_in_the_frozen_observation(tmp_path, scripted_target):
    """未确认就取消：终态正确但过程断言失败，公共证据必须保留这一点。"""
    workflow = workflow_record(steps=[
        {"step_id": "request", "kind": "send_message", "message": "请取消"},
        {"step_id": "before-confirm", "kind": "checkpoint", "assertions": [
            {"op": "eq", "path": "state.order.status", "value": "active"},
        ]},
        {"step_id": "final-check", "kind": "checkpoint", "assertions": [
            {"op": "eq", "path": "state.order.status", "value": "cancelled"},
        ]},
    ])

    class ImmediateCancel(ScriptedSession):
        def send(self, message, *, deadline=None):
            self.received.append(message)
            self.tools["orders.cancel"]({"order_id": "order-1"})
            return {"output": "已取消", "termination_reason": "final_answer"}

    register_target_adapter(TargetAdapter(
        kind="scripted-target",
        capabilities=lambda _manifest: __import__(
            "motte_scenario.targets", fromlist=["TargetCapabilities"]
        ).TargetCapabilities(
            kind="scripted-target", multi_turn=True,
            tool_modes=("real",), tools=("orders.cancel",), interrupt=True,
        ),
        open_session=lambda context: ImmediateCancel(context.get("tools") or {}),
    ), replace=True)

    run = run_record(tmp_path, workflow=workflow)
    envelope = ScenarioCaseExecutor(
        run, fixture_anchor=str(tmp_path / "fx"), tool_handlers=handler_table(),
    ).invoke("case-1")
    assert envelope["scenario"]["status"] == "failed"
    assert envelope["observation"]["final_state"]["order"]["status"] == "cancelled"
    steps = {step["step_id"]: step for step in envelope["observation"]["steps"]}
    assert steps["before-confirm"]["status"] == "failed"
    assert "final-check" not in steps


def test_unimplemented_fixture_tool_is_refused_instead_of_faked(tmp_path, scripted_target):
    workflow = workflow_record(steps=[
        {"step_id": "call", "kind": "invoke_fixture_tool", "tool": "orders.refund",
         "arguments": {"order_id": "order-1"}},
    ])
    run = run_record(tmp_path, fixture={"allowed_tools": ["orders.refund"]},
                     workflow={**workflow})
    envelope = ScenarioCaseExecutor(
        run, fixture_anchor=str(tmp_path / "fx"), tool_handlers=handler_table(),
    ).invoke("case-1")
    assert envelope["scenario"]["status"] == "failed"
    assert "SCENARIO_TOOL_NOT_IMPLEMENTED" in (envelope["scenario"]["reason"] or "")


def test_denied_tool_never_mutates_the_fixture(tmp_path, scripted_target):
    workflow = workflow_record(steps=[
        {"step_id": "call", "kind": "invoke_fixture_tool", "tool": "orders.cancel",
         "arguments": {"order_id": "order-1"}, "tool_mode": "deny"},
        {"step_id": "check", "kind": "checkpoint", "assertions": [
            {"op": "eq", "path": "state.order.cancellation_count", "value": 0},
        ]},
    ])
    run = run_record(tmp_path, workflow=workflow)
    envelope = ScenarioCaseExecutor(
        run, fixture_anchor=str(tmp_path / "fx"), tool_handlers=handler_table(),
    ).invoke("case-1")
    assert envelope["scenario"]["status"] == "failed"
    assert "SCENARIO_TOOL_DENIED" in (envelope["scenario"]["reason"] or "")
    assert envelope["tool_log"][0]["status"] == "denied"


def test_unsupported_fixture_isolation_is_refused_without_leaving_residue(
    tmp_path, scripted_target,
):
    """per_run 共享根没有租约协议：必须在建立任何实例之前具名拒绝。"""
    run = run_record(tmp_path, fixture={"isolation": "per_run"})
    with pytest.raises(Exception, match="isolation"):
        ScenarioCaseExecutor(
            run, fixture_anchor=str(tmp_path / "fx"), tool_handlers=handler_table(),
        ).invoke("case-1")
    instances = tmp_path / "fx" / "instances"
    assert not instances.exists() or not any(instances.iterdir())
