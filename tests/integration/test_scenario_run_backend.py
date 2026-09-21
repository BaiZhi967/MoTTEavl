"""R6 / M5-T05：Scenario 公共 Run 纵向闭环（公共 API → 普通 Worker → 报告）。

覆盖 R6 要求（docs/superpowers/plans/2026-09-21-m5-completion-review.md）：

* 创建走公共 API，Run 落库为 queued，由**普通 WorkerLoop** 领取执行；
* 本文件不直接调用 ScenarioCaseExecutor，也不在测试内把 backend 标成 available；
* 一个 CaseAttempt 保存 steps / Invocations / Artifacts / FrozenObservation；
* workflow evaluator 只消费冻结、归属有效的证据；普通离线 rescore 复用同一份
  证据（业务工具与模型调用数都不增加）；
* 冻结的 mock / replay 模式在公共路径上各自消费自己的来源；
* 无 fixture 的纯消息流程：创建期与执行期对"是否需要 fixture"判断一致，
  Run 不会先被接受、再因为缺 primary binding 而失败。
"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from apps.worker.motte_worker.reporting import WorkerReporter
from apps.worker.motte_worker.runtime import WorkerLoop
from motte_contracts.fixture import FixtureSpec, fixture_content_hash
from motte_scenario.targets import (
    TargetAdapter,
    TargetCapabilities,
    register_target_adapter,
    unregister_target_adapter,
)
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore

PUBLISHED_AT = "2026-09-21T00:00:00Z"
TARGET_KIND = "r6-scripted-agent"
MESSAGE_TARGET_KIND = "r6-message-agent"
FIXTURE_ID = "order-state"
WORKFLOW_ID = "order-cancel-confirmed"
MESSAGE_WORKFLOW_ID = "greeting-only"
SCENARIO_NAME = "order-cancel"
MESSAGE_SCENARIO_NAME = "greeting"
EVALUATOR_ID = "workflow-assertions"
ACTION_LOG_EVIDENCE_ID = "action-log"

CANCEL_FIRST_TURN_STEPS = [
    {"step_id": "request", "kind": "send_message", "message": "请取消订单 order-1"},
    {"step_id": "before-confirm", "kind": "checkpoint", "assertions": [
        {"op": "eq", "path": "state.order.status", "value": "active"},
        {"op": "eq", "path": "state.order.cancellation_count", "value": 0},
    ]},
    {"step_id": "confirm", "kind": "send_message", "message": "我确认取消"},
    {"step_id": "final-check", "kind": "checkpoint", "label": "final", "assertions": [
        {"op": "eq", "path": "state.order.status", "value": "cancelled"},
        {"op": "eq", "path": "state.order.cancellation_count", "value": 1},
    ]},
]

DEFAULT_LIMITS = {"max_total_steps": 20, "max_turns": 4, "wall_time_sec": 30}

def confirm_before_cancel_metric() -> dict:
    """先确认后取消：动作日志里 orders.get 必须早于 orders.cancel。"""
    return {
        "metric_id": "confirm-before-cancel",
        "kind": "response-policy",
        "log": ACTION_LOG_EVIDENCE_ID,
        "ordered": True,
        "require": [
            {"action": "orders.get", "target": "order-1", "min_count": 1, "max_count": 1},
            {"action": "orders.cancel", "target": "order-1", "min_count": 1,
             "max_count": 1, "status": "succeeded"},
        ],
    }


def cancel_once_metric() -> dict:
    """取消动作必须恰好发生一次（重复副作用在这里暴露）。"""
    return {
        "metric_id": "cancel-once",
        "kind": "response-policy",
        "log": ACTION_LOG_EVIDENCE_ID,
        "require": [
            {"action": "orders.cancel", "target": "order-1", "min_count": 1,
             "max_count": 1, "status": "succeeded"},
        ],
    }


def order_cancelled_metric() -> dict:
    """终态断言：最后一个 checkpoint 里订单必须是 cancelled。"""
    return {
        "metric_id": "order-cancelled",
        "kind": "state-equals",
        "checkpoint": "final-check",
        "path": "order.status",
        "expected": "cancelled",
    }


def no_refund_metric() -> dict:
    """副作用断言：受监控范围内不得出现成功的退款动作。"""
    return {
        "metric_id": "no-refund",
        "kind": "no-side-effect",
        "scope": ["actions"],
        "log": ACTION_LOG_EVIDENCE_ID,
        "forbid": [{"action": "orders.refund", "status": "succeeded"}],
    }


#: 公共路径上的五个 workflow 指标种类（M5-T04 注册的 metric kinds）。
def default_metrics() -> list[dict]:
    return [
        confirm_before_cancel_metric(),
        cancel_once_metric(),
        order_cancelled_metric(),
        {
            "metric_id": "cancel-count",
            "kind": "state-delta",
            "before": "before-confirm",
            "after": "final-check",
            "path": "order.cancellation_count",
            "delta": 1,
        },
        no_refund_metric(),
        {
            "metric_id": "goal",
            "kind": "goal-achieved",
            "components": [
                {"role": "final_state", "metric": {
                    "metric_id": "goal-final-state", "kind": "state-equals",
                    "checkpoint": "final-check", "path": "order.status",
                    "expected": "cancelled",
                }},
                {"role": "process", "metric": {
                    "metric_id": "goal-process", "kind": "response-policy",
                    "log": ACTION_LOG_EVIDENCE_ID,
                    "require": [
                        {"action": "orders.get", "target": "order-1", "min_count": 1},
                        {"action": "orders.cancel", "target": "order-1", "min_count": 1,
                         "max_count": 1, "status": "succeeded"},
                    ],
                }},
            ],
        },
    ]


def fixture_spec(**overrides) -> dict:
    payload = {
        "fixture_id": FIXTURE_ID,
        "version": 1,
        "kind": "json",
        "initial_data": {"order": {
            "id": "order-1", "status": "active", "cancellation_count": 0, "refunded": False,
        }},
        "allowed_tools": ["orders.get", "orders.cancel"],
        "visible_fields": ["order"],
        "isolation": "per_case",
        "cleanup": "delete_owned",
        "published_at": PUBLISHED_AT,
        "lifecycle": "published",
    }
    payload.update(overrides)
    spec = FixtureSpec.model_validate(payload)
    record = spec.model_dump(mode="json")
    record["content_hash"] = fixture_content_hash(spec)
    return record


def workflow_body(**overrides) -> dict:
    body = {
        "workflow_id": WORKFLOW_ID,
        "version": "1",
        "description": "取消订单前必须取得用户确认",
        "published_at": PUBLISHED_AT,
        "fixture_refs": [{
            "fixture_id": FIXTURE_ID, "version": 1, "kind": "json",
            "content_hash": fixture_spec()["content_hash"],
        }],
        "target_requirements": {
            "multi_turn": True, "min_turns": 2, "required_tools": [],
            "tool_modes": ["real"], "interrupt": True,
            "evidence": ["events", "invocations", "artifacts"],
        },
        "steps": [dict(step) for step in CANCEL_FIRST_TURN_STEPS],
        "limits": dict(DEFAULT_LIMITS),
        "failure_policy": "stop_case",
    }
    body.update(overrides)
    return body


def message_workflow_body(**overrides) -> dict:
    body = {
        "workflow_id": MESSAGE_WORKFLOW_ID,
        "version": "1",
        "description": "无 fixture 的纯消息流程",
        "published_at": PUBLISHED_AT,
        "target_requirements": {
            "multi_turn": True, "min_turns": 1, "required_tools": [],
            "tool_modes": ["real"], "interrupt": False, "evidence": ["events"],
        },
        "steps": [
            {"step_id": "greet", "kind": "send_message", "message": "打个招呼"},
            {"step_id": "greeted", "kind": "assert", "assertions": [
                {"op": "eq", "path": "step_results.greet.status", "value": "sent"},
            ]},
        ],
        "limits": dict(DEFAULT_LIMITS),
    }
    body.update(overrides)
    return body


MESSAGE_METRICS = [{
    "metric_id": "greeting-policy",
    "kind": "response-policy",
    "source": "final_output",
    "require": [{"text": "你好", "min_count": 1}],
}]


# --------------------------------------------------------------- 业务目标与工具

class ScriptedBusinessTarget:
    """确定性业务目标：不调用任何模型，只通过受控工具桥发出业务动作。"""

    def __init__(self, tools, *, behavior: str = "confirm-then-cancel") -> None:
        self.tools = dict(tools)
        self.behavior = behavior
        self.sent: list[str] = []
        self.denied: list[str] = []

    def begin(self):
        return {"state": "active"}

    def _call(self, name, arguments):
        return self.tools[name](arguments)

    def send(self, message, *, deadline=None):
        """按 turn 顺序执行确定性业务剧本；不调用任何模型。"""
        self.sent.append(message)
        turn = len(self.sent)
        if self.behavior == "confirm-then-cancel":
            if turn == 1:
                self._call("orders.get", {"order_id": "order-1"})
                return {"output": "请确认是否取消 order-1", "termination_reason": "final_answer"}
            self._call("orders.cancel", {"order_id": "order-1"})
            return {"output": "已取消", "termination_reason": "final_answer"}
        if self.behavior == "cancel-without-confirmation":
            if turn == 1:
                self._call("orders.cancel", {"order_id": "order-1"})
                return {"output": "已取消", "termination_reason": "final_answer"}
            return {"output": "已经取消了", "termination_reason": "final_answer"}
        if self.behavior == "retry-after-tool-error":
            if turn == 1:
                self._call("orders.get", {"order_id": "order-1"})
                return {"output": "请确认是否取消 order-1", "termination_reason": "final_answer"}
            try:
                self._call("orders.cancel", {"order_id": "order-1"})
            except Exception:  # noqa: BLE001 - 业务工具暂态失败后重试
                self._call("orders.cancel", {"order_id": "order-1"})
            return {"output": "重试后已取消", "termination_reason": "final_answer"}
        if self.behavior == "duplicate-cancel":
            if turn == 1:
                self._call("orders.get", {"order_id": "order-1"})
                return {"output": "请确认是否取消 order-1", "termination_reason": "final_answer"}
            self._call("orders.cancel", {"order_id": "order-1"})
            self._call("orders.cancel", {"order_id": "order-1"})
            return {"output": "已取消两次", "termination_reason": "final_answer"}
        if self.behavior == "overreach":
            try:
                self._call("orders.refund", {"order_id": "order-1"})
            except Exception:  # noqa: BLE001 - 平台拒绝越权调用
                self.denied.append("orders.refund")
                return {"output": "退款被拒绝", "termination_reason": "final_answer"}
            return {"output": "退款成功", "termination_reason": "final_answer"}
        if self.behavior == "slow":
            # 越过步骤期限后正常返回：由引擎的"返回后复核"如实记为失败。
            import time

            time.sleep(0.08)
            return {"output": "迟到的回答", "termination_reason": "final_answer"}
        if self.behavior == "slow-unconfirmed":
            # 目标自己报告超时，并且**无法确认**已经停止（needs_review 路径）。
            import time

            if deadline is not None:
                time.sleep(max(0.0, deadline - time.monotonic()))
            return {"output": None, "termination_reason": "per_call_timeout", "timeout": True}
        if self.behavior == "crash":
            raise RuntimeError("target process crashed")
        raise AssertionError(f"unknown behavior: {self.behavior}")

    def observe(self):
        return {"turns": len(self.sent)}

    def interrupt(self, reason):
        """停止确认按事实报告：slow-unconfirmed 故意无法确认已停止。"""
        return {"reason": reason, "confirmed": self.behavior != "slow-unconfirmed"}

    def close(self):
        return {"state": "closed"}


class MessageOnlyTarget:
    """无 fixture 的纯消息目标；不接收任何工具。"""

    def __init__(self, tools) -> None:
        assert not tools, "a fixture-less workflow must not expose business tools"
        self.sent: list[str] = []

    def begin(self):
        return {"state": "ready"}

    def send(self, message, *, deadline=None):
        self.sent.append(message)
        return {"output": "你好，很高兴见到你", "termination_reason": "final_answer"}

    def observe(self):
        return {"turns": len(self.sent)}

    def interrupt(self, reason):
        return {"reason": reason, "confirmed": True}

    def close(self):
        return {"state": "closed"}


@pytest.fixture
def targets():
    created: list[ScriptedBusinessTarget] = []
    messages: list[MessageOnlyTarget] = []
    behavior = {"value": "confirm-then-cancel"}

    def open_session(context):
        session = ScriptedBusinessTarget(context.get("tools") or {}, behavior=behavior["value"])
        created.append(session)
        return session

    def open_message_session(context):
        session = MessageOnlyTarget(context.get("tools") or {})
        messages.append(session)
        return session

    register_target_adapter(TargetAdapter(
        kind=TARGET_KIND,
        capabilities=lambda _manifest: TargetCapabilities(
            kind=TARGET_KIND, multi_turn=True,
            tool_modes=("real", "mock", "replay", "deny"),
            tools=("orders.get", "orders.cancel", "orders.refund"),
            interrupt=True, evidence=("events", "invocations", "artifacts"),
        ),
        open_session=open_session,
    ), replace=True)
    register_target_adapter(TargetAdapter(
        kind=MESSAGE_TARGET_KIND,
        capabilities=lambda _manifest: TargetCapabilities(
            kind=MESSAGE_TARGET_KIND, multi_turn=True, tool_modes=("real",),
            tools=(), interrupt=False, evidence=("events",),
        ),
        open_session=open_message_session,
    ), replace=True)
    try:
        yield SimpleNamespace(
            sessions=created, message_sessions=messages,
            set_behavior=lambda value: behavior.update(value=value),
        )
    finally:
        unregister_target_adapter(TARGET_KIND)
        unregister_target_adapter(MESSAGE_TARGET_KIND)


class BusinessTools:
    """业务工具实现（real / mock / replay 三种来源分别登记）。"""

    def __init__(self) -> None:
        self.real_calls: list[tuple[str, dict]] = []
        self.mock_calls: list[tuple[str, dict]] = []
        self.fail_next_cancel = False
        #: 每次都计数（用于复现"重复副作用"）：默认按幂等业务语义只计一次。
        self.always_increment = False

    def handlers(self) -> dict:
        return {
            "orders.get": self.get,
            "orders.cancel": self.cancel,
            "orders.refund": self.refund,
        }

    def mock_handlers(self) -> dict:
        return {"orders.cancel": self.mock_cancel}

    def replay_records(self) -> dict:
        return {"orders.get": {
            "state": {"order": {"id": "order-1", "status": "active",
                                "cancellation_count": 0, "refunded": False}},
            "result": {"order": {"id": "order-1", "status": "active",
                                 "cancellation_count": 0}},
        }}

    # ------------------------------------------------------------- real
    def get(self, state, arguments):
        self.real_calls.append(("orders.get", dict(arguments)))
        order = dict(state["order"])
        return {"order": {"id": order["id"], "status": order["status"],
                          "cancellation_count": order["cancellation_count"]}}

    def cancel(self, state, arguments):
        self.real_calls.append(("orders.cancel", dict(arguments)))
        if self.fail_next_cancel:
            self.fail_next_cancel = False
            raise RuntimeError("transient business tool failure")
        order = dict(state["order"])
        if self.always_increment or order.get("status") != "cancelled":
            order["status"] = "cancelled"
            order["cancellation_count"] = int(order.get("cancellation_count", 0)) + 1
        return {**state, "order": order}, {
            "order": {"id": order["id"], "status": order["status"],
                      "cancellation_count": order["cancellation_count"]},
        }

    def refund(self, state, arguments):  # pragma: no cover - 越权调用必须到不了这里
        self.real_calls.append(("orders.refund", dict(arguments)))
        order = dict(state["order"])
        order["refunded"] = True
        return {**state, "order": order}, {"ok": True}

    # ------------------------------------------------------------- mock
    def mock_cancel(self, state, arguments):
        self.mock_calls.append(("orders.cancel", dict(arguments)))
        order = dict(state["order"])
        order["status"] = "mock-cancelled"
        order["cancellation_count"] = int(order.get("cancellation_count", 0)) + 1
        return {**state, "order": order}, {
            "order": {"id": order["id"], "status": order["status"],
                      "cancellation_count": order["cancellation_count"]},
        }


@pytest.fixture
def tools():
    from motte_sdk.scenario_backend import register_scenario_tools, unregister_scenario_tools

    implementation = BusinessTools()
    register_scenario_tools(
        FIXTURE_ID,
        handlers=implementation.handlers(),
        mock_handlers=implementation.mock_handlers(),
        replay_records=implementation.replay_records(),
    )
    try:
        yield implementation
    finally:
        unregister_scenario_tools(FIXTURE_ID)


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("MOTTE_SCENARIO_FIXTURE_ROOT", str(tmp_path / "fixtures"))
    store = InMemoryRunStore()
    resources = InMemoryResourceStore()
    application = create_app(store, resource_store=resources)
    client = TestClient(application)
    return SimpleNamespace(
        client=client, app=application, store=store, resources=resources,
        service=application.state.run_service, tmp_path=tmp_path,
    )


def publish_order_scenario(client, resources, *, metrics=None, workflow=None,
                           fixture: dict | None = None) -> None:
    resources.publish_fixture(fixture or fixture_spec())
    published = client.post("/api/v1/workflows", json=workflow or workflow_body())
    assert published.status_code == 201, published.text
    scenario = client.post("/api/v1/scenarios", json={
        "name": SCENARIO_NAME, "version": "1",
        "evaluator": {
            "evaluator_id": EVALUATOR_ID, "version": "1",
            "config": {"metrics": metrics if metrics is not None else default_metrics()},
        },
    })
    assert scenario.status_code == 201, scenario.text


def create_order_run(client, *, agent: str = TARGET_KIND, scenario: str = SCENARIO_NAME,
                     workflow: str = WORKFLOW_ID, case_ids=("case-1",)):
    return client.post("/api/v1/runs", json={
        "scenario_version": f"{scenario}@1",
        "manifest": {"workflow": f"{workflow}@1", "agent": f"{agent}@1"},
        "case_ids": list(case_ids),
    })


def run_worker(app, run_id: str | None = None) -> dict:
    """普通 Worker：与生产入口同一个 claim → dispatch 路径，不直接调执行器。"""
    worker = WorkerLoop(app.state.run_service, reporter=WorkerReporter(enabled=False))
    result = worker.claim_and_execute(run_id)
    assert result is not None, "the worker did not claim the queued run"
    return result


def scores_by_metric(run_view: dict) -> dict[str, dict]:
    return {score["metric_id"]: score for score in run_view["scores"] if score.get("metric_id")}


def case_row(environment, run_id: str, case_id: str = "case-1") -> dict:
    rows = [row for row in environment.store.case_runs.list_for_run(run_id)
            if row["case_id"] == case_id]
    assert rows, f"no case row for {case_id}"
    return rows[-1]


# --------------------------------------------------------------- 公共纵向闭环


def test_public_scenario_run_completes_through_an_ordinary_worker(environment, targets, tools):
    """公共创建 → queued → 普通 Worker 领取 → CaseAttempt/证据 → 报告读取。"""
    publish_order_scenario(environment.client, environment.resources)

    created = create_order_run(environment.client)
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]
    queued = environment.client.get(f"/api/v1/runs/{run_id}").json()
    assert queued["status"] == "queued"
    assert queued["manifest"]["execution"]["backend_id"] == "scenario"
    # 快照在创建期由服务端冻结：Workflow / Target / Fixture / 评分器身份。
    assert queued["manifest"]["workflow_snapshot"]["workflow_id"] == WORKFLOW_ID
    assert queued["manifest"]["fixture_snapshot"][f"{FIXTURE_ID}@1"]["content_hash"]
    frozen_evaluator = queued["manifest"]["resource_snapshots"]["workflow_evaluator"]
    assert frozen_evaluator["evaluator_id"] == EVALUATOR_ID
    assert frozen_evaluator["config_sha256"].startswith("sha256:")
    assert queued["manifest"]["evaluation"]["scorer_id"] == EVALUATOR_ID

    completed = run_worker(environment.app, run_id)
    assert completed["status"] == "completed", completed.get("error")

    view = environment.client.get(f"/api/v1/runs/{run_id}").json()
    assert view["status"] == "completed"
    scores = scores_by_metric(view)
    assert set(scores) == {
        "confirm-before-cancel", "cancel-once", "order-cancelled", "cancel-count",
        "no-refund", "goal",
    }
    for metric_id, score in scores.items():
        assert score["metric_status"] == "scored", (metric_id, score.get("reason"), score)
        assert score["passed"] is True, (metric_id, score.get("reason"), score)
    assert view["scoring_pass"]["scorer_id"] == EVALUATOR_ID
    assert view["scoring_pass"]["summary"]["multi_metric"] is True

    # 报告可读，读取不产生任何新的模型/工具调用。
    report = environment.client.get(f"/api/v1/runs/{run_id}/report").json()
    assert report["summary"]["cases"] == 1
    assert report["summary"]["passed"] == len(scores)
    assert len(environment.store.runs.list()) == 1  # 步骤不创建子 Run
    assert getattr(environment.store, "trials", None) is None or (
        environment.store.trials.list_for_run(run_id) == []
    )

    # 一个 CaseAttempt：步骤证据 + 冻结 Observation + checkpoint artifact。
    row = case_row(environment, run_id)
    envelope = row["result"]
    assert envelope["scenario"]["status"] == "completed"
    assert envelope["scenario"]["turns"] == 2
    assert [step["step_id"] for step in envelope["observation"]["steps"]] == [
        "request", "before-confirm", "confirm", "final-check",
    ]
    frozen = envelope["frozen_observation"]
    assert frozen["run_id"] == run_id and frozen["case_id"] == "case-1"
    assert frozen["termination"]["reason"] == "final_answer"
    assert frozen["coverage"]["complete"] is True
    assert {ref["evidence_id"] for ref in frozen["workflow_evidence"]} >= {
        "before-confirm", "final-check", ACTION_LOG_EVIDENCE_ID,
    }
    # 每个 checkpoint 证据都有 owner、artifact、双层 hash。
    from motte_storage.artifacts import ArtifactStore

    store = ArtifactStore(environment.tmp_path / "artifacts")
    for ref in frozen["workflow_evidence"]:
        assert ref["owner"] == {
            "run_id": run_id, "case_id": "case-1",
            "attempt_id": ref["owner"]["attempt_id"], "fixture_id": FIXTURE_ID,
        }
        assert ref["owner"]["attempt_id"]
        raw = store.read_bytes(ref["artifact_id"])
        assert hashlib.sha256(raw).hexdigest() == ref["artifact_sha256"]
        entry = next(item for item in frozen["artifact_refs"]
                     if item["artifact_id"] == ref["artifact_id"])
        assert entry["sha256"] == ref["artifact_sha256"]

    # 标准 subject 调用账本：模型之外的业务工具动作都落在 Invocation ledger 上。
    invocations = environment.store.invocations.list_for_run(run_id)
    assert [item["kind"] for item in invocations]
    assert all(item["status"] == "settled" for item in invocations)
    assert {item["tool_name"] for item in invocations if item["kind"] == "tool"} == {
        "orders.get", "orders.cancel",
    }


def test_offline_rescore_reuses_frozen_evidence_without_touching_tools_or_target(
    environment, targets, tools,
):
    """普通离线 rescore：复用冻结证据，零业务工具调用、零模型调用。"""
    publish_order_scenario(environment.client, environment.resources)
    created = create_order_run(environment.client)
    run_id = created.json()["id"]
    run_worker(environment.app, run_id)
    before = environment.client.get(f"/api/v1/runs/{run_id}").json()
    initial_pass = before["current_scoring_pass_id"]
    calls_before = list(tools.real_calls)
    sends_before = sum(len(session.sent) for session in targets.sessions)
    sessions_before = len(targets.sessions)

    rescored = environment.client.post(f"/api/v1/runs/{run_id}/rescore")
    assert rescored.status_code == 200, rescored.text
    after = rescored.json()
    assert after["current_scoring_pass_id"] != initial_pass
    after_scores = scores_by_metric(after)
    assert {metric_id: score["passed"] for metric_id, score in after_scores.items()} == {
        metric_id: score["passed"] for metric_id, score in scores_by_metric(before).items()
    }
    # 业务工具、目标会话、模型（这里没有 provider）都不再被触碰。
    assert tools.real_calls == calls_before
    assert sum(len(session.sent) for session in targets.sessions) == sends_before
    assert len(targets.sessions) == sessions_before
    # 固定的历史 pass 仍可读，且与 current 不同。
    passes = environment.client.get(f"/api/v1/runs/{run_id}/scoring-passes").json()
    assert passes["total"] == 2
    historical = environment.client.get(
        f"/api/v1/runs/{run_id}/report", params={"scoring_pass_id": initial_pass}
    ).json()
    assert historical["summary"]["passed"] == before["scoring_pass"]["summary"]["passed"]


def test_cancelled_run_is_never_claimed_and_touches_no_business_tool(environment, targets, tools):
    """取消：Run 不再被领取，不开 Target 会话、不调任何业务工具。"""
    publish_order_scenario(environment.client, environment.resources)
    created = create_order_run(environment.client)
    run_id = created.json()["id"]

    cancelled = environment.client.post(
        f"/api/v1/runs/{run_id}/cancel", json={"reason": "operator request"},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"

    worker = WorkerLoop(environment.app.state.run_service, reporter=WorkerReporter(enabled=False))
    assert worker.claim_and_execute() is None  # 普通 Worker 轮询：没有可领取的 Run
    assert environment.client.get(f"/api/v1/runs/{run_id}").json()["status"] == "cancelled"
    assert targets.sessions == []
    assert tools.real_calls == []


def test_foreign_observation_is_refused_by_the_scoring_consumer(environment, targets, tools):
    """归属被篡改的 Observation 一律拒评：只产生 insufficient，绝不发布 pass。"""
    from motte_sdk.scenario_backend import scenario_scores

    publish_order_scenario(environment.client, environment.resources)
    created = create_order_run(environment.client)
    run_id = created.json()["id"]
    run_worker(environment.app, run_id)
    run = environment.store.runs.get(run_id)
    row = case_row(environment, run_id)
    tampered = dict(row["result"]["frozen_observation"])
    tampered["run_id"] = "run-foreign"
    scores = scenario_scores(run, [{"case_id": "case-1", "result": {
        "frozen_observation": tampered,
    }}])
    assert scores
    assert all(score["metric_status"] == "insufficient_evidence" for score in scores)
    assert all(score["passed"] is None for score in scores)


def test_frozen_mock_and_replay_modes_have_their_own_public_sources(environment, targets, tools):
    """mock / replay 在公共路径上消费自己的来源；real handler 一次都不被调用。"""
    workflow = workflow_body(steps=[
        {"step_id": "mock-call", "kind": "invoke_fixture_tool", "tool": "orders.cancel",
         "arguments": {"order_id": "order-1"}, "tool_mode": "mock"},
        {"step_id": "after-mock", "kind": "checkpoint", "assertions": [
            {"op": "eq", "path": "state.order.status", "value": "mock-cancelled"},
        ]},
        {"step_id": "replay-call", "kind": "invoke_fixture_tool", "tool": "orders.get",
         "arguments": {"order_id": "order-1"}, "tool_mode": "replay"},
        {"step_id": "after-replay", "kind": "checkpoint", "assertions": [
            {"op": "eq", "path": "state.order.status", "value": "active"},
        ]},
    ])
    publish_order_scenario(
        environment.client, environment.resources, workflow=workflow,
        metrics=[{
            "metric_id": "replayed-state", "kind": "state-equals",
            "checkpoint": "after-replay", "path": "order.status", "expected": "active",
        }],
    )
    created = create_order_run(environment.client)
    run_id = created.json()["id"]
    completed = run_worker(environment.app, run_id)
    assert completed["status"] == "completed", completed.get("error")

    view = environment.client.get(f"/api/v1/runs/{run_id}").json()
    assert scores_by_metric(view)["replayed-state"]["passed"] is True
    assert tools.real_calls == []
    assert tools.mock_calls == [("orders.cancel", {"order_id": "order-1"})]
    modes = {entry["tool"]: entry["mode"]
             for entry in case_row(environment, run_id)["result"]["tool_log"]}
    assert modes == {"orders.cancel": "mock", "orders.get": "replay"}


def test_a_mode_without_its_source_is_refused_on_the_public_path(environment, targets, tools):
    """mock 没有登记实现时具名拒绝，绝不回退真实 handler。"""
    from motte_sdk.scenario_backend import unregister_scenario_tools

    workflow = workflow_body(steps=[
        {"step_id": "mock-call", "kind": "invoke_fixture_tool", "tool": "orders.cancel",
         "arguments": {"order_id": "order-1"}, "tool_mode": "mock"},
    ])
    publish_order_scenario(environment.client, environment.resources, workflow=workflow)
    unregister_scenario_tools(FIXTURE_ID)
    try:
        created = create_order_run(environment.client)
        run_id = created.json()["id"]
        result = run_worker(environment.app, run_id)
    finally:
        from motte_sdk.scenario_backend import register_scenario_tools

        register_scenario_tools(
            FIXTURE_ID, handlers=tools.handlers(), mock_handlers=tools.mock_handlers(),
            replay_records=tools.replay_records(),
        )
    # 业务动作具名拒绝：Run 走完流程并留下失败证据，但绝不回退真实 handler。
    assert result["status"] == "completed", result.get("error")
    envelope = case_row(environment, run_id)["result"]
    assert envelope["scenario"]["status"] == "failed"
    assert "SCENARIO_TOOL_MOCK_MISSING" in (envelope["scenario"]["reason"] or "")
    assert tools.real_calls == []


def test_fixture_less_message_workflow_agrees_between_creation_and_execution(
    environment, targets, tools,
):
    """纯消息流程：创建期接受，执行期不索要 fixture；需要 fixture 的步骤在创建期被拒。"""
    published = environment.client.post("/api/v1/workflows", json=message_workflow_body())
    assert published.status_code == 201, published.text
    scenario = environment.client.post("/api/v1/scenarios", json={
        "name": MESSAGE_SCENARIO_NAME, "version": "1",
        "evaluator": {"evaluator_id": EVALUATOR_ID, "version": "1",
                      "config": {"metrics": MESSAGE_METRICS}},
    })
    assert scenario.status_code == 201, scenario.text

    created = create_order_run(
        environment.client, agent=MESSAGE_TARGET_KIND,
        scenario=MESSAGE_SCENARIO_NAME, workflow=MESSAGE_WORKFLOW_ID,
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]
    completed = run_worker(environment.app, run_id)
    assert completed["status"] == "completed", completed.get("error")
    view = environment.client.get(f"/api/v1/runs/{run_id}").json()
    assert scores_by_metric(view)["greeting-policy"]["passed"] is True

    # 同一份 Run 不含 fixture 快照；目标没有拿到任何业务工具。
    assert "fixture_snapshot" not in view["manifest"]
    assert targets.message_sessions and targets.message_sessions[-1].sent == ["打个招呼"]

    # 反向：声明 fixture 工具步骤但没有任何 fixture_refs 的 Workflow 在创建期
    # 就必须具名拒绝（不能先接受、执行时才说缺 primary binding）。
    needs_fixture = workflow_body(
        fixture_refs=[],
        steps=[
            {"step_id": "call", "kind": "invoke_fixture_tool", "tool": "orders.cancel",
             "arguments": {"order_id": "order-1"}},
        ],
    )
    published = environment.client.post("/api/v1/workflows", json=needs_fixture)
    assert published.status_code == 201, published.text
    refused = create_order_run(
        environment.client, agent=TARGET_KIND,
        scenario=MESSAGE_SCENARIO_NAME, workflow=WORKFLOW_ID,
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "SCENARIO_FIXTURE_REQUIRED"
