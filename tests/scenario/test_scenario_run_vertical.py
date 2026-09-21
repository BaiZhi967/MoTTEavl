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
from motte_scenario.fixtures import FixturePrepareError, FixtureRuntime
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


def run_record(tmp_path, *, workflow=None, fixture=None, case_input=None, target=None,
               extra_fixtures=None):
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
            "target_snapshot": {
                "multi_turn": True, "min_turns": 2, "required_tools": [],
                "tool_modes": ["real"], "interrupt": False,
                "skill_injection": False, "evidence": ["events"], **(target or {}),
            },
            "fixture_snapshot": {
                "order-state@1": {
                    "fixture_id": "order-state", "version": "1", "kind": "json",
                    "content_hash": spec_hash,
                    "record": {**spec, "content_hash": spec_hash},
                },
                **(extra_fixtures or {}),
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


def test_nested_gold_in_state_and_tool_results_never_reaches_the_target(tmp_path):
    """F07：state 与工具结果里的嵌套 gold 都不能进入 Target 可见输出。"""
    fixture = fixture_record(
        initial_data={"order": {"id": "order-1", "status": "active",
                                "cancellation_count": 0, "gold": "state-secret"}},
        visible_fields=["order"],
    )
    workflow = workflow_record(steps=[
        {"step_id": "call", "kind": "invoke_fixture_tool", "tool": "orders.cancel",
         "arguments": {"order_id": "order-1"}},
        {"step_id": "check", "kind": "checkpoint", "assertions": [
            {"op": "eq", "path": "state.order.status", "value": "cancelled"},
        ]},
        {"step_id": "request", "kind": "send_message", "message": "go"},
    ])

    def nested_handler(state, arguments):  # noqa: ARG001 - 工具协议签名
        order = dict(state["order"])
        order["status"] = "cancelled"
        return {**state, "order": order}, {
            "order": {**order, "gold": "result-secret"},
            "line_items": [{"sku": "a", "gold": "list-secret"}],
        }

    received: list = []

    class RecordingSession(ScriptedSession):
        def send(self, message, *, deadline=None):
            self.received.append(message)
            received.append(self.tools["orders.cancel"]({"order_id": "order-1"}))
            return {"output": "已取消", "termination_reason": "final_answer"}

    register_target_adapter(TargetAdapter(
        kind="scripted-target",
        capabilities=lambda _manifest: __import__(
            "motte_scenario.targets", fromlist=["TargetCapabilities"]
        ).TargetCapabilities(
            kind="scripted-target", multi_turn=True,
            tool_modes=("real",), tools=("orders.cancel",), interrupt=True,
        ),
        open_session=lambda context: RecordingSession(context.get("tools") or {}),
    ), replace=True)
    try:
        run = run_record(tmp_path, fixture=fixture, workflow=workflow)
        envelope = ScenarioCaseExecutor(
            run, fixture_anchor=str(tmp_path / "fx"),
            tool_handlers={"orders.cancel": nested_handler},
        ).invoke("case-1")
    finally:
        unregister_target_adapter("scripted-target")

    assert envelope["scenario"]["status"] == "completed"
    assert envelope["observation"]["final_state"]["order"]["status"] == "cancelled"
    # 三个真值字符串一个都不能出现在任何可见输出里
    text = json.dumps(envelope, ensure_ascii=False)
    for secret in ("state-secret", "result-secret", "list-secret"):
        assert secret not in text
    # observation / 工具日志 / 目标收到的工具结果都不含隐藏键
    assert "gold" not in json.dumps(envelope["observation"], ensure_ascii=False)
    assert "gold" not in json.dumps(envelope["tool_log"], ensure_ascii=False)
    assert received and received[0]["order"]["status"] == "cancelled"
    assert "secret" not in json.dumps(received[0], ensure_ascii=False)
    assert "gold" not in json.dumps(received[0], ensure_ascii=False)


def test_mock_and_replay_steps_consume_their_own_sources_not_the_real_handler(
    tmp_path, scripted_target,
):
    """F06：mock/replay 各自消费明确的实现；real handler 一次都不被调用。"""
    workflow = workflow_record(steps=[
        {"step_id": "mock-call", "kind": "invoke_fixture_tool", "tool": "orders.cancel",
         "arguments": {"order_id": "order-1"}, "tool_mode": "mock"},
        {"step_id": "after-mock", "kind": "assert", "assertions": [
            {"op": "eq", "path": "state.order.status", "value": "mock-cancelled"},
        ]},
        {"step_id": "replay-call", "kind": "invoke_fixture_tool", "tool": "orders.get",
         "arguments": {"order_id": "order-1"}, "tool_mode": "replay"},
        {"step_id": "after-replay", "kind": "assert", "assertions": [
            {"op": "eq", "path": "state.order.status", "value": "replayed"},
        ]},
    ])
    real_calls: list = []
    mock_calls: list = []

    def real_cancel(state, arguments):  # noqa: ARG001 - 工具协议签名
        real_calls.append(arguments)
        return {"ok": True}

    def mock_cancel(state, arguments):  # noqa: ARG001 - 工具协议签名
        mock_calls.append(arguments)
        order = dict(state["order"])
        order["status"] = "mock-cancelled"
        return {**state, "order": order}, {"ok": True, "source": "mock"}

    run = run_record(tmp_path, workflow=workflow)
    envelope = ScenarioCaseExecutor(
        run, fixture_anchor=str(tmp_path / "fx"),
        tool_handlers={"orders.cancel": real_cancel, "orders.get": real_cancel},
        mock_handlers={"orders.cancel": mock_cancel},
        replay_records={"orders.get": {
            "state": {"order": {"id": "order-1", "status": "replayed",
                                "cancellation_count": 0}},
            "result": {"order": {"id": "order-1", "status": "replayed"}},
        }},
    ).invoke("case-1")

    assert envelope["scenario"]["status"] == "completed", envelope["scenario"]["reason"]
    assert real_calls == []
    assert mock_calls == [{"order_id": "order-1"}]
    assert envelope["observation"]["final_state"]["order"]["status"] == "replayed"
    modes = {entry["tool"]: entry["mode"] for entry in envelope["tool_log"]}
    assert modes == {"orders.cancel": "mock", "orders.get": "replay"}


@pytest.mark.parametrize(
    ("mode", "code"),
    [("mock", "SCENARIO_TOOL_MOCK_MISSING"), ("replay", "SCENARIO_TOOL_REPLAY_MISSING")],
)
def test_a_mode_without_its_own_source_is_refused_and_never_falls_back_to_real(
    tmp_path, scripted_target, mode, code,
):
    """F06：mock/replay 没有配置来源时具名拒绝，绝不回退到 real handler。"""
    workflow = workflow_record(steps=[
        {"step_id": "call", "kind": "invoke_fixture_tool", "tool": "orders.cancel",
         "arguments": {"order_id": "order-1"}, "tool_mode": mode},
    ])
    real_calls: list = []

    def real_cancel(state, arguments):  # noqa: ARG001 - 工具协议签名
        real_calls.append(arguments)
        return {"ok": True}

    run = run_record(tmp_path, workflow=workflow)
    envelope = ScenarioCaseExecutor(
        run, fixture_anchor=str(tmp_path / "fx"),
        tool_handlers={"orders.cancel": real_cancel},
    ).invoke("case-1")

    assert envelope["scenario"]["status"] == "failed"
    assert code in (envelope["scenario"]["reason"] or "")
    assert real_calls == []
    assert envelope["observation"]["final_state"]["order"]["status"] == "active"


def test_target_tool_bridge_uses_the_frozen_mode_instead_of_hardcoded_real(
    tmp_path, scripted_target,
):
    """F06：目标自身调用也要消费冻结的有效 mode，而不是固定 real。"""
    workflow = workflow_record(steps=[
        {"step_id": "request", "kind": "send_message", "message": "请取消"},
        {"step_id": "confirm", "kind": "send_message", "message": "确认"},
        {"step_id": "check", "kind": "checkpoint", "assertions": [
            {"op": "eq", "path": "state.order.status", "value": "mock-cancelled"},
        ]},
    ])
    real_calls: list = []
    mock_calls: list = []

    def real_cancel(state, arguments):  # noqa: ARG001 - 工具协议签名
        real_calls.append(arguments)
        order = dict(state["order"])
        order["status"] = "real-cancelled"
        return {**state, "order": order}, {"ok": True}

    def mock_cancel(state, arguments):  # noqa: ARG001 - 工具协议签名
        mock_calls.append(arguments)
        order = dict(state["order"])
        order["status"] = "mock-cancelled"
        return {**state, "order": order}, {"ok": True}

    run = run_record(tmp_path, workflow=workflow, target={"tool_modes": ["mock"]})
    envelope = ScenarioCaseExecutor(
        run, fixture_anchor=str(tmp_path / "fx"),
        tool_handlers={"orders.cancel": real_cancel},
        mock_handlers={"orders.cancel": mock_cancel},
    ).invoke("case-1")

    assert envelope["scenario"]["status"] == "completed", envelope["scenario"]["reason"]
    assert real_calls == []
    assert mock_calls == [{"order_id": "order-1"}]


def test_target_tool_bridge_refuses_an_ambiguous_frozen_mode(tmp_path, scripted_target):
    """F06：冻结声明里没有 real 又不止一个模式时，不猜有效 mode，直接拒绝。"""
    workflow = workflow_record(steps=[
        {"step_id": "request", "kind": "send_message", "message": "请取消"},
        {"step_id": "confirm", "kind": "send_message", "message": "确认"},
    ])
    real_calls: list = []

    def real_cancel(state, arguments):  # noqa: ARG001 - 工具协议签名
        real_calls.append(arguments)
        return {"ok": True}

    run = run_record(tmp_path, workflow=workflow, target={"tool_modes": ["mock", "replay"]})
    executor = ScenarioCaseExecutor(
        run, fixture_anchor=str(tmp_path / "fx"),
        tool_handlers={"orders.cancel": real_cancel},
        mock_handlers={"orders.cancel": real_cancel},
        replay_records={"orders.cancel": {"result": {"ok": True}}},
    )
    with pytest.raises(ScenarioToolError) as excinfo:
        executor.invoke("case-1")
    assert excinfo.value.code == "SCENARIO_TOOL_MODE_AMBIGUOUS"
    assert real_calls == []
    # 拒绝发生在任何业务动作之前，现场也被回收（没有静默残留）
    instances = tmp_path / "fx" / "instances"
    assert not instances.exists() or not any(instances.iterdir())


def test_unconfirmed_stop_retains_the_fixture_and_its_ownership_marker(tmp_path):
    """F08：停止无法确认时保留现场与审计证据，且 owner 可核验。"""
    workflow = workflow_record(steps=[
        {"step_id": "request", "kind": "send_message", "message": "block",
         "timeout_sec": 1.0},
    ])

    class UnconfirmedStop:
        def begin(self):
            return {"state": "active"}

        def send(self, message, *, deadline=None):  # noqa: ARG002 - TargetPort
            return {"output": None, "termination_reason": "per_call_timeout"}

        def observe(self):
            return {"state": "active"}

        def interrupt(self, reason):
            return {"reason": reason, "confirmed": False, "residual_pids": [4242]}

        def close(self):
            return {"state": "unknown"}

    register_target_adapter(TargetAdapter(
        kind="scripted-target",
        capabilities=lambda _manifest: __import__(
            "motte_scenario.targets", fromlist=["TargetCapabilities"]
        ).TargetCapabilities(
            kind="scripted-target", multi_turn=True, tool_modes=("real",),
            tools=("orders.cancel",), interrupt=True,
        ),
        open_session=lambda _context: UnconfirmedStop(),
    ), replace=True)
    try:
        run = run_record(tmp_path, workflow=workflow)
        envelope = ScenarioCaseExecutor(
            run, fixture_anchor=str(tmp_path / "fx"), tool_handlers=handler_table(),
        ).invoke("case-1")
    finally:
        unregister_target_adapter("scripted-target")

    assert envelope["scenario"]["status"] == "needs_review"
    assert envelope["scenario"]["needs_review"] is True
    cleanup = envelope["cleanup"]
    assert cleanup and cleanup[0]["status"] == "unknown"
    assert cleanup[0]["interrupted"] is True
    assert cleanup[0]["residual"]

    instance_root = tmp_path / "fx" / "instances" / cleanup[0]["instance_id"]
    marker_path = instance_root / "_instance.json"
    assert marker_path.is_file()  # 所有权标记仍然留在现场
    assert (instance_root / "state" / "state.json").is_file()
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["instance_id"] == cleanup[0]["instance_id"]
    assert marker["owner_token_sha256"].startswith("sha256:")
    assert len(marker["owner_token_sha256"]) == len("sha256:") + 64
    evidence = tmp_path / "fx" / "_cleanup" / f"{cleanup[0]['instance_id']}.json"
    assert evidence.is_file()
    record = json.loads(evidence.read_text(encoding="utf-8"))
    assert record["status"] == "unknown"
    assert record["interrupted"] is True


def test_a_failing_second_prepare_cleans_the_first_instance_with_evidence(
    tmp_path, scripted_target, monkeypatch,
):
    """F14：第二个 prepare 失败时，第一个已成功实例必须被清理并留证。"""
    second = {
        "order-z@1": {
            "fixture_id": "order-z", "version": "1", "kind": "json",
            "content_hash": "sha256:" + "c" * 64,
            "record": {
                "fixture_id": "order-z", "version": 1, "kind": "json",
                "initial_data": {"order": {"id": "order-z"}},
                "published_at": PUBLISHED_AT, "lifecycle": "published",
                "content_hash": "sha256:" + "c" * 64,
            },
        },
    }
    run = run_record(tmp_path, extra_fixtures=second)
    original = FixtureRuntime.prepare

    def faulty(self, spec, **kwargs):
        if spec.fixture_id == "order-z":
            raise RuntimeError("second prepare fails")
        return original(self, spec, **kwargs)

    monkeypatch.setattr(FixtureRuntime, "prepare", faulty)
    executor = ScenarioCaseExecutor(
        run, fixture_anchor=str(tmp_path / "fx"), tool_handlers=handler_table(),
    )
    with pytest.raises(RuntimeError, match="second prepare fails") as excinfo:
        executor.invoke("case-1")

    # 原错误保留（并带上清理结论），第一个实例的现场被回收且留有清理记录
    assert getattr(excinfo.value, "cleanups", None)
    assert excinfo.value.cleanups[0]["status"] == "success"

    anchor = tmp_path / "fx"
    evidence = sorted((anchor / "_cleanup").glob("*.json"))
    assert len(evidence) == 1
    record = json.loads(evidence[0].read_text(encoding="utf-8"))
    assert record["status"] == "success"
    instances = anchor / "instances"
    assert not instances.exists() or not any(instances.iterdir())


def test_a_failing_second_prepare_of_a_real_fixture_still_cleans_the_first(
    tmp_path, scripted_target,
):
    """F14：第二个 fixture 真的初始化失败（FixturePrepareError）时同样无残留。"""
    second = {
        "order-z@1": {
            "fixture_id": "order-z", "version": "1", "kind": "sqlite",
            "content_hash": "sha256:" + "d" * 64,
            "record": {
                "fixture_id": "order-z", "version": 1, "kind": "sqlite",
                "initial_data": {
                    "schema": ["CREATE TABLE orders (id TEXT PRIMARY KEY)"],
                    "rows": {"missing_table": [{"id": "order-1"}]},
                },
                "published_at": PUBLISHED_AT, "lifecycle": "published",
                "content_hash": "sha256:" + "d" * 64,
            },
        },
    }
    run = run_record(tmp_path, extra_fixtures=second)
    executor = ScenarioCaseExecutor(
        run, fixture_anchor=str(tmp_path / "fx"), tool_handlers=handler_table(),
    )
    with pytest.raises(FixturePrepareError) as excinfo:
        executor.invoke("case-1")

    assert "missing_table" in (excinfo.value.instance.prepare_error or "")
    assert getattr(excinfo.value, "cleanups", None)
    assert excinfo.value.cleanups[0]["status"] == "success"

    anchor = tmp_path / "fx"
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((anchor / "_cleanup").glob("*.json"))
    ]
    assert [record["status"] for record in records] == ["success", "success"]
    instances = anchor / "instances"
    assert not instances.exists() or not any(instances.iterdir())


class _BrokenPhaseSession:
    """生命周期某一阶段失败的 Target；其它阶段保持最小可用。"""

    def __init__(self, *, begin_error=None, send_error=None, close_error=None) -> None:
        self.begin_error = begin_error
        self.send_error = send_error
        self.close_error = close_error
        self.closed = False

    def begin(self):
        if self.begin_error is not None:
            raise self.begin_error
        return {"state": "active"}

    def send(self, message, *, deadline=None):  # noqa: ARG002 - TargetPort
        if self.send_error is not None:
            raise self.send_error
        return {"output": "ok", "termination_reason": "final_answer"}

    def observe(self):
        return {"state": "active"}

    def interrupt(self, reason):
        return {"reason": reason, "confirmed": True}

    def close(self):
        if self.close_error is not None:
            raise self.close_error
        self.closed = True
        return {"state": "closed"}


def _register_phase_target(session):
    register_target_adapter(TargetAdapter(
        kind="scripted-target",
        capabilities=lambda _manifest: __import__(
            "motte_scenario.targets", fromlist=["TargetCapabilities"]
        ).TargetCapabilities(
            kind="scripted-target", multi_turn=True, tool_modes=("real",),
            tools=("orders.cancel",), interrupt=True,
        ),
        open_session=lambda _context: session,
    ), replace=True)


def test_a_target_begin_failure_keeps_the_original_error_and_the_cleanup_record(tmp_path):
    """R1 验收：begin 失败时原错误与清理结论都要留住，且没有静默残留。"""
    _register_phase_target(_BrokenPhaseSession(begin_error=RuntimeError("cannot start target")))
    try:
        run = run_record(tmp_path)
        envelope = ScenarioCaseExecutor(
            run, fixture_anchor=str(tmp_path / "fx"), tool_handlers=handler_table(),
        ).invoke("case-1")
    finally:
        unregister_target_adapter("scripted-target")

    assert envelope["scenario"]["status"] == "failed"
    assert "cannot start target" in (envelope["scenario"]["reason"] or "")
    assert [item["status"] for item in envelope["cleanup"]] == ["success"]
    instances = tmp_path / "fx" / "instances"
    assert not instances.exists() or not any(instances.iterdir())


def test_a_target_send_failure_keeps_the_original_error_and_the_cleanup_record(tmp_path):
    _register_phase_target(_BrokenPhaseSession(send_error=RuntimeError("send exploded")))
    try:
        run = run_record(tmp_path)
        envelope = ScenarioCaseExecutor(
            run, fixture_anchor=str(tmp_path / "fx"), tool_handlers=handler_table(),
        ).invoke("case-1")
    finally:
        unregister_target_adapter("scripted-target")

    assert envelope["scenario"]["status"] == "failed"
    steps = {step["step_id"]: step for step in envelope["observation"]["steps"]}
    assert "send exploded" in (steps["request"]["detail"] or "")
    assert [item["status"] for item in envelope["cleanup"]] == ["success"]
    instances = tmp_path / "fx" / "instances"
    assert not instances.exists() or not any(instances.iterdir())


def test_a_target_close_failure_retains_the_fixture_for_review(tmp_path):
    """F08 相邻面：close 失败同样是"停止未确认"，现场必须保留。"""
    _register_phase_target(_BrokenPhaseSession(close_error=RuntimeError("cannot confirm closed")))
    workflow = workflow_record(steps=[
        {"step_id": "request", "kind": "send_message", "message": "go"},
    ])
    try:
        run = run_record(tmp_path, workflow=workflow)
        envelope = ScenarioCaseExecutor(
            run, fixture_anchor=str(tmp_path / "fx"), tool_handlers=handler_table(),
        ).invoke("case-1")
    finally:
        unregister_target_adapter("scripted-target")

    assert envelope["scenario"]["status"] == "needs_review"
    cleanup = envelope["cleanup"]
    assert cleanup and cleanup[0]["status"] == "unknown"
    assert cleanup[0]["interrupted"] is True
    instance_root = tmp_path / "fx" / "instances" / cleanup[0]["instance_id"]
    assert (instance_root / "_instance.json").is_file()
    assert cleanup[0]["evidence_path"]


def test_an_ordinary_process_failure_with_a_close_failure_still_retains_the_fixture(
    tmp_path,
):
    """终局是 failed 但停止未确认时，资源同样保留（needs_review 标记独立生效）。"""
    _register_phase_target(_BrokenPhaseSession(close_error=RuntimeError("cannot confirm closed")))
    try:
        run = run_record(tmp_path)  # 默认流程的 final-check 会因目标没有真正取消而失败
        envelope = ScenarioCaseExecutor(
            run, fixture_anchor=str(tmp_path / "fx"), tool_handlers=handler_table(),
        ).invoke("case-1")
    finally:
        unregister_target_adapter("scripted-target")

    assert envelope["scenario"]["status"] == "failed"
    assert envelope["scenario"]["needs_review"] is True
    cleanup = envelope["cleanup"]
    assert cleanup and cleanup[0]["status"] == "unknown"
    assert cleanup[0]["interrupted"] is True
    instance_root = tmp_path / "fx" / "instances" / cleanup[0]["instance_id"]
    assert (instance_root / "_instance.json").is_file()
