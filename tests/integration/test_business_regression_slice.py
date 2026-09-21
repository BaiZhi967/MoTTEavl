"""M5-T12 / R6：代表性业务边界的公共回归切片。

七个业务边界都走**同一条公共链路**（API 创建 → 普通 WorkerLoop → 冻结证据 →
既有评分入口 → 报告），不用多个互不相干的单元测试替代主流程：

1. 成功：终态与过程（先确认后取消）同时满足；
2. 缺确认但终态正确：过程指标失败，goal 不被正确终态掩盖；
3. 工具暂态失败后恢复：失败的调用与恢复都留在冻结动作日志里；
4. 超时：越过步骤期限的那一步失败，Run 不可能报告成功；
5. 越权：平台 ∩ fixture 权限拒绝副作用，尝试本身留证；
6. 重复副作用：终态仍是 cancelled，但重复调用被过程指标发现；
7. 进程崩溃：缺证据只能是 insufficient，不产生 pass；

另加一条监测覆盖反例：停止未确认时动作日志不完整，no-side-effect 只能
insufficient（不能给出"已确认无副作用"的否定结论）。
"""
from __future__ import annotations

import pytest

from .test_scenario_run_backend import (
    ACTION_LOG_EVIDENCE_ID,
    SCENARIO_NAME,
    TARGET_KIND,
    WORKFLOW_ID,
    cancel_once_metric,
    case_row,
    create_order_run,
    default_metrics,
    fixture_spec,
    no_refund_metric,
    order_cancelled_metric,
    publish_order_scenario,
    run_worker,
    scores_by_metric,
    workflow_body,
)
from .test_scenario_run_backend import environment as _environment_fixture
from .test_scenario_run_backend import targets as _targets_fixture
from .test_scenario_run_backend import tools as _tools_fixture


#: 复用第一条公共链路用例的装配（app / 脚本目标 / 业务工具），只换业务剧本。
@pytest.fixture
def scenario_app(_environment_fixture):  # noqa: F811 - pytest fixture 透传
    return _environment_fixture


@pytest.fixture
def scripted_targets(_targets_fixture):  # noqa: F811 - pytest fixture 透传
    return _targets_fixture


@pytest.fixture
def business_tools(_tools_fixture):  # noqa: F811 - pytest fixture 透传
    return _tools_fixture


def run_public_case(scenario_app, scripted_targets, business_tools, *, behavior, workflow=None,
                    metrics=None, extra_metrics=None, case_ids=("case-1",)):
    """公共链路跑一个 Case；返回 (run_id, run_view, case_envelope)。"""
    scripted_targets.set_behavior(behavior)
    publish_order_scenario(
        scenario_app.client, scenario_app.resources, workflow=workflow,
        metrics=metrics if metrics is not None else default_metrics(),
    )
    created = create_order_run(scenario_app.client, case_ids=case_ids)
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]
    result = run_worker(scenario_app.app, run_id)
    assert result is not None
    view = scenario_app.client.get(f"/api/v1/runs/{run_id}").json()
    return run_id, view, case_row(scenario_app, run_id)["result"]


def test_business_success_satisfies_state_and_process_metrics(scenario_app, scripted_targets, business_tools):
    """1. 成功：确认后只取消一次，终态、过程与 goal 全部通过。"""
    run_id, view, envelope = run_public_case(
        scenario_app, scripted_targets, business_tools, behavior="confirm-then-cancel",
    )
    assert view["status"] == "completed"
    assert envelope["scenario"]["status"] == "completed"
    scores = scores_by_metric(view)
    assert scores["confirm-before-cancel"]["passed"] is True
    assert scores["goal"]["passed"] is True
    assert all(score["passed"] is True for score in scores.values())
    assert [call["tool_name"] for call in envelope["frozen_observation"]["tool_calls"]] == [
        "orders.get", "orders.cancel",
    ]


def test_business_missing_confirmation_with_correct_final_state(scenario_app, scripted_targets, business_tools):
    """2. 缺确认但终态正确：过程失败，goal 不被正确终态掩盖。"""
    workflow = workflow_body(failure_policy="continue_for_evidence")
    run_id, view, envelope = run_public_case(
        scenario_app, scripted_targets, business_tools, behavior="cancel-without-confirmation",
        workflow=workflow,
    )
    assert envelope["scenario"]["status"] == "failed"
    # 失败后只读 checkpoint 仍然取证：终态证据是完整的。
    assert envelope["observation"]["final_state"]["order"]["status"] == "cancelled"
    scores = scores_by_metric(view)
    assert scores["order-cancelled"]["metric_status"] == "scored"
    assert scores["order-cancelled"]["passed"] is True
    assert scores["confirm-before-cancel"]["passed"] is False
    assert scores["confirm-before-cancel"]["reason"] == "required_match_missing"
    assert scores["goal"]["passed"] is False
    assert scores["goal"]["reason"] == "goal_not_achieved"


def test_business_transient_tool_error_then_recovery(scenario_app, scripted_targets, business_tools):
    """3. 工具暂态失败后恢复：失败与恢复都在冻结动作日志里。"""
    business_tools.fail_next_cancel = True
    metrics = [
        {
            "metric_id": "cancel-succeeded-once",
            "kind": "response-policy",
            "log": ACTION_LOG_EVIDENCE_ID,
            "require": [{"action": "orders.cancel", "target": "order-1",
                         "status": "succeeded", "min_count": 1, "max_count": 1}],
        },
        {
            "metric_id": "transient-failure-observed",
            "kind": "response-policy",
            "log": ACTION_LOG_EVIDENCE_ID,
            "require": [{"action": "orders.cancel", "status": "failed", "min_count": 1,
                         "max_count": 1}],
        },
        order_cancelled_metric(),
        {
            "metric_id": "goal",
            "kind": "goal-achieved",
            "components": [
                {"role": "final_state", "metric": order_cancelled_metric()},
                {"role": "process", "metric": {
                    "metric_id": "goal-process", "kind": "response-policy",
                    "log": ACTION_LOG_EVIDENCE_ID,
                    "require": [{"action": "orders.cancel", "target": "order-1",
                                 "status": "succeeded", "min_count": 1, "max_count": 1}],
                }},
            ],
        },
    ]
    workflow = workflow_body(steps=[
        {"step_id": "request", "kind": "send_message", "message": "请取消订单 order-1"},
        {"step_id": "confirm", "kind": "send_message", "message": "我确认取消"},
        {"step_id": "final-check", "kind": "checkpoint", "assertions": [
            {"op": "eq", "path": "state.order.status", "value": "cancelled"},
        ]},
    ])
    run_id, view, envelope = run_public_case(
        scenario_app, scripted_targets, business_tools, behavior="retry-after-tool-error",
        workflow=workflow, metrics=metrics,
    )
    assert envelope["scenario"]["status"] == "completed"
    scores = scores_by_metric(view)
    for metric_id, score in scores.items():
        assert score["metric_status"] == "scored", (metric_id, score.get("reason"))
        assert score["passed"] is True, (metric_id, score.get("reason"))
    # 两次调用都真实发生过：一次失败、一次成功。
    cancels = [call for call in business_tools.real_calls if call[0] == "orders.cancel"]
    assert len(cancels) == 2
    statuses = [item["status"] for item in envelope["frozen_observation"]["tool_calls"]]
    assert statuses == ["succeeded", "failed", "succeeded"]


def test_business_step_deadline_is_enforced_end_to_end(scenario_app, scripted_targets, business_tools):
    """4. 超时：越过步骤期限的那一步失败，Run 不可能报告成功。"""
    workflow = workflow_body(steps=[
        {"step_id": "request", "kind": "send_message", "message": "请取消订单 order-1",
         "timeout_sec": 0.02},
        {"step_id": "final-check", "kind": "checkpoint", "assertions": [
            {"op": "eq", "path": "state.order.status", "value": "cancelled"},
        ]},
    ])
    run_id, view, envelope = run_public_case(
        scenario_app, scripted_targets, business_tools, behavior="slow", workflow=workflow,
        metrics=[order_cancelled_metric()],
    )
    assert envelope["scenario"]["status"] == "failed"
    assert "exceeded its deadline" in (envelope["scenario"]["reason"] or "")
    frozen = envelope["frozen_observation"]
    assert frozen["termination"]["reason"] == "per_call_timeout"
    # 期限之后不再有新的 turn：目标只被调用一次。
    assert sum(len(session.sent) for session in scripted_targets.sessions) == 1
    scores = scores_by_metric(view)
    assert scores["order-cancelled"]["metric_status"] == "insufficient_evidence"
    assert scores["order-cancelled"]["passed"] is None
    assert view["scoring_pass"]["summary"]["passed"] == 0


def test_business_over_permission_is_refused_without_side_effects(scenario_app, scripted_targets, business_tools):
    """5. 越权：平台 ∩ fixture 权限拒绝副作用，尝试本身留证。"""
    metrics = [
        no_refund_metric(),
        {
            "metric_id": "refund-attempt-refused",
            "kind": "response-policy",
            "log": ACTION_LOG_EVIDENCE_ID,
            "require": [{"action": "orders.refund", "target": "order-1",
                         "status": "denied", "min_count": 1, "max_count": 1}],
        },
        {
            "metric_id": "refund-never-succeeded",
            "kind": "response-policy",
            "log": ACTION_LOG_EVIDENCE_ID,
            "forbid": [{"action": "orders.refund", "status": "succeeded"}],
        },
    ]
    workflow = workflow_body(steps=[
        {"step_id": "request", "kind": "send_message", "message": "帮我退款 order-1"},
        {"step_id": "final-check", "kind": "checkpoint", "assertions": [
            {"op": "eq", "path": "state.order.refunded", "value": False},
        ]},
    ])
    run_id, view, envelope = run_public_case(
        scenario_app, scripted_targets, business_tools, behavior="overreach", workflow=workflow,
        metrics=metrics,
    )
    assert envelope["scenario"]["status"] == "completed"
    assert scripted_targets.sessions[-1].denied == ["orders.refund"]
    assert [call[0] for call in business_tools.real_calls] == ["orders.get"] or business_tools.real_calls == []
    assert all(call[0] != "orders.refund" for call in business_tools.real_calls)
    refused = [entry for entry in envelope["tool_log"] if entry["tool"] == "orders.refund"]
    assert refused and refused[0]["status"] == "denied"
    scores = scores_by_metric(view)
    for metric_id, score in scores.items():
        assert score["metric_status"] == "scored", (metric_id, score.get("reason"))
        assert score["passed"] is True, (metric_id, score.get("reason"))
    assert envelope["observation"]["final_state"]["order"]["refunded"] is False


def test_business_duplicate_side_effect_is_detected(scenario_app, scripted_targets, business_tools):
    """6. 重复副作用：终态仍是 cancelled，重复调用被过程指标发现。"""
    business_tools.always_increment = True
    workflow = workflow_body(failure_policy="continue_for_evidence")
    metrics = [
        cancel_once_metric(),
        order_cancelled_metric(),
        {
            "metric_id": "cancel-count-exactly-one",
            "kind": "state-equals",
            "checkpoint": "final-check",
            "path": "order.cancellation_count",
            "expected": 1,
        },
        {
            "metric_id": "goal",
            "kind": "goal-achieved",
            "components": [
                {"role": "final_state", "metric": order_cancelled_metric()},
                {"role": "process", "metric": cancel_once_metric()},
            ],
        },
    ]
    run_id, view, envelope = run_public_case(
        scenario_app, scripted_targets, business_tools, behavior="duplicate-cancel",
        workflow=workflow, metrics=metrics,
    )
    assert envelope["observation"]["final_state"]["order"]["status"] == "cancelled"
    assert envelope["observation"]["final_state"]["order"]["cancellation_count"] == 2
    scores = scores_by_metric(view)
    assert scores["order-cancelled"]["passed"] is True  # 终态本身"看起来"正确
    assert scores["cancel-once"]["passed"] is False
    assert scores["cancel-once"]["reason"] == "action_count_exceeded"
    assert scores["cancel-count-exactly-one"]["passed"] is False
    assert scores["goal"]["passed"] is False


def test_business_process_crash_cannot_produce_a_scored_pass(scenario_app, scripted_targets, business_tools):
    """7. 进程崩溃：缺证据只能是 insufficient，不产生 pass。"""
    run_id, view, envelope = run_public_case(
        scenario_app, scripted_targets, business_tools, behavior="crash",
        metrics=[order_cancelled_metric(), default_metrics()[-1]],
    )
    assert envelope["scenario"]["status"] == "failed"
    assert "target process crashed" in (envelope["scenario"]["reason"] or "")
    assert envelope["frozen_observation"]["termination"]["reason"] == "error"
    scores = scores_by_metric(view)
    # 崩溃前没有产生任何最终 checkpoint：终态断言只能是 insufficient。
    assert scores["order-cancelled"]["metric_status"] == "insufficient_evidence"
    # goal 的过程分量是"已确认失败"（受监控范围内一次动作都没有），
    # 因此 goal 是 scored/False，而不是 pass——缺证据永远不会变成通过。
    assert scores["goal"]["passed"] is False
    assert scores["goal"]["reason"] == "goal_not_achieved"
    assert view["scoring_pass"]["summary"]["passed"] == 0


def test_no_side_effect_without_monitoring_coverage_is_only_insufficient(
    scenario_app, scripted_targets, business_tools,
):
    """停止未确认 -> 动作日志不完整 -> no-side-effect 只能 insufficient。"""
    workflow = workflow_body(steps=[
        {"step_id": "request", "kind": "send_message", "message": "请取消订单 order-1",
         "timeout_sec": 0.02},
        {"step_id": "final-check", "kind": "checkpoint", "assertions": [
            {"op": "eq", "path": "state.order.status", "value": "cancelled"},
        ]},
    ])
    metrics = [no_refund_metric(), order_cancelled_metric()]
    run_id, view, envelope = run_public_case(
        scenario_app, scripted_targets, business_tools, behavior="slow-unconfirmed",
        workflow=workflow, metrics=metrics,
    )
    assert envelope["scenario"]["status"] == "needs_review"
    assert envelope["scenario"]["needs_review"] is True
    # 停止未确认：现场保留（interrupted 清理），动作日志标记为不完整。
    assert envelope["cleanup"] and envelope["cleanup"][0]["interrupted"] is True
    frozen = envelope["frozen_observation"]
    assert frozen["coverage"]["complete"] is False
    log_ref = next(
        ref for ref in frozen["workflow_evidence"]
        if ref["evidence_id"] == ACTION_LOG_EVIDENCE_ID
    )
    assert log_ref["complete"] is False
    scores = scores_by_metric(view)
    assert scores["no-refund"]["metric_status"] == "insufficient_evidence"
    assert scores["no-refund"]["reason"] == "action_log_incomplete"
    assert scores["no-refund"]["passed"] is None
    assert view["scoring_pass"]["summary"]["passed"] == 0


def test_unavailable_backend_is_still_refused_at_creation(scenario_app, scripted_targets, business_tools):
    """能力不满足时创建期具名拒绝；显式关闭执行同样拒绝，不静默改选后端。"""
    from motte_sdk import scenario_backend

    publish_order_scenario(scenario_app.client, scenario_app.resources)
    original = scenario_backend.SCENARIO_BACKEND_AVAILABLE
    try:
        scenario_backend.install_scenario_backend(available=False)
        refused = create_order_run(scenario_app.client)
    finally:
        scenario_backend.install_scenario_backend(available=original)
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "EXECUTION_BACKEND_UNAVAILABLE"
    assert scenario_app.store.runs.list() == []

    # 目标能力不满足：同样是创建期拒绝，且不注册任何 Run。
    strict_id = "order-cancel-strict"
    published = scenario_app.client.post("/api/v1/workflows", json=workflow_body(
        workflow_id=strict_id,
        target_requirements={
            "multi_turn": True, "min_turns": 2, "required_tools": ["orders.archive"],
            "tool_modes": ["real"], "interrupt": True, "evidence": ["events"],
        },
    ))
    assert published.status_code == 201, published.text
    unsupported = create_order_run(scenario_app.client, workflow=strict_id)
    assert unsupported.status_code == 422
    assert unsupported.json()["error"]["code"] == "SCENARIO_TARGET_TOOL_UNSUPPORTED"
    assert scenario_app.store.runs.list() == []


def test_fixture_snapshot_is_frozen_and_foreign_evaluator_snapshot_is_rejected(
    scenario_app, scripted_targets, business_tools,
):
    """客户端不能伪造 workflow evaluator 快照；服务端冻结的配置有内容 hash。"""
    publish_order_scenario(scenario_app.client, scenario_app.resources)
    forged = scenario_app.client.post("/api/v1/runs", json={
        "scenario_version": f"{SCENARIO_NAME}@1",
        "manifest": {
            "workflow": f"{WORKFLOW_ID}@1", "agent": f"{TARGET_KIND}@1",
            "resource_snapshots": {"workflow_evaluator": {"evaluator_id": "forged"}},
        },
        "case_ids": ["case-1"],
    })
    assert forged.status_code == 422, forged.text
    assert forged.json()["error"]["code"] == "SNAPSHOT_RESERVED"
    assert scenario_app.store.runs.list() == []


def test_scenario_run_without_a_declared_evaluator_produces_no_invented_scores(
    scenario_app, scripted_targets, business_tools,
):
    """没有声明 evaluator 的 Scenario 不产生任何评分行（不发明 pass）。"""
    scenario_app.resources.publish_fixture(fixture_spec())
    published = scenario_app.client.post("/api/v1/workflows", json=workflow_body())
    assert published.status_code == 201, published.text
    scenario = scenario_app.client.post("/api/v1/scenarios", json={
        "name": SCENARIO_NAME, "version": "1",
    })
    assert scenario.status_code == 201, scenario.text
    created = create_order_run(scenario_app.client)
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]
    result = run_worker(scenario_app.app, run_id)
    assert result["status"] == "completed"
    view = scenario_app.client.get(f"/api/v1/runs/{run_id}").json()
    assert view["scores"] == []
    assert view["scoring_pass"]["summary"]["scores"] == 0
