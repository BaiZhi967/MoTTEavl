"""M5-T10：校准集、逐项分歧、实验性状态与人工修订（软件半部）。

边界（对应 M5-G18/G19、验收 A17）：

- CalibrationSample 的来源是显式字段：人工样本必须同时有 annotator、reviewer
  和复核时间；软件只能生成 candidate 样本（source=synthetic_candidate），
  **永远不能**把候选样本标成 human_reviewed。没有人工标签时，报告会给出
  not_run 原因，而不是伪造质量真值。
- CalibrationPolicy 阈值按 rubric 用途固定（motte_eval.rubrics.policy_for），
  运行时不放宽。
- 报告输出逐 criterion 混淆/分歧、missing/refusal/error 率、重复评分稳定性和
  pairwise 正反位置结果；每次重复/换序都是一条 CalibrationCall，带费用。
- 未校准或配置变化的 Judge 是 experimental，不能进入阻断门禁，也不继承上一个
  模型/rubric 的合格状态：门禁资格按**所选 pass 的 judge spec hash** 精确查找，
  新校准的 pass 不会让旧的 experimental pass 获得资格。
- 人工修订命名来源 pass、操作者、原因、证据与差异；稀疏改动生成完整新 ScoreSet，
  未变指标带来源复制。并发修订用显式 CAS，绝不覆盖较新的 current。纯函数不
  触发任何模型调用，人工修订不花 Judge 费用。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from motte_contracts.evaluation import EvidenceRef
from motte_contracts.identity import canonical_sha256
from motte_contracts.messages import Contract

from .judge import JudgeSpec
from .rubrics import (
    CALIBRATION_SAMPLE_KINDS,
    CalibrationPolicy,
    policy_for,
    validate_policy,
)

__all__ = [
    "CALIBRATION_SCHEMA_VERSION",
    "CalibrationCall",
    "CalibrationError",
    "CalibrationReport",
    "CalibrationSample",
    "CalibrationSet",
    "CriterionConfusion",
    "HumanReviewRequired",
    "JudgeQualification",
    "ManualRevisionConflict",
    "ManualRevisionRequest",
    "ManualScoreChange",
    "QualificationRegistry",
    "apply_manual_revision_to_store",
    "build_calibration_report",
    "build_calibration_set",
    "build_manual_revision",
    "candidate_sample",
    "manual_revision_pass",
    "SAMPLE_KINDS",
    "SAMPLE_SOURCES",
    "SAMPLE_STATUSES",
    "calibration_content_sha256",
    "judge_spec_identity",
    "pass_gate_eligibility",
    "qualification_record",
    "qualify_judge",
    "repeat_plan",
    "require_human_reviewed",
    "review_sample",
    "sample_content_sha256",
]

CALIBRATION_SCHEMA_VERSION = 1
SAMPLE_KINDS = CALIBRATION_SAMPLE_KINDS
SAMPLE_SOURCES = ("human", "synthetic_candidate")
SAMPLE_STATUSES = ("candidate", "human_reviewed", "rejected")
CALL_KINDS = ("single", "repeat", "order_forward", "order_reverse")


class CalibrationError(ValueError):
    """校准或人工修订的配置错误（绝不是 subject 失败）。"""


class HumanReviewRequired(CalibrationError):
    """缺少真正的人工复核标签：软件路径可以验证，但验收必须标 not_run。"""


class ManualRevisionConflict(CalibrationError):
    """并发人工修订：当前 pass 已不是调用方预期的那一个。"""


# ------------------------------------------------------------------ 样本与校准集

class CalibrationSample(Contract):
    """一条校准样本；human_reviewed 只可能由真人工复核产生。"""

    sample_id: str = Field(min_length=1)
    kind: Literal[
        "clear_pass", "clear_fail", "borderline", "missing_evidence", "injection",
    ]
    candidate_output: str = ""
    observation: dict[str, Any] | None = None
    source: Literal["human", "synthetic_candidate"]
    status: Literal["candidate", "human_reviewed", "rejected"] = "candidate"
    labelling_notes: str = ""
    annotator: str | None = None
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    #: 人类金标：criterion_id -> 预期是否通过。空表示未标注该项。
    expected_criteria: dict[str, bool] = Field(default_factory=dict)
    #: 人类对整体状态的预期（例如空证据样本应 insufficient_evidence）。
    expected_status: Literal[
        "scored", "insufficient_evidence", "evaluator_error",
    ] | None = None
    rubric_id: str = Field(min_length=1)
    rubric_version: str = Field(min_length=1)
    model: str = Field(min_length=1)
    judge_spec_sha256: str | None = None
    calibration_version: str | None = None
    created_at: str | None = None
    content_sha256: str = Field(min_length=71, max_length=71)

    @model_validator(mode="after")
    def review_honesty(self) -> "CalibrationSample":
        if self.status == "human_reviewed":
            missing = [
                name for name in ("annotator", "reviewed_by", "reviewed_at")
                if not getattr(self, name)
            ]
            if missing:
                raise ValueError(
                    "a human-reviewed sample must name annotator/reviewer/reviewed_at: "
                    + ", ".join(missing)
                )
            if self.source != "human":
                raise ValueError(
                    "synthetic candidates can never be marked human_reviewed"
                )
            if not self.expected_criteria and self.expected_status is None:
                raise ValueError(
                    "a human-reviewed sample needs expected_criteria or expected_status"
                )
        if self.content_sha256 != sample_content_sha256(self):
            raise ValueError("sample content_sha256 does not match its content")
        return self

    @property
    def is_human_reviewed(self) -> bool:
        return self.status == "human_reviewed"


def _sample_identity(sample: "CalibrationSample | dict[str, Any]") -> dict[str, Any]:
    payload = (
        sample.model_dump(mode="json")
        if isinstance(sample, CalibrationSample)
        else dict(sample)
    )
    payload.pop("content_sha256", None)
    return payload


def sample_content_sha256(sample: "CalibrationSample | dict[str, Any]") -> str:
    return canonical_sha256(_sample_identity(sample))


def _finalize_sample(payload: dict[str, Any]) -> CalibrationSample:
    """补齐默认值后再算内容摘要：hash 必须覆盖模型实际持有的完整内容。"""
    draft = CalibrationSample.model_construct(**{**payload, "content_sha256": None})
    finalized = dict(payload)
    finalized["content_sha256"] = canonical_sha256(_sample_identity(draft))
    return CalibrationSample.model_validate(finalized)


def candidate_sample(
    sample_id: str,
    kind: str,
    candidate_output: str,
    *,
    rubric_id: str,
    rubric_version: str,
    model: str,
    judge_spec_sha256: str | None = None,
    observation: dict[str, Any] | None = None,
    labelling_notes: str = "",
    expected_criteria: dict[str, bool] | None = None,
    expected_status: str | None = None,
    calibration_version: str | None = None,
    created_at: str | None = None,
) -> CalibrationSample:
    """软件生成的候选样本：source=synthetic_candidate，status 永远是 candidate。"""
    if kind not in CALIBRATION_SAMPLE_KINDS:
        raise CalibrationError(f"unknown calibration sample kind: {kind!r}")
    payload: dict[str, Any] = {
        "sample_id": sample_id,
        "kind": kind,
        "candidate_output": candidate_output,
        "observation": observation,
        "source": "synthetic_candidate",
        "status": "candidate",
        "labelling_notes": labelling_notes,
        "expected_criteria": dict(expected_criteria or {}),
        "expected_status": expected_status,
        "rubric_id": rubric_id,
        "rubric_version": rubric_version,
        "model": model,
        "judge_spec_sha256": judge_spec_sha256,
        "calibration_version": calibration_version,
        "created_at": created_at,
    }
    return _finalize_sample(payload)


def review_sample(
    sample: CalibrationSample,
    *,
    annotator: str,
    reviewer: str,
    expected_criteria: dict[str, bool],
    reviewed_at: str,
    labelling_notes: str | None = None,
    expected_status: str | None = None,
) -> CalibrationSample:
    """记录一次真实人工复核（这是唯一能产生 human_reviewed 的入口）。"""
    if not annotator or not reviewer or not reviewed_at:
        raise HumanReviewRequired(
            "human review requires a named annotator, reviewer and timestamp"
        )
    if not expected_criteria and expected_status is None:
        raise HumanReviewRequired("human review requires at least one labelled criterion")
    payload = sample.model_dump(mode="json")
    payload.update({
        "source": "human",
        "status": "human_reviewed",
        "annotator": annotator,
        "reviewed_by": reviewer,
        "reviewed_at": reviewed_at,
        "expected_criteria": dict(expected_criteria),
        "expected_status": (
            expected_status if expected_status is not None else sample.expected_status
        ),
        "labelling_notes": (
            labelling_notes if labelling_notes is not None else sample.labelling_notes
        ),
    })
    payload.pop("content_sha256", None)
    return _finalize_sample(payload)


class CalibrationSet(Contract):
    """一个 rubric/model/config 组合下的校准集（内容寻址、不可变）。"""

    schema_version: Literal[1] = CALIBRATION_SCHEMA_VERSION
    calibration_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    rubric_id: str = Field(min_length=1)
    rubric_version: str = Field(min_length=1)
    model: str = Field(min_length=1)
    judge_spec_sha256: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""
    samples: list[CalibrationSample] = Field(default_factory=list)
    created_at: str | None = None
    content_sha256: str = Field(min_length=71, max_length=71)

    @model_validator(mode="after")
    def one_configuration(self) -> "CalibrationSet":
        seen: set[str] = set()
        for sample in self.samples:
            if sample.sample_id in seen:
                raise ValueError(f"duplicate calibration sample id: {sample.sample_id}")
            seen.add(sample.sample_id)
            if (
                sample.rubric_id != self.rubric_id
                or sample.rubric_version != self.rubric_version
                or sample.model != self.model
            ):
                raise ValueError(
                    "calibration samples must share the set's rubric and model: "
                    + sample.sample_id
                )
        if self.content_sha256 != calibration_content_sha256(self):
            raise ValueError("calibration content_sha256 does not match its content")
        return self

    @property
    def reference(self) -> str:
        return f"{self.calibration_id}@{self.version}"

    def human_reviewed(self) -> list[CalibrationSample]:
        return [item for item in self.samples if item.is_human_reviewed]

    def candidates(self) -> list[CalibrationSample]:
        return [item for item in self.samples if item.status == "candidate"]

    def kind_counts(self) -> dict[str, int]:
        counts = {kind: 0 for kind in CALIBRATION_SAMPLE_KINDS}
        for item in self.human_reviewed():
            counts[item.kind] = counts.get(item.kind, 0) + 1
        return counts

    def covers(self, policy: CalibrationPolicy | None = None) -> tuple[bool, list[str]]:
        """按固定政策检查首批人工样本是否覆盖五类且数量达标。"""
        policy = validate_policy(policy or policy_for(self.rubric_id, self.rubric_version))
        reasons: list[str] = []
        if len(self.human_reviewed()) < policy.min_human_reviewed_samples:
            reasons.append(
                f"human-reviewed samples {len(self.human_reviewed())} < required "
                f"{policy.min_human_reviewed_samples}"
            )
        counts = self.kind_counts()
        for kind, minimum in (
            ("clear_pass", policy.min_clear_pass),
            ("clear_fail", policy.min_clear_fail),
            ("borderline", policy.min_borderline),
            ("missing_evidence", policy.min_missing_evidence),
            ("injection", policy.min_injection),
        ):
            if counts.get(kind, 0) < minimum:
                reasons.append(
                    f"human-reviewed {kind} samples {counts.get(kind, 0)} < required {minimum}"
                )
        return (not reasons, reasons)


def calibration_content_sha256(calibration: "CalibrationSet | dict[str, Any]") -> str:
    payload = (
        calibration.model_dump(mode="json")
        if isinstance(calibration, CalibrationSet)
        else dict(calibration)
    )
    payload.pop("content_sha256", None)
    return canonical_sha256(payload)


def build_calibration_set(
    calibration_id: str,
    version: str,
    *,
    rubric_id: str,
    rubric_version: str,
    model: str,
    samples: list[CalibrationSample],
    judge_spec_sha256: str | None = None,
    config: dict[str, Any] | None = None,
    notes: str = "",
    created_at: str | None = None,
) -> CalibrationSet:
    payload: dict[str, Any] = {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "calibration_id": calibration_id,
        "version": version,
        "rubric_id": rubric_id,
        "rubric_version": rubric_version,
        "model": model,
        "judge_spec_sha256": judge_spec_sha256,
        "config": dict(config or {}),
        "notes": notes,
        "samples": [item.model_dump(mode="json") for item in samples],
        "created_at": created_at,
    }
    payload["content_sha256"] = canonical_sha256(payload)
    return CalibrationSet.model_validate(payload)


def require_human_reviewed(
    calibration: CalibrationSet, policy: CalibrationPolicy | None = None,
) -> None:
    """真实校准验收的前置条件；不满足只报告，绝不伪造标签。"""
    covered, reasons = calibration.covers(policy)
    if not covered:
        raise HumanReviewRequired(
            "calibration is not human-reviewed at the required level: "
            + "; ".join(reasons)
        )


# ------------------------------------------------------------------ 报告

class CriterionConfusion(Contract):
    criterion_id: str = Field(min_length=1)
    compared: int = Field(default=0, ge=0)
    true_pass: int = Field(default=0, ge=0)
    false_pass: int = Field(default=0, ge=0)
    true_fail: int = Field(default=0, ge=0)
    false_fail: int = Field(default=0, ge=0)
    missing_evidence: int = Field(default=0, ge=0)
    refusals: int = Field(default=0, ge=0)
    errors: int = Field(default=0, ge=0)

    @property
    def disagreements(self) -> int:
        return self.false_pass + self.false_fail

    @property
    def agreement_rate(self) -> float | None:
        if self.compared == 0:
            return None
        return (self.compared - self.disagreements) / self.compared


class CalibrationCall(Contract):
    """一次 Judge 调用（含重复与换序）及其费用；每次都必须有账本引用。"""

    call_id: str = Field(min_length=1)
    sample_id: str = Field(min_length=1)
    kind: Literal["single", "repeat", "order_forward", "order_reverse"]
    judge_job_id: str = Field(min_length=1)
    invocation_id: str = Field(min_length=1)
    outcome: Literal["succeeded", "failed", "indeterminate"]
    status: Literal[
        "ok", "refused", "malformed", "missing_evidence", "forged_evidence",
        "missing_criterion",
    ] | None = None
    criteria: dict[str, bool] = Field(default_factory=dict)
    cost_usd: float | None = None
    price_table_version: str | None = None
    presentation_order: list[str] = Field(default_factory=list)
    winner_candidate_id: str | None = None


class CalibrationReport(Contract):
    schema_version: Literal[1] = CALIBRATION_SCHEMA_VERSION
    calibration_id: str = Field(min_length=1)
    calibration_version: str = Field(min_length=1)
    calibration_sha256: str = Field(min_length=71, max_length=71)
    rubric_id: str = Field(min_length=1)
    rubric_version: str = Field(min_length=1)
    model: str = Field(min_length=1)
    judge_spec_sha256: str | None = None
    policy: CalibrationPolicy
    sample_count: int = Field(ge=0)
    human_reviewed_count: int = Field(ge=0)
    candidate_only_count: int = Field(ge=0)
    kind_counts: dict[str, int] = Field(default_factory=dict)
    per_criterion: list[CriterionConfusion] = Field(default_factory=list)
    disagreement_rate: float | None = None
    error_rate: float | None = None
    refusal_rate: float | None = None
    missing_evidence_rate: float | None = None
    repeat_stability: dict[str, Any] = Field(default_factory=dict)
    position_swap: dict[str, Any] = Field(default_factory=dict)
    calls: list[CalibrationCall] = Field(default_factory=list)
    call_count: int = Field(default=0, ge=0)
    cost: dict[str, Any] = Field(default_factory=dict)
    qualified: bool = False
    experimental: bool = True
    gate_eligible: bool = False
    reasons: list[str] = Field(default_factory=list)
    not_run: list[str] = Field(default_factory=list)
    generated_at: str | None = None


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def build_calibration_report(
    calibration: CalibrationSet,
    *,
    observed: dict[str, Any],
    calls: list[CalibrationCall] | None = None,
    policy: CalibrationPolicy | None = None,
    generated_at: str | None = None,
) -> CalibrationReport:
    """纯计算报告：不调用任何模型，只消费已记录的结果与调用账本。"""
    resolved = validate_policy(
        policy or policy_for(calibration.rubric_id, calibration.rubric_version)
    )
    calls = list(calls or [])
    samples = calibration.samples
    human = [item for item in samples if item.is_human_reviewed]
    criteria_order: list[str] = []
    for item in samples:
        for criterion_id in item.expected_criteria:
            if criterion_id not in criteria_order:
                criteria_order.append(criterion_id)

    counters: dict[str, dict[str, int]] = {
        criterion_id: {
            "compared": 0, "true_pass": 0, "false_pass": 0, "true_fail": 0,
            "false_fail": 0, "missing_evidence": 0, "refusals": 0, "errors": 0,
        }
        for criterion_id in criteria_order
    }
    compared = disagreements = refusals = errors = missing_evidence = 0
    for item in human:
        outcome = observed.get(item.sample_id)
        if outcome is None:
            # 计数单位与逐 criterion 混淆一致：一个 criterion 一条判断。
            errors += len(item.expected_criteria)
            for criterion_id in item.expected_criteria:
                counters[criterion_id]["errors"] += 1
            continue
        status = (
            getattr(outcome, "status", None)
            or (outcome.get("status", "malformed") if isinstance(outcome, dict) else "malformed")
        )
        raw_judgements = getattr(outcome, "judgements", None)
        if raw_judgements is None and isinstance(outcome, dict):
            raw_judgements = []
        judgements = {
            judgement.criterion_id: judgement for judgement in (raw_judgements or [])
        }
        if status in {"refused", "malformed", "forged_evidence", "missing_criterion"}:
            refusal = status == "refused"
            refusals += len(item.expected_criteria) if refusal else 0
            errors += 0 if refusal else len(item.expected_criteria)
            for criterion_id in item.expected_criteria:
                if refusal:
                    counters[criterion_id]["refusals"] += 1
                else:
                    counters[criterion_id]["errors"] += 1
            continue
        for criterion_id, expected in item.expected_criteria.items():
            judgement = judgements.get(criterion_id)
            outcome_name = getattr(judgement, "outcome", None) if judgement else None
            if judgement is None or outcome_name != "scored":
                counters[criterion_id]["missing_evidence"] += 1
                missing_evidence += 1
                continue
            counters[criterion_id]["compared"] += 1
            compared += 1
            actual = bool(judgement.passed)
            if expected and actual:
                counters[criterion_id]["true_pass"] += 1
            elif expected and not actual:
                counters[criterion_id]["false_fail"] += 1
                disagreements += 1
            elif not expected and actual:
                counters[criterion_id]["false_pass"] += 1
                disagreements += 1
            else:
                counters[criterion_id]["true_fail"] += 1

    confusions = [
        CriterionConfusion(criterion_id=criterion_id, **counters[criterion_id])
        for criterion_id in criteria_order
    ]
    total_judged = compared + sum(
        bucket["missing_evidence"] + bucket["refusals"] + bucket["errors"]
        for bucket in counters.values()
    )
    stability = _repeat_stability(calls)
    swap = _position_swap(calls)
    known_costs = [call.cost_usd for call in calls if call.cost_usd is not None]
    cost = {
        "known": len(known_costs) == len(calls) and bool(calls),
        "total_usd": round(sum(known_costs), 8) if known_costs else None,
        "priced_calls": len(known_costs),
        "price_table_versions": sorted({
            call.price_table_version for call in calls if call.price_table_version
        }),
    }
    report = CalibrationReport(
        calibration_id=calibration.calibration_id,
        calibration_version=calibration.version,
        calibration_sha256=calibration.content_sha256,
        rubric_id=calibration.rubric_id,
        rubric_version=calibration.rubric_version,
        model=calibration.model,
        judge_spec_sha256=calibration.judge_spec_sha256,
        policy=resolved,
        sample_count=len(samples),
        human_reviewed_count=len(human),
        candidate_only_count=len(calibration.candidates()),
        kind_counts=calibration.kind_counts(),
        per_criterion=confusions,
        disagreement_rate=_rate(disagreements, compared),
        error_rate=_rate(errors, total_judged),
        refusal_rate=_rate(refusals, total_judged),
        missing_evidence_rate=_rate(missing_evidence, total_judged),
        repeat_stability=stability,
        position_swap=swap,
        calls=calls,
        call_count=len(calls),
        cost=cost,
        generated_at=generated_at,
    )
    return qualify_judge(report)


def _repeat_stability(calls: list[CalibrationCall]) -> dict[str, Any]:
    repeats: dict[str, list[CalibrationCall]] = {}
    for call in calls:
        if call.kind in {"repeat", "single"}:
            repeats.setdefault(call.sample_id, []).append(call)
    multi = {key: value for key, value in repeats.items() if len(value) >= 2}
    if not multi:
        return {"measured": False, "reason": "no repeated scoring was recorded",
                "samples": 0, "stable": 0, "rate": None}
    stable = 0
    unstable: list[str] = []
    for sample_id, items in multi.items():
        signatures = {
            (item.status, tuple(sorted(item.criteria.items()))) for item in items
        }
        if len(signatures) == 1:
            stable += 1
        else:
            unstable.append(sample_id)
    return {
        "measured": True, "samples": len(multi), "stable": stable,
        "rate": stable / len(multi), "unstable_samples": sorted(unstable),
    }


def _position_swap(calls: list[CalibrationCall]) -> dict[str, Any]:
    forward: dict[str, CalibrationCall] = {}
    reverse: dict[str, CalibrationCall] = {}
    for call in calls:
        if call.kind == "order_forward":
            forward[call.sample_id] = call
        elif call.kind == "order_reverse":
            reverse[call.sample_id] = call
    pairs = sorted(set(forward) & set(reverse))
    if not pairs:
        return {"measured": False, "reason": "no swapped presentation was recorded",
                "pairs": 0, "consistent": 0, "rate": None}
    consistent = [
        sample_id for sample_id in pairs
        if forward[sample_id].winner_candidate_id is not None
        and forward[sample_id].winner_candidate_id == reverse[sample_id].winner_candidate_id
    ]
    return {
        "measured": True, "pairs": len(pairs), "consistent": len(consistent),
        "rate": len(consistent) / len(pairs),
        "inconsistent_samples": sorted(set(pairs) - set(consistent)),
    }


def repeat_plan(
    sample_ids: list[str], *, repeats: int = 1, swap: bool = False,
) -> list[dict[str, Any]]:
    """重复/换序的计划：每次条目都必须产生一次真实 Judge 调用与费用。"""
    if type(repeats) is not int or repeats < 1:
        raise CalibrationError("repeats must be a positive integer")
    plan: list[dict[str, Any]] = []
    for sample_id in sample_ids:
        for index in range(repeats):
            plan.append({
                "call_id": f"cal-{sample_id}-r{index + 1}",
                "sample_id": sample_id,
                "kind": "single" if index == 0 else "repeat",
                "repeat": index + 1,
            })
        if swap:
            plan.append({
                "call_id": f"cal-{sample_id}-fwd",
                "sample_id": sample_id, "kind": "order_forward", "repeat": 0,
            })
            plan.append({
                "call_id": f"cal-{sample_id}-rev",
                "sample_id": sample_id, "kind": "order_reverse", "repeat": 0,
            })
    return plan


# ------------------------------------------------------------------ 资格

class JudgeQualification(Contract):
    """某个 judge 配置的校准资格；按 spec hash 精确匹配，不跨版本继承。"""

    qualification_id: str = Field(min_length=1)
    judge_spec_sha256: str = Field(min_length=71, max_length=71)
    rubric_id: str = Field(min_length=1)
    rubric_version: str = Field(min_length=1)
    model: str = Field(min_length=1)
    calibration_id: str = Field(min_length=1)
    calibration_version: str = Field(min_length=1)
    calibration_sha256: str = Field(min_length=71, max_length=71)
    qualified: bool
    experimental: bool
    gate_eligible: bool
    reasons: list[str] = Field(default_factory=list)
    evaluated_at: str | None = None


def qualify_judge(
    report: CalibrationReport, *, evaluated_at: str | None = None,
) -> CalibrationReport:
    """把报告收敛成 qualified/experimental/gate_eligible 与原因列表。"""
    reasons: list[str] = []
    not_run: list[str] = []
    policy = report.policy
    if report.human_reviewed_count < policy.min_human_reviewed_samples:
        not_run.append(
            "no human labels: the software path is verifiable, but real G18/T10 "
            f"acceptance is not_run (human-reviewed samples "
            f"{report.human_reviewed_count} < {policy.min_human_reviewed_samples})"
        )
        reasons.append("calibration is not human-reviewed at the required level")
    if report.judge_spec_sha256 is None:
        reasons.append("calibration does not pin a judge spec hash")
    if report.disagreement_rate is None:
        reasons.append("no human-labelled criteria could be compared")
    elif report.disagreement_rate > policy.max_disagreement_rate:
        reasons.append(
            f"disagreement rate {report.disagreement_rate:.3f} > "
            f"{policy.max_disagreement_rate}"
        )
    if report.error_rate is None:
        reasons.append("no human-reviewed sample produced a judge outcome")
    elif report.error_rate > policy.max_error_rate:
        reasons.append(f"error rate {report.error_rate:.3f} > {policy.max_error_rate}")
    if policy.require_repeat_stability and not report.repeat_stability.get("measured"):
        reasons.append("repeated scoring stability was not measured")
    if policy.require_position_swap_consistency and not report.position_swap.get("measured"):
        reasons.append("pairwise position-swap consistency was not measured")
    if report.position_swap.get("measured") and (
        (report.position_swap.get("rate") or 0.0) < 1.0
    ):
        reasons.append("pairwise position swap changed the mapped winner")
    qualified = not reasons
    return report.model_copy(update={
        "qualified": qualified,
        "experimental": not qualified,
        "gate_eligible": qualified,
        "reasons": reasons,
        "not_run": not_run,
        "generated_at": report.generated_at,
    })


def qualification_record(
    report: CalibrationReport, *, qualification_id: str | None = None,
    evaluated_at: str | None = None,
) -> JudgeQualification:
    if not report.judge_spec_sha256:
        raise CalibrationError("qualification requires a calibration that pins a judge spec")
    return JudgeQualification(
        qualification_id=qualification_id or f"qual-{report.calibration_sha256[7:19]}",
        judge_spec_sha256=report.judge_spec_sha256,
        rubric_id=report.rubric_id,
        rubric_version=report.rubric_version,
        model=report.model,
        calibration_id=report.calibration_id,
        calibration_version=report.calibration_version,
        calibration_sha256=report.calibration_sha256,
        qualified=report.qualified,
        experimental=report.experimental,
        gate_eligible=report.gate_eligible,
        reasons=list(report.reasons),
        evaluated_at=evaluated_at or report.generated_at,
    )


class QualificationRegistry:
    """按 judge spec hash + rubric + model 精确登记的资格；不继承、不按名匹配。"""

    def __init__(self, records: list[JudgeQualification] | None = None) -> None:
        self._records: dict[tuple[str, str, str, str, str], JudgeQualification] = {}
        self._history: list[JudgeQualification] = []
        for record in records or []:
            self.register(record)

    def register(self, record: JudgeQualification) -> JudgeQualification:
        key = (
            record.judge_spec_sha256, record.rubric_id, record.rubric_version,
            record.model, record.calibration_version,
        )
        self._records[key] = record
        self._history.append(record)
        return record

    def history(self) -> list[JudgeQualification]:
        return list(self._history)

    def lookup(
        self, *, judge_spec_sha256: str, rubric_id: str, rubric_version: str,
        model: str, calibration_version: str | None = None,
    ) -> JudgeQualification | None:
        """精确查找；calibration_version=None 表示"当前校准版本未知"，不允许命中。"""
        if calibration_version is None:
            return None
        return self._records.get((
            judge_spec_sha256, rubric_id, rubric_version, model, calibration_version,
        ))

    def eligible(
        self, *, judge_spec_sha256: str, rubric_id: str, rubric_version: str,
        model: str, calibration_version: str | None = None,
    ) -> dict[str, Any]:
        record = self.lookup(
            judge_spec_sha256=judge_spec_sha256, rubric_id=rubric_id,
            rubric_version=rubric_version, model=model,
            calibration_version=calibration_version,
        )
        if record is None:
            return {
                "gate_eligible": False, "experimental": True,
                "reason": "no calibration qualification for this judge configuration",
            }
        return {
            "gate_eligible": record.gate_eligible,
            "experimental": record.experimental,
            "reason": "; ".join(record.reasons) or "calibrated for this configuration",
            "qualification_id": record.qualification_id,
        }


def pass_gate_eligibility(
    pass_record: dict[str, Any], registry: QualificationRegistry,
) -> dict[str, Any]:
    """门禁资格只看**所选 pass** 的 judge 身份，不看当前 Judge 设置。"""
    if pass_record.get("source") != "judge" or not isinstance(pass_record.get("judge"), dict):
        return {
            "gate_eligible": False, "experimental": False,
            "reason": "pass is not judge-scored; calibration eligibility does not apply",
        }
    judge = pass_record["judge"]
    return registry.eligible(
        judge_spec_sha256=str(judge.get("spec_sha256") or ""),
        rubric_id=str(judge.get("rubric_id") or ""),
        rubric_version=str(judge.get("rubric_version") or ""),
        model=str(judge.get("model") or ""),
        calibration_version=(
            str(judge["calibration_version"]) if judge.get("calibration_version") else None
        ),
    )


def judge_spec_identity(spec: JudgeSpec) -> dict[str, Any]:
    return {
        "judge_spec_sha256": spec.spec_sha256,
        "rubric_id": spec.rubric_id,
        "rubric_version": spec.rubric_version,
        "model": spec.model,
        "calibration_version": spec.calibration_version,
    }


# ------------------------------------------------------------------ 人工修订

class ManualScoreChange(Contract):
    """一次稀疏人工改动：定位到 (case, trial, metric) 并给出新判据。"""

    case_id: str = Field(min_length=1)
    metric_id: str = Field(min_length=1)
    trial_id: str | None = None
    passed: bool | None = Field(default=None, strict=True)
    value: float | None = Field(default=None, allow_inf_nan=False)
    reason: str | None = None

    @model_validator(mode="after")
    def change_is_meaningful(self) -> "ManualScoreChange":
        if self.passed is None and self.value is None and self.reason is None:
            raise ValueError("a manual change must set passed, value or reason")
        return self

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.case_id, self.trial_id or "", self.metric_id)


class ManualRevisionRequest(Contract):
    run_id: str = Field(min_length=1)
    source_pass_id: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    changes: list[ManualScoreChange] = Field(default_factory=list)
    expected_current_pass_id: str | None = None

    @model_validator(mode="after")
    def unique_changes(self) -> "ManualRevisionRequest":
        keys = [change.key for change in self.changes]
        if len(keys) != len(set(keys)):
            raise ValueError("manual revision has duplicate change targets")
        return self


def build_manual_revision(
    source_pass: dict[str, Any],
    request: ManualRevisionRequest,
    *,
    revision_id: str,
    created_at: str,
    current_pass_id: str | None,
) -> dict[str, Any]:
    """构造完整的新 pass 记录（纯函数）：未变指标带来源复制，改动记录 diff。"""
    if source_pass.get("id") != request.source_pass_id:
        raise CalibrationError("source pass id does not match the loaded pass")
    if source_pass.get("run_id") != request.run_id:
        raise CalibrationError("source pass belongs to another run")
    if request.expected_current_pass_id != current_pass_id:
        raise ManualRevisionConflict(
            "current scoring pass changed: "
            f"expected {request.expected_current_pass_id!r}, found {current_pass_id!r}"
        )
    changes = {change.key: change for change in request.changes}
    scores: list[dict[str, Any]] = []
    diffs: list[dict[str, Any]] = []
    for row in source_pass.get("scores") or []:
        key = (
            str(row.get("case_id") or ""),
            str(row.get("trial_id") or ""),
            str(row.get("metric_id") or ""),
        )
        change = changes.pop(key, None)
        updated = dict(row)
        updated["details"] = dict(row.get("details") or {})
        updated["scoring_pass_id"] = revision_id
        updated["details"]["manual_revision"] = {
            "revision_id": revision_id,
            "source_pass_id": source_pass["id"],
            "actor": request.actor,
            "changed": change is not None,
            "copied": change is None,
            "reason": request.reason,
        }
        if change is not None:
            before = {
                key_name: row.get(key_name)
                for key_name in ("passed", "value", "metric_status", "reason")
            }
            if change.passed is not None:
                updated["passed"] = change.passed
                updated["metric_status"] = "scored"
                if updated.get("denominator") is None:
                    updated["denominator"] = True
            if change.value is not None:
                updated["value"] = change.value
                updated["metric_status"] = "scored"
                if updated.get("denominator") is None:
                    updated["denominator"] = True
            if change.reason is not None:
                updated["reason"] = change.reason
            diffs.append({
                "case_id": key[0], "trial_id": key[1], "metric_id": key[2],
                "before": before,
                "after": {
                    key_name: updated.get(key_name)
                    for key_name in ("passed", "value", "metric_status", "reason")
                },
            })
        scores.append(updated)
    if changes:
        raise CalibrationError(
            "manual revision targets unknown metrics: "
            + ", ".join("/".join(key) for key in sorted(changes))
        )
    summary = dict(source_pass.get("summary") or {})
    manual_summary = {
        "revision_id": revision_id,
        "source_pass_id": source_pass["id"],
        "actor": request.actor,
        "reason": request.reason,
        "evidence": [item.model_dump(mode="json") for item in request.evidence],
        "diffs": diffs,
        "changed_metrics": len(diffs),
        "copied_metrics": len(scores) - len(diffs),
    }
    summary.update({
        "scores": len(scores),
        "passed": sum(1 for row in scores if row.get("passed") is True),
        "manual_revision": manual_summary,
    })
    record = {
        "id": revision_id,
        "run_id": source_pass["run_id"],
        "scorer_id": "manual-revision",
        "scorer_version": f"{source_pass.get('scorer_version') or 'unknown'}+manual",
        "created_at": created_at,
        "source": "manual_revision",
        "source_run_revision": source_pass.get("source_run_revision"),
        "source_snapshot_hash": source_pass.get("source_snapshot_hash"),
        "previous_pass_id": source_pass["id"],
        "purpose": "manual_revision",
        "manual_revision": manual_summary,
        "summary": summary,
        "scores": scores,
    }
    return {"pass": record, "scores": scores, "diffs": diffs, "summary": manual_summary}


def manual_revision_pass(pass_record: dict[str, Any]) -> dict[str, Any] | None:
    """读取人工修订摘要；非人工修订 pass 返回 None（零模型调用）。"""
    if pass_record.get("source") != "manual_revision":
        return None
    value = pass_record.get("manual_revision")
    return dict(value) if isinstance(value, dict) else None


def apply_manual_revision_to_store(
    store: Any,
    request: ManualRevisionRequest,
    *,
    revision_id: str,
    created_at: str,
) -> dict[str, Any]:
    """把人工修订追加为新 pass：显式 CAS，绝不覆盖较新的 current。"""
    source = store.scoring_passes.get(request.source_pass_id)
    if source is None:
        raise CalibrationError(f"source scoring pass is missing: {request.source_pass_id}")
    current = store.scoring_passes.current(request.run_id)
    run = store.runs.get(request.run_id)
    if run is None:
        raise CalibrationError(f"run is missing: {request.run_id}")
    built = build_manual_revision(
        source, request, revision_id=revision_id, created_at=created_at,
        current_pass_id=(current or {}).get("id"),
    )
    stored = store.scoring_passes.append(
        built["pass"],
        built["scores"],
        expected_run_revision=run["revision"],
        expected_run_status=run["status"],
        event={
            "run_id": request.run_id,
            "type": "scoring_pass_created",
            "scoring_pass_id": revision_id,
            "source": "manual_revision",
            "actor": request.actor,
            "previous_pass_id": request.source_pass_id,
        },
        run_changes={"updated_at": created_at},
    )
    return {
        "pass": stored,
        "diffs": built["diffs"],
        "summary": built["summary"],
    }
