"""实验契约（M6-T04/T05）：ExperimentSpec、FactorRegistry、ExperimentCell。

Experiment 只编排既有 Run/Dispatcher/Worker——不建立第二套执行队列。Spec
发布后不可变；cell_id 是 experiment version + canonical factor assignment +
repeat_index 的稳定 hash。experiment repeat 产生独立 Cell/Run；Harbor Trial
只在 Run 内聚合，两层不相等、不相乘（协议 §1）。
"""
from __future__ import annotations

from pydantic import Field, field_validator, model_validator

from .hashing import canonical_hash
from .messages import Contract

#: 首批资源因素（协议/执行计划 T04）。每个因素有资源 owner 与合法值域；
#: 数据集/评分器变更不是模型比较的允许因素——它们是 task_ref/evaluation_ref。
EXPERIMENT_FACTORS: frozenset[str] = frozenset({
    "model_profile",
    "reasoning_level",
    "prompt_version",
    "runtime_version",
    "skill_version",
})

#: 因素 → 资源 owner（校验 factor value 时按 owner 解析）。
FACTOR_OWNERS: dict[str, str] = {
    "model_profile": "models",
    "reasoning_level": "models",
    "prompt_version": "workflows",
    "runtime_version": "runtimes",
    "skill_version": "skills",
}

#: 矩阵与规模上限（执行计划 T04：超限整体拒绝，不能先排队一部分）。
DEFAULT_MAX_CELLS = 200
DEFAULT_MAX_RUNS_PER_CELL = 1  # 正式分配恰好一个 initial Run；retry 走显式子 Run
DEFAULT_MAX_TOTAL_CALLS = 2000
DEFAULT_MAX_TOTAL_TOKENS = 20_000_000


class FactorValue(Contract):
    """一个因素取值：值 + 资源引用 + 版本 hash（创建时冻结，TOCTOU 重校验）。"""

    factor: str
    value: str = Field(min_length=1)
    resource_ref: str | None = None
    version_hash: str | None = None

    @field_validator("factor")
    @classmethod
    def _factor_must_be_known(cls, value: str) -> str:
        if value not in EXPERIMENT_FACTORS:
            raise ValueError(
                f"unknown experiment factor: {value!r} "
                f"(known: {','.join(sorted(EXPERIMENT_FACTORS))})"
            )
        return value


class FactorAssignment(Contract):
    """一个 Cell 的因素赋值：canonical 顺序按 factor 名排序。"""

    values: tuple[FactorValue, ...] = ()

    @model_validator(mode="after")
    def _canonical_order_and_unique(self) -> FactorAssignment:
        factors = [value.factor for value in self.values]
        if len(factors) != len(set(factors)):
            duplicates = sorted({f for f in factors if factors.count(f) > 1})
            raise ValueError("duplicate factors: " + ",".join(duplicates))
        if factors != sorted(factors):
            raise ValueError(
                "factor assignment must be canonical (sorted by factor name): "
                + ",".join(factors)
            )
        return self

    def as_dict(self) -> dict[str, str]:
        return {value.factor: value.value for value in self.values}

    def canonical_payload(self) -> dict[str, dict[str, str | None]]:
        return {
            value.factor: {
                "value": value.value,
                "resource_ref": value.resource_ref,
                "version_hash": value.version_hash,
            }
            for value in self.values
        }


class BudgetPolicy(Contract):
    """实验级预算（执行计划 T04）：超限整体拒绝。null 表示不设该维上限。"""

    max_total_calls: int = Field(ge=1, strict=True)
    max_total_tokens: int | None = Field(default=None, ge=1, strict=True)
    max_cost_usd: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    #: 已知费用上限需要价格表；未知费用不能通过严格成本上限（协议 §6.2）。
    cost_known_required: bool = False


class StopPolicy(Contract):
    """停止政策（协议 §1/执行计划 T04）：不允许"直到成功"。"""

    on_first_failure: bool = False
    max_failures: int | None = Field(default=None, ge=0, strict=True)
    wall_clock_seconds: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def _no_retry_until_success(self) -> StopPolicy:
        # StopPolicy 只描述"何时停"，永不描述"何时重跑提高分数"；显式 retry
        # 走 superseding 子 Run（保留原结果），不在此处表达。
        if self.max_failures == 0 and self.on_first_failure:
            raise ValueError("on_first_failure with max_failures=0 is contradictory")
        return self


class EvaluationRef(Contract):
    """评分引用：Experiment 的 Run 评分固定用哪个评分协议。"""

    scoring: str = "default"
    scoring_pass_hint: str | None = None
    statistical_policy: str = "statistical_policy@1"


class ExperimentSpec(Contract):
    """实验规格（发布后不可变；新内容 = 新 version）。"""

    experiment_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    #: task_ref：suite + 既有场景/数据集引用（{"suite": ..., "scenario_version": ...}）。
    task_ref: dict[str, str] = Field(min_length=1)
    selected_case_keys: tuple[str, ...] = ()
    #: factor → 取值列表（矩阵维度）；空列表 = 该因素不变化。
    factors: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    #: 冻结的 Run 级控制条件（timeouts/budgets/工具等；进 manifest）。
    controlled_conditions: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict,
    )
    #: 事前声明的 experiment repeat 数（≠ Harbor trial repeat）。
    repeats: int = Field(default=1, ge=1, strict=True)
    #: 每 Run 的 Trial 计划（Harbor 类）；非 Trial 套件必须为 None。
    trials_per_run: int | None = Field(default=None, ge=1, strict=True)
    budget_policy: BudgetPolicy
    evaluation_ref: EvaluationRef = Field(default_factory=EvaluationRef)
    stop_policy: StopPolicy = Field(default_factory=StopPolicy)
    #: 规模护栏（拒绝超矩阵/超预算的 preview 与创建）。
    max_cells: int = Field(default=DEFAULT_MAX_CELLS, ge=1, strict=True)
    created_by: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    created_at: str | None = None

    @field_validator("task_ref")
    @classmethod
    def _task_ref_needs_suite(cls, value: dict[str, str]) -> dict[str, str]:
        if not value.get("suite"):
            raise ValueError("task_ref requires a suite")
        return value

    @field_validator("factors")
    @classmethod
    def _factors_must_be_known(cls, value: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        unknown = sorted(set(value) - EXPERIMENT_FACTORS)
        if unknown:
            raise ValueError(
                "unknown factors: " + ",".join(unknown)
                + f" (known: {','.join(sorted(EXPERIMENT_FACTORS))})"
            )
        for factor, values in value.items():
            if not values:
                raise ValueError(f"factor {factor!r} declares no values")
            if len(set(values)) != len(values):
                raise ValueError(f"factor {factor!r} declares duplicate values")
        return value

    def cell_count(self) -> int:
        """矩阵展开的 cell 数 = 各因素取值数乘积 × repeats。"""
        product = 1
        for values in self.factors.values():
            product *= len(values)
        return product * self.repeats

    def max_potential_calls(self) -> int:
        """最大潜在模型调用数（preview 用）：cell 数 × 每 Run 估算调用。"""
        per_run = self.trials_per_run or 1
        # selected_case_keys 为空表示全集：preview 阶段按 1 计，创建前由
        # 资源解析替换为真实 case 数（Experiments service 负责）。
        cases = len(self.selected_case_keys) or 1
        return self.cell_count() * per_run * cases

    def content_hash(self) -> str:
        payload = self.model_dump()
        return canonical_hash(payload)


#: Cell 分配状态（执行计划 T05）。
CELL_STATUSES: frozenset[str] = frozenset({
    "pending", "allocating", "allocated", "failed", "cancelled",
})


class ExperimentCell(Contract):
    """实验单元：同实验同配置同 repeat 只分配一次（cell_id 唯一约束保护）。"""

    cell_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    experiment_version: str = Field(min_length=1)
    factor_assignment: FactorAssignment
    repeat_index: int = Field(ge=0, strict=True)
    resolved_spec_hash: str = Field(min_length=1)
    run_id: str | None = None
    allocation_status: str = "pending"
    #: 显式 retry 产生的 superseding 子 Run 记录（原 initial Run 不消失）。
    superseding_run_ids: tuple[str, ...] = ()
    failure_reason: str | None = None

    @field_validator("allocation_status")
    @classmethod
    def _status_must_be_known(cls, value: str) -> str:
        if value not in CELL_STATUSES:
            raise ValueError(
                f"unknown cell status: {value!r} "
                f"(known: {','.join(sorted(CELL_STATUSES))})"
            )
        return value


def compute_cell_id(
    experiment_id: str, experiment_version: str,
    assignment: FactorAssignment, repeat_index: int,
) -> str:
    """cell_id = sha256(experiment_id, version, canonical assignment, repeat)。"""
    return canonical_hash({
        "experiment_id": experiment_id,
        "experiment_version": experiment_version,
        "factors": assignment.canonical_payload(),
        "repeat_index": repeat_index,
    })


def cell_key(assignment: FactorAssignment, repeat_index: int) -> str:
    """分组 baseline 的 cell 条件键：factor 赋值 + repeat（不含实验 id/version）。"""
    return canonical_hash({
        "factors": assignment.canonical_payload(),
        "repeat_index": repeat_index,
    })
