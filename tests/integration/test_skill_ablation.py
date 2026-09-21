"""M5-T08：no-skill / skill-v1 / skill-v2 三臂受控对照（公共冻结资源）。

三臂都是**普通 Run**：公共 API 创建 → 普通 Worker 领取 → 冻结证据 → 标准
评分 pass。没有任何第二套调度器，也没有测试专用的执行后门。

对照条件：除已发布 Skill 之外逐字段相同（Workflow/Fixture/Target/Agent/
Provider/预算政策/评分器/相同业务 ID 与相同会话输入）。两个 Case 都用业务 ID
`order-1`，每臂各自拥有独立 fixture 实例。

行为证据来自**真实 Agent 请求**：脚本化 Provider 只按收到的 system prompt
（即冻结注入声明的渲染结果）决定工具调用序列，因此三臂的差异只能来自 Skill
是否真的进入了请求。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from apps.worker.motte_worker.reporting import WorkerReporter
from apps.worker.motte_worker.runtime import WorkerLoop
from motte_contracts.fixture import FixtureSpec, fixture_content_hash
from motte_sdk.comparisons import ComparisonService
from motte_sdk.skill_ablation import (
    ARM_IDS,
    ARM_NO_SKILL,
    ARM_SKILL_V1,
    ARM_SKILL_V2,
    AblationPlanError,
    ArmSpec,
    bind_ablation_references,
    build_ablation_report,
    plan_skill_ablation,
    run_skill_ablation,
)
from motte_skill.content_store import FileContentStore
from motte_skill.versions import SkillVersion, publish_skill
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore

PUBLISHED_AT = "2026-09-21T00:00:00Z"
FIXTURE_ID = "order-state"
WORKFLOW_ID = "order-cancel-confirmed"
SCENARIO_NAME = "order-cancel"
EVALUATOR_ID = "workflow-assertions"
SKILL_ID = "cancel-guard"
BUSINESS_ID = "order-1"
CASE_KEYS = ("case-1", "case-2")
BUDGET = {"policy": "same-total-budget", "max_total_tokens": 4000}

#: 两版 Skill 的指令文本：Provider 只按它决定行为（真实请求里的文本）。
INSTRUCTION_V1 = "先向用户确认，再取消订单。"
INSTRUCTION_V2 = "立即读取订单状态并直接取消，不等待确认。"
MARKER_V1 = "先向用户确认"
MARKER_V2 = "立即读取订单状态"


def confirm_before_cancel_metric() -> dict:
    return {
        "metric_id": "confirm-before-cancel",
        "kind": "response-policy",
        "log": "action-log",
        "ordered": True,
        "require": [
            {"action": "orders.get", "target": BUSINESS_ID, "min_count": 1, "max_count": 1},
            {"action": "orders.cancel", "target": BUSINESS_ID, "min_count": 1,
             "max_count": 1, "status": "succeeded"},
        ],
    }


def cancel_once_metric() -> dict:
    return {
        "metric_id": "cancel-once",
        "kind": "response-policy",
        "log": "action-log",
        "require": [
            {"action": "orders.cancel", "target": BUSINESS_ID, "min_count": 1,
             "max_count": 1, "status": "succeeded"},
        ],
    }


def default_metrics() -> list[dict]:
    return [
        confirm_before_cancel_metric(),
        cancel_once_metric(),
        {
            "metric_id": "order-cancelled",
            "kind": "state-equals",
            "checkpoint": "final-check",
            "path": "order.status",
            "expected": "cancelled",
        },
        {
            "metric_id": "cancel-count",
            "kind": "state-delta",
            "before": "initial",
            "after": "final-check",
            "path": "order.cancellation_count",
            "delta": 1,
        },
        {
            "metric_id": "no-refund",
            "kind": "no-side-effect",
            "scope": ["actions"],
            "log": "action-log",
            "forbid": [{"action": "orders.refund", "status": "succeeded"}],
        },
    ]


def fixture_record() -> dict:
    spec = FixtureSpec.model_validate({
        "fixture_id": FIXTURE_ID,
        "version": 1,
        "kind": "json",
        "initial_data": {"order": {
            "id": BUSINESS_ID, "status": "active", "cancellation_count": 0,
            "refunded": False,
        }},
        "allowed_tools": ["orders.get", "orders.cancel"],
        "visible_fields": ["order"],
        "isolation": "per_case",
        "cleanup": "delete_owned",
        "published_at": PUBLISHED_AT,
        "lifecycle": "published",
    })
    record = spec.model_dump(mode="json")
    record["content_hash"] = fixture_content_hash(spec)
    return record


def workflow_body() -> dict:
    return {
        "workflow_id": WORKFLOW_ID,
        "version": "1",
        "description": "取消订单前必须取得用户确认",
        "published_at": PUBLISHED_AT,
        "fixture_refs": [{
            "fixture_id": FIXTURE_ID, "version": 1, "kind": "json",
            "content_hash": fixture_record()["content_hash"],
        }],
        "target_requirements": {
            "multi_turn": True, "min_turns": 2, "required_tools": [],
            "tool_modes": ["real"], "interrupt": True,
            "skill_injection": True,
            "evidence": ["events", "invocations", "artifacts"],
        },
        "steps": [
            # 任何臂的第一个 checkpoint：初态必须逐字段相同（真实冻结产物）。
            {"step_id": "initial", "kind": "checkpoint", "label": "initial",
             "assertions": [
                 {"op": "eq", "path": "state.order.status", "value": "active"},
                 {"op": "eq", "path": "state.order.cancellation_count", "value": 0},
             ]},
            {"step_id": "request", "kind": "send_message",
             "message": "请取消订单 order-1"},
            {"step_id": "before-confirm", "kind": "checkpoint", "label": "before-confirm",
             "assertions": [
                 {"op": "eq", "path": "state.order.status", "value": "active"},
                 {"op": "eq", "path": "state.order.cancellation_count", "value": 0},
             ]},
            {"step_id": "confirm", "kind": "send_message", "message": "我确认取消"},
            {"step_id": "final-check", "kind": "checkpoint", "label": "final",
             "assertions": [
                 {"op": "eq", "path": "state.order.status", "value": "cancelled"},
                 {"op": "eq", "path": "state.order.cancellation_count", "value": 1},
             ]},
        ],
        "limits": {"max_total_steps": 20, "max_turns": 4, "wall_time_sec": 60},
        "failure_policy": "stop_case",
    }


# --------------------------------------------------------- 真实 Agent 请求


def decision(tool: str, arguments: dict) -> dict:
    return {"content": json.dumps(
        {"action": "tool", "tool": tool, "input": arguments}, ensure_ascii=False,
    ), "finish_reason": "stop"}


def final(text: str) -> dict:
    return {"content": json.dumps(
        {"action": "final", "answer": text}, ensure_ascii=False,
    ), "finish_reason": "stop"}


class SkillDrivenProvider:
    """只按**收到的 system prompt** 决定行为；工具序列是真实执行的副作用证据。"""

    def __init__(self) -> None:
        self.requests: list = []

    def complete(self, request):  # noqa: ANN001 - Provider 协议
        self.requests.append(request)
        system = request.system or ""
        prompts = [
            index for index, message in enumerate(request.messages)
            if message.role == "user"
            and not str(message.content).startswith("observation: ")
        ]
        tail = request.messages[prompts[-1] + 1:] if prompts else list(request.messages)
        called: list[str] = []
        for message in tail:
            if message.role != "assistant":
                continue
            try:
                called.append(str(json.loads(str(message.content)).get("tool")))
            except ValueError:
                continue
        last_prompt = str(request.messages[prompts[-1]].content) if prompts else ""
        confirmed = "确认" in last_prompt and "取消订单" not in last_prompt
        if MARKER_V1 in system:
            if not confirmed:
                # 第一轮：只读状态并停下来问确认（Skill 指令要求的顺序）。
                if "orders.get" not in called:
                    return decision("orders.get", {"order_id": BUSINESS_ID})
                return final("请确认是否取消 order-1")
            if "orders.cancel" not in called:
                return decision("orders.cancel", {"order_id": BUSINESS_ID})
            return final("已确认并取消")
        if MARKER_V2 in system:
            if "orders.get" not in called:
                return decision("orders.get", {"order_id": BUSINESS_ID})
            if "orders.cancel" not in called:
                return decision("orders.cancel", {"order_id": BUSINESS_ID})
            return final("已直接取消")
        if "orders.cancel" not in called:
            return decision("orders.cancel", {"order_id": BUSINESS_ID})
        return final("已直接取消")


class BusinessTools:
    """真实业务工具实现（只有 real 模式会用到）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def handlers(self) -> dict:
        return {"orders.get": self.get, "orders.cancel": self.cancel,
                "orders.refund": self.refund}

    def get(self, state, arguments):
        self.calls.append(("orders.get", dict(arguments)))
        order = dict(state["order"])
        return {"order": {"id": order["id"], "status": order["status"],
                          "cancellation_count": order["cancellation_count"]}}

    def cancel(self, state, arguments):
        self.calls.append(("orders.cancel", dict(arguments)))
        order = dict(state["order"])
        if order.get("status") != "cancelled":
            order["status"] = "cancelled"
            order["cancellation_count"] = int(order.get("cancellation_count", 0)) + 1
        return {**state, "order": order}, {
            "order": {"id": order["id"], "status": order["status"],
                      "cancellation_count": order["cancellation_count"]},
        }

    def refund(self, state, arguments):  # pragma: no cover - 越权调用不该到达
        self.calls.append(("orders.refund", dict(arguments)))
        raise AssertionError("orders.refund is not allowed by the frozen fixture")


# ------------------------------------------------------------------ 环境


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("MOTTE_SCENARIO_FIXTURE_ROOT", str(tmp_path / "fixtures"))
    store = InMemoryRunStore()
    resources = InMemoryResourceStore(
        content_store=FileContentStore(tmp_path / "skill-content"),
    )
    app = create_app(store, resource_store=resources)
    client = TestClient(app)
    return SimpleNamespace(
        client=client, app=app, store=store, resources=resources, tmp_path=tmp_path,
        service=app.state.run_service,
    )


@pytest.fixture
def tools():
    from motte_sdk.scenario_backend import register_scenario_tools, unregister_scenario_tools

    implementation = BusinessTools()
    register_scenario_tools(FIXTURE_ID, handlers=implementation.handlers())
    try:
        yield implementation
    finally:
        unregister_scenario_tools(FIXTURE_ID)


def publish_skills(resources) -> dict[str, dict]:
    """把两版 Skill 发布进**公共资源仓库**，并按内容地址读回字节。"""
    published: dict[str, dict] = {}
    for version, instruction, marker in (
        ("1", INSTRUCTION_V1, MARKER_V1), ("2", INSTRUCTION_V2, MARKER_V2),
    ):
        body = SkillVersion.model_validate({
            "skill_id": SKILL_ID, "version": version, "kind": "instruction",
            "instruction": instruction, "injection_mode": "context-section",
            "requested_permissions": {"tools": ["orders.get", "orders.cancel"]},
            "published_at": PUBLISHED_AT,
        })
        record = publish_skill(
            resources.skills, body, resource_store=resources.content_store,
        )
        assert marker in record["instruction"]
        published[version] = record
    return published


def publish_scenario(environment) -> None:
    resources = environment.resources
    resources.publish_fixture(fixture_record())
    published = environment.client.post("/api/v1/workflows", json=workflow_body())
    assert published.status_code == 201, published.text
    scenario = environment.client.post("/api/v1/scenarios", json={
        "name": SCENARIO_NAME, "version": "1",
        "evaluator": {
            "evaluator_id": EVALUATOR_ID, "version": "1",
            "config": {"metrics": default_metrics()},
        },
    })
    assert scenario.status_code == 201, scenario.text


def base_manifest() -> dict:
    return {
        "workflow": f"{WORKFLOW_ID}@1",
        "agent": "builtin-agent@1",
        "agent_config": {"mode": "legacy-json"},
        "provider": {
            "kind": "openai_compatible", "model": "scripted-skill-model",
            "base_url": "http://127.0.0.1:9/v1",
        },
        "budget": dict(BUDGET),
        # 两个 Case 用**同一个业务 ID**：初态隔离必须由 fixture 所有权保证。
        "cases": {case_id: {"business_id": BUSINESS_ID} for case_id in CASE_KEYS},
    }


def arms(published) -> list[ArmSpec]:
    return [
        ArmSpec(arm_id=ARM_NO_SKILL),
        ArmSpec(
            arm_id=ARM_SKILL_V1, skills=(f"{SKILL_ID}@1",),
            skill_snapshot={"ref": f"{SKILL_ID}@1",
                            "content_hash": published["1"]["content_hash"]},
            budget_allocation={"instruction_overhead": max(1, len(INSTRUCTION_V1) // 4)},
        ),
        ArmSpec(
            arm_id=ARM_SKILL_V2, skills=(f"{SKILL_ID}@2",),
            skill_snapshot={"ref": f"{SKILL_ID}@2",
                            "content_hash": published["2"]["content_hash"]},
            budget_allocation={"instruction_overhead": max(1, len(INSTRUCTION_V2) // 4)},
        ),
    ]


def create_run(environment):
    def create(_arm_id: str, manifest: dict) -> dict:
        response = environment.client.post("/api/v1/runs", json={
            "scenario_version": f"{SCENARIO_NAME}@1",
            "manifest": manifest,
            "case_ids": list(CASE_KEYS),
        })
        assert response.status_code == 202, response.text
        return response.json()
    return create


def run_worker(environment, run_id: str) -> dict:
    worker = WorkerLoop(environment.service, reporter=WorkerReporter(enabled=False))
    result = worker.claim_and_execute(run_id)
    assert result is not None, "the worker did not claim the queued run"
    return result


def run_view(environment, run_id: str) -> dict:
    response = environment.client.get(f"/api/v1/runs/{run_id}")
    assert response.status_code == 200, response.text
    return response.json()


def evidence_of(environment, execution) -> dict[str, dict]:
    """逐臂证据：Run 视图 + 它自己的 Case 行（case_id → result）。"""
    evidence: dict[str, dict] = {}
    for arm in execution.arms:
        view = run_view(environment, arm.run_id)
        evidence[arm.arm_id] = {"run": view, "cases": list(view.get("cases") or [])}
    return evidence


def initial_checkpoint_hash(run: dict, case_id: str) -> str | None:
    case = next((row for row in run.get("cases") or [] if row["case_id"] == case_id), None)
    assert case is not None, "case row missing"
    frozen = (case.get("result") or {}).get("frozen_observation") or {}
    for item in frozen.get("workflow_evidence") or []:
        if item.get("evidence_id") == "initial":
            return item.get("artifact_sha256")
    return None


def case_status(report: dict, case_key: str, arm_id: str) -> dict:
    case = next(item for item in report["cases"] if item["case_key"] == case_key)
    return case["arms"][arm_id]


# --------------------------------------------------------------- 三臂对照


@pytest.fixture
def three_arms(environment, tools, monkeypatch, tmp_path):
    """公共资源 → 计划 → 三条普通 Run → 普通 Worker → 固定 pass 引用。"""
    publish_scenario(environment)
    published = publish_skills(environment.resources)
    provider = SkillDrivenProvider()
    monkeypatch.setattr(
        "motte_sdk.agent_backend.build_agent_provider",
        lambda manifest: SimpleNamespace(
            provider=SimpleNamespace(complete=provider.complete),
        ),
    )
    plan = plan_skill_ablation(
        base_manifest=base_manifest(),
        experiment_ref="exp-skill-ablation",
        arms=arms(published),
        budget_policy="same-total-budget",
        case_keys=list(CASE_KEYS),
    )
    created = run_skill_ablation(
        plan, base_manifest(), create_run=create_run(environment),
    )
    for arm in created.arms:
        run_worker(environment, arm.run_id)
    service = ComparisonService(environment.store)
    execution = bind_ablation_references(
        created,
        run_view=lambda run_id: run_view(environment, run_id),
        report_ref=lambda run_id, pass_id: service.report_ref(
            run_id, scoring_pass_id=pass_id,
        ),
    )
    return SimpleNamespace(
        plan=plan, created=created, execution=execution, provider=provider,
        environment=environment, service=service,
    )


def test_three_arms_are_ordinary_runs_with_frozen_skill_identity(three_arms):
    environment = three_arms.environment
    assert [arm.arm_id for arm in three_arms.plan.arms] == list(ARM_IDS)
    assert len({arm.run_id for arm in three_arms.execution.arms}) == 3
    for arm in three_arms.execution.arms:
        view = run_view(environment, arm.run_id)
        manifest = view["manifest"]
        assert view["status"] == "completed"
        assert manifest["execution"]["backend_id"] == "scenario"
        assert manifest["skill_arm"] == arm.arm_id
        assert arm.scoring_pass_id and arm.report_ref
        assert arm.report_ref["run_id"] == arm.run_id
        declaration = (manifest.get("resource_snapshots") or {}).get("skill_injection")
        if arm.arm_id == ARM_NO_SKILL:
            assert declaration is None
            assert manifest["skills"] == []
        else:
            assert manifest["skills"] == list(arm.skills)
            assert declaration["refs"] == list(arm.skills)
            entry = declaration["declaration"]["entries"][0]
            assert entry["skill_id"] == SKILL_ID
            assert entry["rendered"] in (INSTRUCTION_V1, INSTRUCTION_V2)
            assert entry["rendered_hash"]
            published_hash = environment.resources.skills.get(
                SKILL_ID, arm.skills[0].rpartition("@")[2],
            )["content_hash"]
            assert entry["content_hash"] == published_hash
    v1 = run_view(environment, three_arms.execution.arm(ARM_SKILL_V1).run_id)
    v2 = run_view(environment, three_arms.execution.arm(ARM_SKILL_V2).run_id)
    plan_v1 = v1["manifest"]["resource_snapshots"]["skill_injection"]["plan_hash"]
    plan_v2 = v2["manifest"]["resource_snapshots"]["skill_injection"]["plan_hash"]
    assert plan_v1 != plan_v2, "different skill versions must not share an injection identity"


def test_non_skill_conditions_are_field_identical_across_arms(three_arms):
    environment = three_arms.environment
    views = {
        arm.arm_id: run_view(environment, arm.run_id)["manifest"]
        for arm in three_arms.execution.arms
    }
    reference = views[ARM_NO_SKILL]
    for arm_id, manifest in views.items():
        for field in ("workflow_snapshot", "fixture_snapshot", "target_snapshot",
                      "agent", "agent_config", "provider", "cases"):
            assert manifest.get(field) == reference.get(field), f"{arm_id} changed {field}"
        assert manifest.get("cases"), "the frozen case inputs must be part of the comparison"
        # 预算：政策固定的总额度相同；只有政策允许的指令开销维度可分配。
        assert manifest["budget"]["policy"] == reference["budget"]["policy"]
        assert manifest["budget"]["max_total_tokens"] == reference["budget"]["max_total_tokens"]
    assert views[ARM_NO_SKILL]["budget"].get("instruction_overhead") is None
    assert views[ARM_SKILL_V1]["budget"]["instruction_overhead"] > 0
    assert views[ARM_SKILL_V2]["budget"]["instruction_overhead"] > 0


def test_every_arm_starts_from_the_same_initial_state_and_owns_its_evidence(three_arms):
    environment = three_arms.environment
    hashes = {}
    for arm in three_arms.execution.arms:
        view = run_view(environment, arm.run_id)
        assert view["case_ids"] == list(CASE_KEYS)
        for case_id in CASE_KEYS:
            case = next(row for row in view["cases"] if row["case_id"] == case_id)
            frozen = case["result"]["frozen_observation"]
            # 证据归属：每条臂的冻结观察只属于它自己的 Run。
            assert frozen["run_id"] == arm.run_id
            assert frozen["case_id"] == case_id
            for artifact in frozen["artifact_refs"]:
                assert arm.run_id in artifact["artifact_id"]
            hashes.setdefault(case_id, set()).add(initial_checkpoint_hash(view, case_id))
    for case_id, values in hashes.items():
        assert values == {next(iter(values))}, (
            f"case {case_id} did not start from an identical initial state"
        )
    # 同名业务 ID 的两个 Case 也各自独立（都从 active 开始并通过初态断言）。
    assert hashes.keys() == set(CASE_KEYS)


def test_skill_content_changes_real_agent_behaviour_per_arm(three_arms):
    environment = three_arms.environment
    report = build_ablation_report(
        three_arms.plan, three_arms.execution, evidence_of(environment, three_arms.execution),
    )
    assert report["coverage"]["complete"] is True
    for case_key in CASE_KEYS:
        assert case_status(report, case_key, ARM_SKILL_V1)["passed"] is True
        assert case_status(report, case_key, ARM_SKILL_V2)["passed"] is False
        assert case_status(report, case_key, ARM_NO_SKILL)["passed"] is False
    assert report["arms"][ARM_SKILL_V1]["tool_calls"] == 4  # 2 cases × (get + cancel)
    assert report["arms"][ARM_SKILL_V2]["tool_calls"] == 4
    assert report["arms"][ARM_NO_SKILL]["tool_calls"] == 2  # 2 cases × cancel
    assert report["gains"][f"{ARM_NO_SKILL}->{ARM_SKILL_V1}"]["passed_delta"] == 2
    assert report["gains"][f"{ARM_NO_SKILL}->{ARM_SKILL_V1}"]["complete"] is True
    # 三臂的 system prompt 都是真实请求的一部分，且逐臂不同。
    systems = {request.system for request in three_arms.provider.requests}
    assert len(systems) >= 3
    assert any(MARKER_V1 in (system or "") for system in systems)
    assert any(MARKER_V2 in (system or "") for system in systems)


def test_missing_usage_and_cost_stay_unknown_and_instruction_tokens_are_not_billed(
    three_arms,
):
    environment = three_arms.environment
    report = build_ablation_report(
        three_arms.plan, three_arms.execution, evidence_of(environment, three_arms.execution),
    )
    assert report["cost"]["known"] is False
    for arm_id, cost in report["cost"]["per_arm"].items():
        assert cost == {"known": False, "total_usd": None, "currency": None}, arm_id
    assert set(report["cost"]["delta_vs_baseline_usd"].values()) == {None}
    assert report["cost"]["instruction_tokens"][ARM_NO_SKILL] == 0
    assert report["cost"]["instruction_tokens"][ARM_SKILL_V1] > 0
    assert report["cost"]["instruction_tokens_billed"] is False
    assert report["arms"][ARM_SKILL_V1]["instruction_tokens"]["measured"] is False
    assert report["arms"][ARM_SKILL_V1]["instruction_tokens"]["billed"] is False


def test_a_comparable_experiment_allows_attribution_and_records_allowed_differences(
    three_arms,
):
    environment = three_arms.environment
    execution = three_arms.execution
    baseline = execution.arm(ARM_NO_SKILL)

    def comparison(arm_id: str) -> dict:
        arm = execution.arm(arm_id)
        result = three_arms.service.compare(
            baseline.run_id, arm.run_id, allowed_factors=("skill",),
            baseline_pass_id=baseline.scoring_pass_id,
            candidate_pass_id=arm.scoring_pass_id,
        )
        return {"eligible": result.eligible, "reasons": list(result.reasons),
                "allowed": list(result.allowed_differences)}

    report = build_ablation_report(
        three_arms.plan, execution, evidence_of(environment, execution),
        comparison=comparison,
    )
    assert report["attribution"]["blocking"] == []
    assert report["attribution"]["eligible"] is True, report["attribution"]["reasons"]
    assert any("skill" in item for item in report["attribution"]["allowed"])
    # 费用维度仍然不可归因：未知就是未知，不因为行为可比就冒充费用可比。
    assert report["attribution"]["cost_comparable"] is False
    assert report["attribution"]["cost_reasons"]


def test_a_differing_budget_or_agent_condition_blocks_pure_skill_attribution(
    three_arms,
):
    """R4 的阻断在**真实 Run** 上生效：改 Agent/预算就不能只归因给 Skill。"""
    environment = three_arms.environment
    execution = three_arms.execution
    baseline = execution.arm(ARM_NO_SKILL)
    reference = run_view(environment, baseline.run_id)

    variants = {
        "agent_config": {
            "agent_config": {**reference["manifest"]["agent_config"], "max_steps": 200},
        },
        "budget": {
            "budget": {**reference["manifest"]["budget"], "max_total_tokens": 100000},
        },
    }
    comparisons: dict[str, dict] = {}
    for label, overrides in variants.items():
        manifest = {
            "workflow": f"{WORKFLOW_ID}@1",
            "agent": "builtin-agent@1",
            "agent_config": {"mode": "legacy-json"},
            "provider": base_manifest()["provider"],
            "budget": dict(BUDGET),
            "skills": [f"{SKILL_ID}@2"],
            "skill_arm": ARM_SKILL_V2,
        }
        manifest.update(overrides)
        response = environment.client.post("/api/v1/runs", json={
            "scenario_version": f"{SCENARIO_NAME}@1",
            "manifest": manifest, "case_ids": list(CASE_KEYS),
        })
        assert response.status_code == 202, response.text
        variant_id = response.json()["id"]
        run_worker(environment, variant_id)
        view = run_view(environment, variant_id)
        blocked = three_arms.service.compare(
            baseline.run_id, variant_id, allowed_factors=("skill",),
            baseline_pass_id=baseline.scoring_pass_id,
            candidate_pass_id=view["current_scoring_pass_id"],
        )
        assert blocked.eligible is False, label
        assert blocked.reasons, label
        comparisons[label] = {"reasons": list(blocked.reasons), "allowed": []}

    # 报告消费同一比较结论：条件不同 → 归因被阻断，收益不再被声称。
    report = build_ablation_report(
        three_arms.plan, execution, evidence_of(environment, execution),
        comparison=lambda arm_id: {
            "reasons": comparisons["agent_config"]["reasons"],
            "allowed": [],
        },
    )
    assert report["attribution"]["eligible"] is False
    assert report["attribution"]["blocking"]
    assert any(
        "AGENT_CONFIG_CHANGED" in item for item in report["attribution"]["blocking"]
    )
    assert any(
        "BUDGET_ALLOWANCE_CHANGED" in item for item in comparisons["budget"]["reasons"]
    )


def test_an_arm_without_a_result_blocks_the_gain_claim_with_coverage(three_arms):
    """缺结果/取消的臂必须被解释成覆盖不足，而不是只挑成功 Case 声称收益。"""
    environment = three_arms.environment
    execution = three_arms.execution
    evidence = evidence_of(environment, execution)
    # 真实证据里去掉 skill-v2 的一个 Case 结果：报告必须显式报覆盖缺口。
    v2 = evidence[ARM_SKILL_V2]
    evidence[ARM_SKILL_V2] = {
        "run": v2["run"],
        "cases": [row for row in v2["cases"] if row["case_id"] != CASE_KEYS[1]],
    }
    report = build_ablation_report(three_arms.plan, execution, evidence)
    assert report["coverage"]["complete"] is False
    assert report["coverage"]["unpaired_cases"] == [CASE_KEYS[1]]
    assert report["attribution"]["eligible"] is False
    assert any(
        CASE_KEYS[1] in reason for reason in report["attribution"]["reasons"]
    )
    assert report["gains"][f"{ARM_NO_SKILL}->{ARM_SKILL_V2}"]["complete"] is False
    assert case_status(report, CASE_KEYS[1], ARM_SKILL_V2)["status"] == "no_result"


def test_a_cancelled_arm_is_reported_as_cancelled_not_as_a_pass(three_arms):
    environment = three_arms.environment
    execution = three_arms.execution
    evidence = evidence_of(environment, execution)
    v1 = evidence[ARM_SKILL_V1]
    evidence[ARM_SKILL_V1] = {
        "run": {**v1["run"], "status": "cancelled"}, "cases": [],
    }
    report = build_ablation_report(three_arms.plan, execution, evidence)
    assert case_status(report, CASE_KEYS[0], ARM_SKILL_V1)["status"] == "cancelled"
    assert report["arms"][ARM_SKILL_V1]["cancelled"] == len(CASE_KEYS)
    assert report["arms"][ARM_SKILL_V1]["terminal"] is False
    assert report["attribution"]["eligible"] is False


def test_arm_identity_mismatch_is_refused_before_any_evidence_is_accepted(three_arms):
    """创建回调返回别的臂：拒绝，不能把 A 臂的 Run 当成 B 臂的证据。"""
    environment = three_arms.environment
    plan = three_arms.plan

    def wrong_create(_arm_id: str, manifest: dict) -> dict:
        response = environment.client.post("/api/v1/runs", json={
            "scenario_version": f"{SCENARIO_NAME}@1",
            "manifest": {**manifest, "skill_arm": "someone-else"},
            "case_ids": list(CASE_KEYS),
        })
        assert response.status_code == 202, response.text
        return response.json()

    with pytest.raises(AblationPlanError) as error:
        run_skill_ablation(plan, base_manifest(), create_run=wrong_create)
    assert error.value.code == "ABLATION_ARM_IDENTITY_MISMATCH"


def test_published_skill_versions_are_readable_through_the_public_api(environment):
    publish_scenario(environment)
    published = publish_skills(environment.resources)
    listing = environment.client.get("/api/v1/skills/versions")
    assert listing.status_code == 200, listing.text
    refs = {item["skill_id"] + "@" + str(item["version"]) for item in listing.json()["items"]}
    assert {f"{SKILL_ID}@1", f"{SKILL_ID}@2"} <= refs
    single = environment.client.get(f"/api/v1/skills/{SKILL_ID}/versions/1")
    assert single.status_code == 200, single.text
    assert single.json()["content_hash"] == published["1"]["content_hash"]
