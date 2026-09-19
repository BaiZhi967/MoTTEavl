"""比较契约（M6-T01 Lite）：固定 RunReportRef 与 ComparisonPolicy。

RunReportRef 钉住 run/scoring pass/schema/evidence hash；ComparisonPolicy
声明允许变量（当前合法因子：model）。执行指纹与比较签名分开：比较按
任务源/内容/期望对齐，不按渲染后的 prompt。
"""
from __future__ import annotations

from pydantic import Field, field_validator

from .messages import Contract

ALLOWED_COMPARISON_FACTORS: frozenset[str] = frozenset({"model"})


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
