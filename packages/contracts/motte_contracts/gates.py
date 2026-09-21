"""Gate 契约（M6-T03/T08）：GateRule、GatePolicyVersion、GateResult。

六类决策、规则 kind registry、决策→退出码映射与优先级全部冻结在协议 §6/§7。
GateResult 的结论等价性：同 evaluation_input_hash + 同 result_semantics_hash
⇒ 同 decision 与 rule results（evaluated_at 只是审计字段，不进 hash）。
"""
from __future__ import annotations

from enum import Enum

from pydantic import Field, field_validator, model_validator

from .comparison import RunReportRef
from .hashing import canonical_hash
from .messages import Contract

GATE_ENGINE_VERSION = "gate-engine@1"
GATE_RULE_REGISTRY_VERSION = "gate-rules@1"

#: 规则 kind 集合（协议 §6 首批；新增 kind 必须同步协议文档并升 registry 版本）。
GATE_RULE_KINDS: frozenset[str] = frozenset({
    "metric_threshold",
    "baseline_delta",
    "critical_case",
    "coverage",
    "model_identity",
    "cost",
    "latency",
    "no_side_effect",
    "safety_marker",
})

#: 规则状态（RuleResult.status）：passed 布尔之外显式区分不足/不适用/诊断跳过。
RULE_STATUSES: frozenset[str] = frozenset({
    "pass", "fail", "insufficient", "not_applicable", "skipped_diagnostic",
})


class GateDecision(str, Enum):
    """六类顶层决策（协议 §6）。"""

    PASS = "pass"
    QUALITY_FAIL = "quality_fail"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    NOT_COMPARABLE = "not_comparable"
    EXECUTION_ERROR = "execution_error"
    SAFETY_BLOCK = "safety_block"


#: 决策 → CLI 退出码（协议 §7）。0/2/4 之外的 4xx 类由入口层直接产生。
DECISION_EXIT_CODES: dict[GateDecision, int] = {
    GateDecision.PASS: 0,
    GateDecision.QUALITY_FAIL: 1,
    GateDecision.EXECUTION_ERROR: 3,
    GateDecision.INSUFFICIENT_EVIDENCE: 5,
    GateDecision.NOT_COMPARABLE: 5,
    GateDecision.SAFETY_BLOCK: 6,
}

#: 多失败并存时的决策优先级（协议 §6）：安全 > 执行 > 证据/不可比 > 质量 > 通过。
DECISION_PRIORITY: tuple[GateDecision, ...] = (
    GateDecision.SAFETY_BLOCK,
    GateDecision.EXECUTION_ERROR,
    GateDecision.INSUFFICIENT_EVIDENCE,
    GateDecision.NOT_COMPARABLE,
    GateDecision.QUALITY_FAIL,
    GateDecision.PASS,
)


def decide(rule_decisions: list[GateDecision | None]) -> GateDecision:
    """按冻结优先级聚合规则级决策；空输入视为 pass。"""
    present = {value for value in rule_decisions if value is not None}
    for decision in DECISION_PRIORITY:
        if decision in present:
            return decision
    return GateDecision.PASS


#: 指标方向与规则 operator 的合法集合。
GATE_OPERATORS: frozenset[str] = frozenset({"gte", "lte", "gt", "lt"})


class GateRule(Contract):
    """一条 Gate 规则（协议 §6）。字段按 kind 取用；未知 kind 拒绝。"""

    rule_id: str = Field(min_length=1)
    kind: str
    metric_id: str | None = None
    metric_version: str | None = None
    operator: str | None = None
    threshold: float | None = Field(default=None, allow_inf_nan=False)
    #: baseline_delta：相对基线最大允许退化（绝对差，按 metric direction）。
    max_regression: float | None = Field(default=None, allow_inf_nan=False)
    critical_case_ids: tuple[str, ...] = ()
    min_samples: int | None = Field(default=None, ge=0, strict=True)
    min_coverage: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    #: comparable = 仅完全可比；partial = 允许部分可比但逐指标资格仍须满足。
    required_comparability: str | None = None
    #: fail_closed 默认；diagnostic_skip 仅诊断模式，且不能把整体变 pass。
    missing_policy: str = "fail_closed"
    severity: str = "block"
    evidence_requirements: tuple[str, ...] = ()
    #: 需要执行成功的规则在 Run failed/needs_review 时给 execution_error。
    requires_successful_run: bool = False
    #: 依赖实验性证据源（未校准 Judge、M4 runtime identity unknown）的规则：
    #: 正式模式下 fail-closed（不足证据），只有显式 diagnostic 才允许继续。
    experimental_evidence: bool = False
    #: model_identity 规则的期望实际模型身份（报告侧回报值，不是请求值）。
    expected: str | None = None

    @field_validator("kind")
    @classmethod
    def _kind_must_be_known(cls, value: str) -> str:
        if value not in GATE_RULE_KINDS:
            raise ValueError(
                f"unknown rule kind: {value!r} (known: {','.join(sorted(GATE_RULE_KINDS))})"
            )
        return value

    @field_validator("operator")
    @classmethod
    def _operator_must_be_known(cls, value: str | None) -> str | None:
        if value is not None and value not in GATE_OPERATORS:
            raise ValueError(
                f"unknown operator: {value!r} (known: {','.join(sorted(GATE_OPERATORS))})"
            )
        return value

    @model_validator(mode="after")
    def _values_in_domain(self) -> GateRule:
        checks = {
            "missing_policy": {"fail_closed", "diagnostic_skip"},
            "severity": {"block", "warn"},
        }
        for field_name, allowed in checks.items():
            value = getattr(self, field_name)
            if value not in allowed:
                raise ValueError(
                    f"unknown {field_name}: {value!r} (known: {','.join(sorted(allowed))})"
                )
        if self.required_comparability is not None and self.required_comparability not in {
            "comparable", "partial",
        }:
            raise ValueError(
                "unknown required_comparability: "
                f"{self.required_comparability!r} (known: comparable,partial)"
            )
        if self.kind in {"metric_threshold", "baseline_delta", "cost", "latency"}:
            if not self.metric_id:
                raise ValueError(f"rule kind {self.kind!r} requires metric_id")
        if self.kind == "metric_threshold" and (
            self.operator is None or self.threshold is None
        ):
            raise ValueError("metric_threshold requires operator and threshold")
        if self.kind == "baseline_delta" and self.max_regression is None:
            raise ValueError("baseline_delta requires max_regression")
        if self.kind == "critical_case" and not self.critical_case_ids:
            raise ValueError("critical_case requires critical_case_ids")
        if self.kind == "model_identity" and self.expected is None:
            raise ValueError("model_identity requires expected identity value")
        return self


class GatePolicyVersion(Contract):
    """版本化 Gate 政策（协议 §9）：published 后不可变。"""

    policy_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    lifecycle: str = "published"
    rules: tuple[GateRule, ...] = Field(min_length=1)
    #: 诊断政策：missing_policy=diagnostic_skip 生效，但整体永不输出 pass。
    diagnostic: bool = False
    created_by: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    created_at: str | None = None

    @field_validator("lifecycle")
    @classmethod
    def _lifecycle_must_be_known(cls, value: str) -> str:
        if value not in {"draft", "published", "deprecated"}:
            raise ValueError(
                f"unknown lifecycle: {value!r} (known: draft,published,deprecated)"
            )
        return value

    @model_validator(mode="after")
    def _rule_ids_unique(self) -> GatePolicyVersion:
        ids = [rule.rule_id for rule in self.rules]
        if len(ids) != len(set(ids)):
            duplicates = sorted({rule_id for rule_id in ids if ids.count(rule_id) > 1})
            raise ValueError("duplicate rule ids: " + ",".join(duplicates))
        # 协议 §6：diagnostic_skip 只允许出现在显式诊断政策里。
        if not self.diagnostic:
            offenders = [
                rule.rule_id for rule in self.rules
                if rule.missing_policy == "diagnostic_skip"
            ]
            if offenders:
                raise ValueError(
                    "diagnostic_skip requires a diagnostic policy: "
                    + ",".join(offenders)
                )
        return self

    def content_hash(self) -> str:
        payload = self.model_dump()
        return canonical_hash(payload)


class RuleResult(Contract):
    """单条规则的求值结果。status 区分 pass/fail/insufficient/not_applicable。"""

    rule_id: str = Field(min_length=1)
    kind: str
    status: str
    severity: str = "block"
    decision: GateDecision | None = None
    reason: str = Field(min_length=1)
    #: 诊断跳过的规则不能把整体变 pass（协议 §6）。
    diagnostic_skipped: bool = False

    @field_validator("status")
    @classmethod
    def _status_must_be_known(cls, value: str) -> str:
        if value not in RULE_STATUSES:
            raise ValueError(
                f"unknown rule status: {value!r} "
                f"(known: {','.join(sorted(RULE_STATUSES))})"
            )
        return value

    @property
    def passed(self) -> bool | None:
        """兼容视图：pass→True、fail→False、其余→None（不确定/不适用）。"""
        if self.status == "pass":
            return True
        if self.status == "fail":
            return False
        return None


class GateResult(Contract):
    """Gate 求值结果（不可变；重复求值产生等价结论，不改写原结果）。"""

    gate_result_id: str = Field(min_length=1)
    policy_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    policy_content_hash: str = Field(min_length=1)
    baseline: dict[str, str] | None = None
    candidates: tuple[RunReportRef, ...] = Field(min_length=1)
    decision: GateDecision
    rule_results: tuple[RuleResult, ...] = ()
    evaluation_input_hash: str = Field(min_length=1)
    result_semantics_hash: str = Field(min_length=1)
    conclusion_hash: str = Field(min_length=1)
    #: 审计字段：不进入任何 hash（协议 §8）。
    evaluated_at: str | None = None
    suggested_actions: tuple[str, ...] = ()

    def compute_gate_result_id(self) -> str:
        """gate_result_id = sha256(evaluation_input_hash + semantics_hash)。"""
        return canonical_hash({
            "evaluation_input_hash": self.evaluation_input_hash,
            "result_semantics_hash": self.result_semantics_hash,
        })

    def compute_conclusion_hash(self) -> str:
        """结论等价性 hash：decision + rule results（无 evaluated_at）。"""
        return canonical_hash({
            "decision": self.decision.value,
            "rule_results": [result.model_dump() for result in self.rule_results],
        })
