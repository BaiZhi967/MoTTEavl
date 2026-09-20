"""比较契约（M6-T01 Lite）：固定 RunReportRef 与 ComparisonPolicy。

RunReportRef 钉住 run/scoring pass/schema/evidence hash；ComparisonPolicy
声明允许变量（合法因子见 ``ALLOWED_COMPARISON_FACTORS``）。执行指纹与比较
签名分开：比较按任务源/内容/期望对齐，不按渲染后的 prompt。

M3（review R09）把冻结实验条件补进因子清单：重复数、Agent、超时、资源、
工具、重试、环境与凭据引用都是**可以**被政策显式允许变化的变量，但默认
不允许——不写进政策就表示"这些条件必须一致"，不能被当作模型差异比较。
"""
from __future__ import annotations

from pydantic import Field, field_validator

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
})


class RunReportRef(Contract):
    """对固定 Run + ScoringPass 报告的不可变引用。"""

    run_id: str = Field(min_length=1)
    scoring_pass_id: str = Field(min_length=1)
    report_schema: str = Field(min_length=1)
    evidence_hash: str = Field(min_length=1)


class ComparisonPolicy(Contract):
    """比较政策：显式列出允许变化的因子。"""

    allowed_factors: tuple[str, ...] = ()

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
