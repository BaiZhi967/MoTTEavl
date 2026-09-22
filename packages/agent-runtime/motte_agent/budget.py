"""Agent 执行预算：步数 / 工具次数 / 时长 / 可观察 token 与费用。

每个维度独立记录强制能力（enforced / observed / unknown）：
- enforced：运行时在动作前检查并可阻断。
- observed：只在事后计量（如 Provider 报告的 cost），超限后停止后续动作。
- unknown：无 tokenizer 或 Provider 不报 usage 时，不得宣称硬限制。
时长一律使用单调时钟。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

ENFORCED = "enforced"
OBSERVED = "observed"
UNKNOWN = "unknown"

# 允许的最大取值；配置超过上限在创建期拒绝（合理正值且有上限）。
LIMITS_CEILING = {
    "max_steps": 64,
    "max_tool_calls": 256,
    "wall_time_sec": 3600.0,
    "per_call_timeout_sec": 600.0,
    "max_output_tokens": 1_000_000,
    "total_token_limit": 100_000_000,
    "observed_cost_limit": 10_000.0,
}

BUDGET_FIELDS = (
    "max_steps", "max_tool_calls", "wall_time_sec", "per_call_timeout_sec",
    "max_output_tokens", "total_token_limit", "observed_cost_limit",
)


def _is_finite_number(value: int | float) -> bool:
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


class BudgetConfigError(ValueError):
    """预算配置非法（非正数 / 超过上限 / 类型错误）。"""


@dataclass
class ExecutionBudget:
    max_steps: int = 8
    max_tool_calls: int | None = None
    wall_time_sec: float | None = None
    per_call_timeout_sec: float | None = None
    max_output_tokens: int | None = None
    total_token_limit: int | None = None
    observed_cost_limit: float | None = None
    per_call_timeout_enforced: bool = False
    _started_monotonic: float = field(default_factory=time.monotonic, repr=False)
    _tool_calls_made: int = field(default=0, repr=False)
    _usage_reported: bool = field(default=False, repr=False)
    _total_tokens: int = field(default=0, repr=False)
    _observed_cost: float = field(default=0.0, repr=False)

    @staticmethod
    def from_config(config: dict[str, Any] | None) -> ExecutionBudget:
        config = dict(config or {})
        unknown = set(config) - {"max_steps", "max_tool_calls", "per_call_timeout_enforced",
                                 *BUDGET_FIELDS}
        if unknown:
            raise BudgetConfigError(f"unknown budget fields: {', '.join(sorted(unknown))}")
        values: dict[str, Any] = {}
        for name in BUDGET_FIELDS:
            value = config.get(name)
            if value is None:
                continue
            ceiling = LIMITS_CEILING[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise BudgetConfigError(f"budget.{name} must be a number")
            if not _is_finite_number(value):
                raise BudgetConfigError(f"budget.{name} must be finite")
            if value <= 0:
                raise BudgetConfigError(f"budget.{name} must be positive")
            if value > ceiling:
                raise BudgetConfigError(
                    f"budget.{name}={value} exceeds the allowed ceiling {ceiling}"
                )
            if name in ("max_steps", "max_tool_calls", "max_output_tokens",
                        "total_token_limit") and (
                not isinstance(value, int) or isinstance(value, bool)
            ):
                raise BudgetConfigError(f"budget.{name} must be an integer")
            values[name] = value if name in ("max_steps", "max_tool_calls",
                                              "max_output_tokens",
                                              "total_token_limit") else float(value)
        enforced = config.get("per_call_timeout_enforced", False)
        if not isinstance(enforced, bool):
            raise BudgetConfigError("per_call_timeout_enforced must be a boolean")
        budget = ExecutionBudget(**values)
        budget.max_steps = values.get("max_steps", 8)
        budget.per_call_timeout_enforced = enforced
        return budget

    # ---------------------------------------------------------------- 检查点

    def step_allowed(self, step: int) -> str | None:
        """步数检查；step 从 1 开始。返回停止原因或 None。"""
        if step > self.max_steps:
            return "max_steps"
        return self._wall_or_usage_stop()

    def tool_call_allowed(self) -> str | None:
        if self.max_tool_calls is not None and self._tool_calls_made >= self.max_tool_calls:
            return "max_tool_calls"
        return None

    def record_tool_call(self) -> None:
        self._tool_calls_made += 1

    def tool_calls_made(self) -> int:
        return self._tool_calls_made

    def wall_elapsed(self) -> float:
        return time.monotonic() - self._started_monotonic

    def per_call_deadline(self, *, now: float | None = None) -> float | None:
        """本次模型调用的单调时钟期限；未配置返回 None。"""
        if self.per_call_timeout_sec is None:
            return None
        current = time.monotonic() if now is None else now
        return current + self.per_call_timeout_sec

    def record_usage(self, usage: dict[str, Any] | None, cost: dict[str, Any] | None) -> None:
        """Provider 报告的 usage/cost 事后计量；不报告则保持 unknown。"""
        total = usage.get("total_tokens") if isinstance(usage, dict) else None
        if total is not None:
            if isinstance(total, bool) or not isinstance(total, int):
                raise BudgetConfigError("usage.total_tokens must be a non-negative integer")
            if total < 0:
                raise BudgetConfigError("usage.total_tokens must be non-negative")
            self._usage_reported = True
            self._total_tokens += total
        total_cost = cost.get("total") if isinstance(cost, dict) else None
        if total_cost is not None:
            if (
                isinstance(total_cost, bool)
                or not isinstance(total_cost, (int, float))
                or not _is_finite_number(total_cost)
            ):
                raise BudgetConfigError("cost.total must be finite")
            if total_cost < 0:
                raise BudgetConfigError("cost.total must be non-negative")
            self._observed_cost += float(total_cost)

    def _wall_or_usage_stop(self) -> str | None:
        if self.wall_time_sec is not None and self.wall_elapsed() > self.wall_time_sec:
            return "wall_time"
        if self.total_token_limit is not None and self._usage_reported:
            if self._total_tokens > self.total_token_limit:
                return "token_limit"
        if self.observed_cost_limit is not None and self._observed_cost > self.observed_cost_limit:
            return "cost_limit"
        return None

    # ---------------------------------------------------------------- 自描述

    def enforcement_report(self) -> dict[str, dict[str, Any]]:
        """记录每个维度实际强制能力；unknown 不得伪装成 enforced。"""
        usage_dependent = UNKNOWN if not self._usage_reported else ENFORCED
        report: dict[str, dict[str, Any]] = {
            "max_steps": {"limit": self.max_steps, "enforcement": ENFORCED},
            "max_tool_calls": {"limit": self.max_tool_calls, "enforcement":
                               ENFORCED if self.max_tool_calls is not None else "not_configured"},
            "wall_time_sec": {"limit": self.wall_time_sec, "enforcement":
                              ENFORCED if self.wall_time_sec is not None else "not_configured"},
            "per_call_timeout_sec": {
                "limit": self.per_call_timeout_sec,
                "enforcement": (
                    ENFORCED if self.per_call_timeout_sec is not None
                    and self.per_call_timeout_enforced else
                    OBSERVED if self.per_call_timeout_sec is not None else "not_configured"
                ),
            },
            "max_output_tokens": {"limit": self.max_output_tokens, "enforcement":
                                  "not_configured" if self.max_output_tokens is None else ENFORCED},
            "total_token_limit": {"limit": self.total_token_limit, "enforcement":
                                  "not_configured" if self.total_token_limit is None else usage_dependent},
            "observed_cost_limit": {"limit": self.observed_cost_limit, "enforcement":
                                    OBSERVED if self.observed_cost_limit is not None
                                    else "not_configured"},
        }
        return report

    def observed_usage(self) -> dict[str, Any]:
        return {
            "reported": self._usage_reported,
            "total_tokens": self._total_tokens if self._usage_reported else None,
            "observed_cost": round(self._observed_cost, 6),
        }
