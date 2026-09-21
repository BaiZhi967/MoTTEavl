"""比较契约（M6）：固定 RunReportRef、ReportSnapshot、ComparisonPolicy、BaselineSnapshot。

RunReportRef 钉住 run/scoring pass/schema/evidence hash；ComparisonPolicy
声明允许变量（合法因子见 ``ALLOWED_COMPARISON_FACTORS``）。执行指纹与比较
签名分开：比较按任务源/内容/期望对齐，不按渲染后的 prompt。

M3（review R09）把冻结实验条件补进因子清单：重复数、Agent、超时、资源、
工具、重试、环境与凭据引用都是**可以**被政策显式允许变化的变量，但默认
不允许——不写进政策就表示"这些条件必须一致"，不能被当作模型差异比较。

M6-Full 在 Lite 之上冻结（协议 §3–§5）：三级比较结论、结构性与指标级原因
分开、ReportSnapshot 的内容 hash、BaselineSnapshot 的资格与分组 entries。
所有 policy/snapshot 契约 ``extra=forbid``，未知字段 fail closed。
"""
from __future__ import annotations

from enum import Enum

from pydantic import Field, field_validator, model_validator

from .hashing import canonical_hash
from .messages import Contract

ALLOWED_COMPARISON_FACTORS: frozenset[str] = frozenset({
    "model",
    # M3 冻结实验条件（与 motte_eval.comparison 的不变量字段同名）。
    "agent_id",
    "agent_version",
    "n_trials",
    "timeouts",
    "resources",
    "tools",
    "retries",
    "environment",
    "credentials",
    # M4：外部 runtime（backend/transport/原生配置）是实验条件——默认必须
    # 一致，政策显式允许时才可作为变量（M4 review R16）。
    "runtime",
    "intervention",
    # M5：Workflow / Fixture / Skill / Judge / rubric / 校准版本 / 预算政策
    # 都是实验条件。默认必须一致；只有政策显式允许时才可作为变量，且
    # Skill 归因还要求预算政策可比（见 motte_eval.comparison）。
    "workflow",
    "fixture",
    "skill",
    "judge",
    "rubric",
    "calibration",
    "budget_policy",
})

#: 比较政策自身允许的取值域（协议 §3/§9）。
_CASE_POLICIES = frozenset({"strict_content"})
_IDENTITY_POLICIES = frozenset({"fail_closed"})
_EVIDENCE_POLICIES = frozenset({"require_pass"})
_POLICY_VALUE_DOMAINS: frozenset[str] = (
    _CASE_POLICIES | _IDENTITY_POLICIES | _EVIDENCE_POLICIES
)


class ComparabilityLevel(str, Enum):
    """三级比较结论（协议 §3）。"""

    COMPARABLE = "comparable"
    PARTIALLY_COMPARABLE = "partially_comparable"
    NOT_COMPARABLE = "not_comparable"


class RunReportRef(Contract):
    """对固定 Run + ScoringPass 报告的不可变引用。"""

    run_id: str = Field(min_length=1)
    scoring_pass_id: str = Field(min_length=1)
    report_schema: str = Field(min_length=1)
    evidence_hash: str = Field(min_length=1)


class ComparisonPolicy(Contract):
    """比较政策：显式列出允许变化的因子与比较口径。

    Lite 兼容：只给 ``allowed_factors`` 时其余字段取缺省，行为与 M2-T09
    消费的 Lite 政策一致；新增字段全部可选，不存在"忽略全部差异"通配符。
    """

    allowed_factors: tuple[str, ...] = ()
    version: str = "1"
    #: 样本对齐口径：strict_content = 按 source/revision/内容 hash 对齐，
    #: 名字相同内容不同必须报 changed（协议 §3；无 name_only 放行模式）。
    case_policy: str = "strict_content"
    #: 缺失身份的处理：fail_closed = 一侧缺失身份即阻断，绝不默认相等。
    identity_policy: str = "fail_closed"
    #: 证据要求：require_pass = 比较输入必须固定 ScoringPass。
    evidence_policy: str = "require_pass"
    #: 统计政策引用（statistical_policy@N）；影响差异区间资格，不影响对齐。
    statistical_policy: str | None = None

    @field_validator("allowed_factors")
    @classmethod
    def _factors_must_be_known(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = sorted(set(value) - ALLOWED_COMPARISON_FACTORS)
        if unknown:
            raise ValueError(
                "unknown comparison factors: " + ",".join(unknown)
                + f" (allowed: {','.join(sorted(ALLOWED_COMPARISON_FACTORS))})"
            )
        return tuple(dict.fromkeys(value))

    @field_validator("case_policy", "identity_policy", "evidence_policy")
    @classmethod
    def _policy_value_must_be_known(cls, value: str) -> str:
        if value not in _POLICY_VALUE_DOMAINS:
            raise ValueError(
                f"unknown policy value: {value!r} (known: "
                + ",".join(sorted(_POLICY_VALUE_DOMAINS)) + ")"
            )
        return value

    def policy_hash(self) -> str:
        """政策内容的 canonical hash（BaselineSnapshot 固定的对象）。"""
        return canonical_hash(self.model_dump())


class EvidencePin(Contract):
    """报告依赖的关键工件 pin：内容寻址，工件删除后新资格降低、旧结论不变。"""

    artifact_id: str = Field(min_length=1)
    sha256: str = Field(min_length=1)


#: 成本口径（协议 §6.2）：subject（被测）/ judge / environment 分开累计。
COST_SCOPES: frozenset[str] = frozenset({"subject", "judge", "environment"})


class CostEntry(Contract):
    """单币种单口径的已知成本；跨币种不自动相加。"""

    scope: str
    currency: str = Field(min_length=1)
    amount: float = Field(ge=0, allow_inf_nan=False)
    price_table_versions: tuple[str, ...] = ()

    @field_validator("scope")
    @classmethod
    def _scope_must_be_known(cls, value: str) -> str:
        if value not in COST_SCOPES:
            raise ValueError(
                f"unknown cost scope: {value!r} (known: {','.join(sorted(COST_SCOPES))})"
            )
        return value


class CostSummary(Contract):
    """成本汇总：known/unknown、币种、价格表版本、来源、覆盖分开（G12）。

    ``null`` 不是 0：unknown_usage_count > 0 或 entries 为空时 known=False；
    已知成本只在同币种内求和（``totals``），异币种各自单列，绝不跨币种相加。
    """

    entries: tuple[CostEntry, ...] = ()
    unknown_usage_count: int = Field(default=0, ge=0, strict=True)
    source: str = "invocations"
    usage_coverage: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)

    @property
    def known(self) -> bool:
        return bool(self.entries) and self.unknown_usage_count == 0

    def totals(self) -> dict[str, float]:
        """按币种求和（同币种内可加；返回 dict，不混合币种）。"""
        totals: dict[str, float] = {}
        for entry in self.entries:
            totals[entry.currency] = totals.get(entry.currency, 0.0) + float(entry.amount)
        return totals

    def total_usd(self) -> float | None:
        """USD 单币种总额；无 USD 条目或多币种并存时返回 None（不换算）。"""
        totals = self.totals()
        if set(totals) != {"USD"}:
            return None
        return round(totals["USD"], 6)


#: Case disposition 真值表（协议 §2）。互斥终分类。
CASE_DISPOSITIONS: frozenset[str] = frozenset({
    "judged", "call_failed", "unknown", "not_attempted", "needs_review",
    "no_expectation",
})


class CaseDispositionRecord(Contract):
    """单个 Case 在固定报告里的互斥 disposition。"""

    case_id: str = Field(min_length=1)
    disposition: str
    detail: str | None = None

    @field_validator("disposition")
    @classmethod
    def _disposition_must_be_known(cls, value: str) -> str:
        if value not in CASE_DISPOSITIONS:
            raise ValueError(
                f"unknown disposition: {value!r} "
                f"(known: {','.join(sorted(CASE_DISPOSITIONS))})"
            )
        return value


#: ReportSnapshot 输出的计数键（协议 §2 不变量：attempted + not_attempted =
#: selected；judged + call_failed + unknown + needs_review <= attempted）。
DISPOSITION_COUNT_KEYS: tuple[str, ...] = (
    "selected", "attempted", "judged", "scored", "call_failed", "unknown",
    "not_attempted", "needs_review", "no_expectation",
)


class ReportSnapshot(Contract):
    """固定 RunReportRef 的冻结视图（协议 §5）。

    snapshot 内容（dispositions、metric values、coverage、cost、pins）全部
    来自已持久化事实；snapshot_id 是内容 canonical hash（排除自身字段）。
    不可变：工件删除/历史缺字段只降低**新**计算资格，不改变已存 snapshot。
    """

    snapshot_id: str = Field(min_length=1)
    ref: RunReportRef
    created_at: str = Field(min_length=1)
    suite: str | None = None
    denominator: str = Field(min_length=1)
    counts: dict[str, int]
    coverage: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    metric_values: dict[str, float | None] = Field(default_factory=dict)
    metric_registry_version: str = Field(min_length=1)
    cost: CostSummary = Field(default_factory=CostSummary)
    evidence_pins: tuple[EvidencePin, ...] = ()
    case_dispositions: tuple[CaseDispositionRecord, ...] = ()

    @field_validator("counts")
    @classmethod
    def _counts_must_cover_keys(cls, value: dict[str, int]) -> dict[str, int]:
        missing = [key for key in DISPOSITION_COUNT_KEYS if key not in value]
        if missing:
            raise ValueError("snapshot counts missing keys: " + ",".join(missing))
        return value

    @model_validator(mode="after")
    def _disposition_invariants(self) -> ReportSnapshot:
        attempted = self.counts.get("attempted", 0)
        selected = self.counts.get("selected", 0)
        not_attempted = self.counts.get("not_attempted", 0)
        if attempted + not_attempted != selected:
            raise ValueError(
                f"disposition invariant broken: attempted {attempted} + "
                f"not_attempted {not_attempted} != selected {selected}"
            )
        observable = (
            self.counts.get("judged", 0) + self.counts.get("call_failed", 0)
            + self.counts.get("unknown", 0) + self.counts.get("needs_review", 0)
        )
        if observable > attempted:
            raise ValueError(
                f"disposition invariant broken: observable dispositions {observable} "
                f"exceed attempted {attempted}"
            )
        return self

    def compute_snapshot_id(self) -> str:
        """内容 canonical hash（排除 snapshot_id 自身）。"""
        payload = self.model_dump()
        payload.pop("snapshot_id")
        return canonical_hash(payload)


#: Baseline 资格（协议 §4）。
BASELINE_ELIGIBILITY: frozenset[str] = frozenset({"formal", "diagnostic"})


class BaselineEntry(Contract):
    """Baseline 的一个固定报告引用；cell_key 为空表示单 Run baseline。"""

    cell_key: str | None = None
    ref: RunReportRef


class BaselineSnapshot(Contract):
    """不可变 Baseline 快照（协议 §4）：固定 entries + 政策 hash + 资格。"""

    baseline_id: str = Field(min_length=1)
    entries: tuple[BaselineEntry, ...] = Field(min_length=1)
    comparison_policy_hash: str = Field(min_length=1)
    eligibility: str = "formal"
    created_by: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    source_note: str | None = None
    created_at: str = Field(min_length=1)
    metrics: dict[str, float | int | None] = Field(default_factory=dict)

    @field_validator("eligibility")
    @classmethod
    def _eligibility_must_be_known(cls, value: str) -> str:
        if value not in BASELINE_ELIGIBILITY:
            raise ValueError(
                f"unknown baseline eligibility: {value!r} "
                f"(known: {','.join(sorted(BASELINE_ELIGIBILITY))}; "
                "ineligible references are rejected at creation)"
            )
        return value

    def content_hash(self) -> str:
        payload = self.model_dump()
        payload.pop("baseline_id")
        return canonical_hash(payload)

    def entry_for_cell(self, cell_key: str | None) -> BaselineEntry | None:
        for entry in self.entries:
            if entry.cell_key == cell_key:
                return entry
        return None


class DefaultBaselinePointer(Contract):
    """默认 baseline 指针（scope → snapshot）：CAS 更新 + 审计，不覆盖历史。"""

    scope: str = Field(min_length=1)
    baseline_id: str = Field(min_length=1)
    updated_by: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    comparison_policy_hash: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)
    position: int = Field(default=0, ge=0, strict=True)
