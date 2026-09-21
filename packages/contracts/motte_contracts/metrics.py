"""指标注册表契约（M6-T02）：版本化的 metric 定义与合法分母。

每个 metric 声明 id/version、单位、方向、聚合方式、分母政策、资格政策、
所需证据与缺失政策。套件（GSM8K/Direct LLM/C-Eval/Harbor/Scenario）各自
的合法分母**不改写**；注册表是声明的事实源，Gate/统计按 metric 身份取值。
"""
from __future__ import annotations

from pydantic import Field, field_validator

from .messages import Contract

METRIC_REGISTRY_VERSION = "metric-registry@1"

#: 已注册分母口径（协议 §2）。新增口径必须同步协议文档。
METRIC_DENOMINATORS: frozenset[str] = frozenset({
    "selected_cases",   # GSM8K / C-Eval / 场景：选中集
    "judged_cases",     # Direct LLM：有期望且已判定
    "valid_trials",     # Harbor：有效 Trial
    "planned_trials",   # Harbor：计划 Trial（覆盖口径）
    "tasks",            # Task 级统计
    "run",              # Run 级（成本/时长的单位是 Run 本身）
})

#: 聚合方式。
METRIC_AGGREGATIONS: frozenset[str] = frozenset({
    "mean", "median", "ratio", "sum", "pass_at_k", "paired_difference",
})

#: 缺失政策：fail_closed = 缺失即不足证据；不计入 = 明确不适用（如无期望样本）。
METRIC_MISSING_POLICIES: frozenset[str] = frozenset({
    "fail_closed", "excluded_by_design",
})


class MetricDefinition(Contract):
    metric_id: str = Field(min_length=1)
    version: str = Field(default="1", min_length=1)
    unit: str = Field(min_length=1)
    direction: str
    aggregation: str
    denominator: str
    eligibility: str = Field(min_length=1)
    required_evidence: tuple[str, ...] = ()
    missing_policy: str = "fail_closed"
    description: str = ""

    @field_validator("direction")
    @classmethod
    def _direction_must_be_known(cls, value: str) -> str:
        if value not in {"gte", "lte"}:
            raise ValueError(f"unknown direction: {value!r} (known: gte,lte)")
        return value

    @field_validator("denominator")
    @classmethod
    def _denominator_must_be_known(cls, value: str) -> str:
        if value not in METRIC_DENOMINATORS:
            raise ValueError(
                f"unknown denominator: {value!r} "
                f"(known: {','.join(sorted(METRIC_DENOMINATORS))})"
            )
        return value

    @field_validator("aggregation")
    @classmethod
    def _aggregation_must_be_known(cls, value: str) -> str:
        if value not in METRIC_AGGREGATIONS:
            raise ValueError(
                f"unknown aggregation: {value!r} "
                f"(known: {','.join(sorted(METRIC_AGGREGATIONS))})"
            )
        return value

    @field_validator("missing_policy")
    @classmethod
    def _missing_policy_must_be_known(cls, value: str) -> str:
        if value not in METRIC_MISSING_POLICIES:
            raise ValueError(
                f"unknown missing policy: {value!r} "
                f"(known: {','.join(sorted(METRIC_MISSING_POLICIES))})"
            )
        return value

    @property
    def qualified_id(self) -> str:
        return f"{self.metric_id}@{self.version}"


def _metric(
    metric_id: str, *, unit: str, direction: str, aggregation: str,
    denominator: str, eligibility: str, required_evidence: tuple[str, ...] = (),
    missing_policy: str = "fail_closed", description: str = "",
) -> MetricDefinition:
    return MetricDefinition(
        metric_id=metric_id, unit=unit, direction=direction,
        aggregation=aggregation, denominator=denominator, eligibility=eligibility,
        required_evidence=required_evidence, missing_policy=missing_policy,
        description=description,
    )


#: 注册表 v1：继承 gate-lite@2 的四个指标并补 M6 需要的口径。
#: 键是 metric_id（不带版本）；``qualified_id`` 用于报告引用。
METRIC_REGISTRY: dict[str, MetricDefinition] = {
    definition.metric_id: definition
    for definition in (
        _metric(
            "accuracy", unit="ratio", direction="gte", aggregation="ratio",
            denominator="selected_cases", eligibility="all selected cases judged",
            description="selected-case accuracy (platform recomputation)",
        ),
        _metric(
            "judged_accuracy", unit="ratio", direction="gte", aggregation="ratio",
            denominator="judged_cases",
            eligibility="cases with expectation and deterministic judgement",
            missing_policy="excluded_by_design",
            description="judged-case accuracy (Direct LLM denominator)",
        ),
        _metric(
            "valid_trial_pass_rate", unit="ratio", direction="gte",
            aggregation="ratio", denominator="valid_trials",
            eligibility="valid trials (Harbor rewards)",
            description="Terminal-Bench valid-Trial pass rate",
        ),
        _metric(
            "valid_trial_coverage", unit="ratio", direction="gte",
            aggregation="ratio", denominator="planned_trials",
            eligibility="valid trials over frozen trial plan",
            description="valid Trials over the frozen trial plan (Harbor)",
        ),
        _metric(
            "cost.total_usd", unit="USD", direction="lte", aggregation="sum",
            denominator="run", eligibility="all usages priced in one currency",
            required_evidence=("price_table",),
            description="total observed run cost (USD only; no conversion)",
        ),
        _metric(
            "cost.per_success_usd", unit="USD", direction="lte", aggregation="sum",
            denominator="run", eligibility="at least one success",
            required_evidence=("price_table",),
            description="cost per successful case/trial; not applicable at zero successes",
        ),
        _metric(
            "pass_at_k", unit="ratio", direction="gte", aggregation="pass_at_k",
            denominator="tasks",
            eligibility="planned, independent, complete trials (n>=k>=1)",
            description="pass@k over pre-planned independent trials",
        ),
        _metric(
            "latency.p50_ms", unit="ms", direction="lte", aggregation="median",
            denominator="run", eligibility="observed case latencies",
            description="median case latency in milliseconds",
        ),
    )
}


def lookup_metric(metric_id: str) -> MetricDefinition | None:
    """按 metric_id 查注册表；带 ``@version`` 后缀时同时校验版本。"""
    if "@" in metric_id:
        base, _, version = metric_id.partition("@")
        definition = METRIC_REGISTRY.get(base)
        if definition is not None and definition.version != version:
            return None
        return definition
    return METRIC_REGISTRY.get(metric_id)
