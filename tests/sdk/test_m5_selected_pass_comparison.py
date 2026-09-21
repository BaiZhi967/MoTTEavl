"""R4/F09：比较必须消费**所选 ScoringPass** 的真实评分身份。

反例（M5 review F09）：同一 Run 用不同 scorer / Judge profile / rubric /
calibration 真实保存两个 Judge pass，ComparisonService 以
allowed_factors=() 比较仍返回 eligible=true —— 比较读
manifest.scoring_provenance，而真实装配只说从所选 pass 投影
interventions，Judge 的身份写在 pass.scorer_* / pass.judge。

本文件的断言全部观察 ComparisonService 的真实输出
（eligible / reasons / 被选 pass 的投影），并由真实 ScoringJobService
发布 pass；不向底层比较函数喂手写 manifest（那正是审查批评的替代品）。
"""
from __future__ import annotations

import json
from typing import Any

from motte_eval.calibration import (
    ManualRevisionRequest,
    ManualScoreChange,
    apply_manual_revision_to_store,
)
from motte_eval.judge import JudgeAuthorisation, build_judge_spec
from motte_sdk.comparisons import ComparisonService
from motte_sdk.scoring_jobs import ScoringJobRequest, ScoringJobService
from motte_storage.run_store import InMemoryRunStore

RUN_ID = "run-m5-compare"
CASE_ID = "case-1"

#: 每个 rubric 的固定判据；Judge 输出必须逐 criterion 覆盖且引用真实证据。
_ANSWERS: dict[str, list[dict[str, Any]]] = {
    "answer-quality@1": [
        {"criterion_id": "task_completion", "passed": True, "reason": "done",
         "evidence": ["event:12"]},
        {"criterion_id": "constraint_adherence", "passed": True, "reason": "ok",
         "evidence": ["event:12"]},
        {"criterion_id": "evidence_grounding", "passed": False, "reason": "weak",
         "evidence": ["event:12"]},
    ],
    "safety-policy@1": [
        {"criterion_id": "no_instruction_override", "passed": True, "reason": "clean",
         "evidence": ["event:12"]},
        {"criterion_id": "no_tool_escalation", "passed": True, "reason": "none"},
        {"criterion_id": "output_shape_respected", "passed": True, "reason": "shape ok"},
    ],
}


class CountingJudgeProvider:
    """脚本化 Judge Provider：按冻结 rubric 返回固定判据并统计真实调用次数。"""

    kind = "scripted"

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def complete(self, request: Any) -> dict[str, Any]:
        answer = _ANSWERS[request.metadata["rubric"]]
        self.calls.append(request)
        return {
            "provider": "scripted",
            "model": request.model,
            "content": json.dumps({"criteria": answer}, ensure_ascii=False),
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
            "cost": {"total": 0.0025, "price_table_version": "price-1"},
            "response_id": f"resp-{len(self.calls)}",
            "metering": {"attempts": 1},
        }


def observation(*, case_id: str = CASE_ID, run_id: str = RUN_ID) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "observation_id": f"obs-{case_id}",
        "run_id": run_id,
        "case_id": case_id,
        "final_output": "alpha",
        "termination": {"reason": "final_answer"},
        "event_refs": [{"kind": "event", "run_id": run_id, "locator": "12"}],
        "artifact_refs": [],
        "coverage": {"complete": True},
        "tool_calls": [],
        "workspace": {"before": [], "after": []},
        "evidence_hash": "sha256:" + "b" * 64,
    }


def make_run(store: InMemoryRunStore, run_id: str = RUN_ID) -> dict[str, Any]:
    return store.runs.create({
        "id": run_id,
        "schema_version": 2,
        "revision": 1,
        "scenario_version": "replay@1",
        "status": "completed",
        "manifest": {
            "evaluation": {"scorer_id": "deterministic", "scorer_version": "1"},
        },
        "requested_manifest": {},
        "case_ids": [CASE_ID],
        "created_at": "2026-09-21T00:00:00+00:00",
        "updated_at": "2026-09-21T00:00:00+00:00",
    }, event={"run_id": run_id, "type": "queued", "status": "completed"})


def make_service(
    store: InMemoryRunStore, provider: CountingJudgeProvider,
) -> ScoringJobService:
    return ScoringJobService(store, provider_factory=lambda model: provider)


def judge_spec(
    *,
    judge_profile_id: str,
    model: str,
    rubric_id: str,
    rubric_version: str = "1",
    calibration_version: str | None = None,
) -> Any:
    return build_judge_spec(
        judge_profile_id=judge_profile_id,
        model=model,
        rubric_id=rubric_id,
        rubric_version=rubric_version,
        calibration_version=calibration_version,
        budget={"max_calls": 8},
    )


def authorisation() -> JudgeAuthorisation:
    return JudgeAuthorisation(
        authorised=True, actor="operator", max_calls=8, max_total_tokens=200_000,
    )


def publish_judge_pass(
    service: ScoringJobService, store: InMemoryRunStore, *, request_key: str, spec: Any,
) -> dict[str, Any]:
    """真实提交 + 领取 + 执行 + 发布，返回保存后的 pass 记录。"""
    submitted = service.submit(ScoringJobRequest(
        request_key=request_key,
        judge_spec=spec,
        mode="single",
        run_id=RUN_ID,
        observations={CASE_ID: observation()},
        authorisation=authorisation(),
    ))
    outcome = service.claim_and_run(submitted["job_id"])
    assert outcome["status"] == "completed", outcome
    stored = store.scoring_passes.get(submitted["reserved_pass_id"])
    assert stored is not None
    return stored


def two_judge_passes() -> tuple[InMemoryRunStore, CountingJudgeProvider, dict, dict]:
    store = InMemoryRunStore()
    make_run(store)
    provider = CountingJudgeProvider()
    service = make_service(store, provider)
    first = publish_judge_pass(
        service, store, request_key="req-quality",
        spec=judge_spec(
            judge_profile_id="judge-quality", model="judge-model-a",
            rubric_id="answer-quality", calibration_version="cal-1",
        ),
    )
    second = publish_judge_pass(
        service, store, request_key="req-safety",
        spec=judge_spec(
            judge_profile_id="judge-safety", model="judge-model-b",
            rubric_id="safety-policy", calibration_version="cal-2",
        ),
    )
    return store, provider, first, second


def test_two_real_saved_judge_passes_are_detected_as_different_scoring_instruments():
    store, provider, first, second = two_judge_passes()

    # 前提：两条 pass 真实保存在同一 Run 上，current 指向后一条。
    assert store.scoring_passes.get(first["id"])["judge"]["rubric"] == "answer-quality@1"
    assert store.scoring_passes.get(second["id"])["judge"]["rubric"] == "safety-policy@1"
    assert store.scoring_passes.current(RUN_ID)["id"] == second["id"]

    comparisons = ComparisonService(store)
    calls_before = len(provider.calls)

    result = comparisons.compare(
        RUN_ID, RUN_ID, allowed_factors=(),
        baseline_pass_id=first["id"], candidate_pass_id=second["id"],
    )
    assert result.eligible is False, result.reasons
    reasons = " ".join(result.reasons)
    for code in (
        "SCORER_CHANGED", "JUDGE_CHANGED", "RUBRIC_CHANGED", "CALIBRATION_CHANGED",
    ):
        assert code in reasons, (code, result.reasons)

    # 同一个 pass 与自己比较仍然可比：阻断来自真实身份差异，不是无条件拒绝。
    same = comparisons.compare(
        RUN_ID, RUN_ID, allowed_factors=(),
        baseline_pass_id=first["id"], candidate_pass_id=first["id"],
    )
    assert same.eligible is True, same.reasons

    # 政策显式允许的口径变化必须被记录，而不是静默通过。
    allowed = comparisons.compare(
        RUN_ID, RUN_ID, allowed_factors=("judge", "rubric", "calibration"),
        baseline_pass_id=first["id"], candidate_pass_id=second["id"],
    )
    assert allowed.eligible is True, allowed.reasons
    assert any("ALLOWED_FACTOR" in item for item in allowed.allowed_differences)

    # 比较全程零模型调用：Judge Provider 的调用次数不变。
    assert len(provider.calls) == calls_before


def test_a_fixed_pass_identity_does_not_follow_the_current_pointer():
    store, _provider, first, second = two_judge_passes()
    comparisons = ComparisonService(store)

    # current 已经是第二条 pass，固定第一条时投影仍是第一条的真实身份。
    assert store.scoring_passes.current(RUN_ID)["id"] == second["id"]
    view = comparisons._manifest_view(RUN_ID, first["id"])
    assert view["scoring_provenance"]["judge_profile_id"] == "judge-quality"
    assert view["scoring_provenance"]["rubric_id"] == "answer-quality"
    assert view["scoring_provenance"]["scorer_id"] == "judge:judge-quality"

    fixed = comparisons.compare(
        RUN_ID, RUN_ID, allowed_factors=(),
        baseline_pass_id=first["id"], candidate_pass_id=first["id"],
    )
    assert fixed.eligible is True, fixed.reasons

    # 不指定候选 pass 时才消费 current；此时身份变化必须被看见。
    follows_current = comparisons.compare(
        RUN_ID, RUN_ID, allowed_factors=(), baseline_pass_id=first["id"],
    )
    assert follows_current.eligible is False
    assert any("JUDGE_CHANGED" in reason for reason in follows_current.reasons)


def append_single_metric_pass(
    store: InMemoryRunStore, pass_id: str, *, scorer_id: str, scorer_version: str,
) -> dict[str, Any]:
    """保存一条单指标 pass：与所选 pass 的可比性判断只在评分身份上不同。"""
    return store.scoring_passes.append({
        "id": pass_id,
        "run_id": RUN_ID,
        "scorer_id": scorer_id,
        "scorer_version": scorer_version,
        "created_at": "2026-09-21T00:00:00+00:00",
        "source": "initial",
        "source_run_revision": 1,
        "summary": {},
    }, [{
        "case_id": CASE_ID,
        "metric_id": "accuracy",
        "evaluator_id": scorer_id,
        "evaluator_version": scorer_version,
        "metric_status": "scored",
        "value": 1.0,
        "passed": True,
        "denominator": True,
        "details": {},
    }])


def test_gate_comparability_consumes_the_fixed_pass_not_the_current_pointer():
    """Gate 的可比性判断必须与 candidate_summary/report_ref 用同一个固定 pass。"""
    store = InMemoryRunStore()
    make_run(store)
    first = append_single_metric_pass(
        store, "pass-a", scorer_id="deterministic", scorer_version="1",
    )
    second = append_single_metric_pass(
        store, "pass-b", scorer_id="other-scorer", scorer_version="2",
    )
    assert store.scoring_passes.current(RUN_ID)["id"] == second["id"]
    comparisons = ComparisonService(store)
    policy = {
        "metric": "accuracy", "op": "gte", "threshold": 0.0,
        "required_coverage": 0.0, "require_cost_known": False,
        "require_comparable": True,
    }

    # current 是第二条；把候选固定为第一条时必须识别出评分口径变化。
    gate = comparisons.evaluate_gate(
        RUN_ID, policy=policy, baseline_run_id=RUN_ID, scoring_pass_id=first["id"],
    )
    rules = {rule["id"]: rule for rule in gate["rules"]}
    assert rules["comparable"]["passed"] is False, gate
    assert "SCORER_CHANGED" in rules["comparable"]["reason"], rules["comparable"]

    # 固定候选 = current 那条时可比：阻断来自真实身份差异，不是无条件拒绝。
    same = comparisons.evaluate_gate(
        RUN_ID, policy=policy, baseline_run_id=RUN_ID, scoring_pass_id=second["id"],
    )
    assert {rule["id"]: rule for rule in same["rules"]}["comparable"]["passed"] is True, same


def test_a_historical_pass_without_a_judge_block_is_not_backfilled():
    store, _provider, judged, _second = two_judge_passes()
    # 历史行：真实保存的旧 pass 没有 judge 身份块，只有 scorer_id/version。
    store.scoring_passes.append({
        "id": "pass-legacy",
        "run_id": RUN_ID,
        "scorer_id": "deterministic",
        "scorer_version": "1",
        "created_at": "2026-09-20T00:00:00+00:00",
        "source": "initial",
        "source_run_revision": 1,
        "summary": {},
    }, [{
        "case_id": CASE_ID,
        "metric_id": "accuracy",
        "evaluator_id": "deterministic",
        "evaluator_version": "1",
        "metric_status": "scored",
        "value": 1.0,
        "passed": True,
        "denominator": True,
        "details": {},
    }])

    comparisons = ComparisonService(store)
    provenance = comparisons._manifest_view(RUN_ID, "pass-legacy")["scoring_provenance"]
    # 历史缺字段保持 unknown：不从 subject 的原 evaluator、也不从当前资源补齐。
    assert provenance["judge_profile_id"] is None
    assert provenance["rubric_id"] is None
    assert provenance["rubric_version"] is None
    assert provenance["calibration_version"] is None
    assert provenance["spec_sha256"] is None

    result = comparisons.compare(
        RUN_ID, RUN_ID, allowed_factors=(),
        baseline_pass_id="pass-legacy", candidate_pass_id=judged["id"],
    )
    assert result.eligible is False
    assert any("IDENTITY_MISSING" in reason for reason in result.reasons), result.reasons


def test_a_manual_revision_is_a_human_change_within_the_same_instrument():
    # 单一真实 Judge pass：它就是 current，可直接作为人工修订的来源。
    store = InMemoryRunStore()
    make_run(store)
    provider = CountingJudgeProvider()
    service = make_service(store, provider)
    judged = publish_judge_pass(
        service, store, request_key="req-manual-source",
        spec=judge_spec(
            judge_profile_id="judge-quality", model="judge-model-a",
            rubric_id="answer-quality", calibration_version="cal-1",
        ),
    )
    assert store.scoring_passes.current(RUN_ID)["id"] == judged["id"]
    applied = apply_manual_revision_to_store(
        store,
        ManualRevisionRequest(
            run_id=RUN_ID,
            source_pass_id=judged["id"],
            actor="operator",
            reason="human review found the answer addressed the constraint",
            changes=[ManualScoreChange(
                case_id=CASE_ID, metric_id="evidence_grounding", passed=True,
                reason="artifact shows the evidence",
            )],
            expected_current_pass_id=judged["id"],
        ),
        revision_id="pass-manual-1",
        created_at="2026-09-21T02:00:00+00:00",
    )
    revision = applied["pass"]
    assert revision["source"] == "manual_revision"
    comparisons = ComparisonService(store)

    result = comparisons.compare(
        RUN_ID, RUN_ID, allowed_factors=(),
        baseline_pass_id=judged["id"], candidate_pass_id=revision["id"],
    )
    assert result.eligible is False, result.reasons
    assert any("MANUAL_REVISION_CHANGED" in reason for reason in result.reasons)
    # 人工修订继承其来源 pass 的评分工具身份：不是新 rubric，也不是缺身份。
    assert not any("RUBRIC_CHANGED" in reason for reason in result.reasons), result.reasons
    assert not any("IDENTITY_MISSING" in reason for reason in result.reasons), result.reasons

    allowed = comparisons.compare(
        RUN_ID, RUN_ID, allowed_factors=("intervention",),
        baseline_pass_id=judged["id"], candidate_pass_id=revision["id"],
    )
    assert allowed.eligible is True, allowed.reasons
    assert any(
        "MANUAL_REVISION_CHANGED" in item for item in allowed.allowed_differences
    ), allowed.allowed_differences
