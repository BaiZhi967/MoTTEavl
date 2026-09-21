"""MoTTEavl evaluator package.

这里刻意**不**在包导入时加载 Judge / rubric 依赖：motte_eval._regex_worker
会在 spawn 出来的子进程里被重新导入，子进程必须保持 stdlib-only（见该模块
文档）。因此 re-export 采用 PEP 562 的惰性属性：from motte_eval import JudgeSpec
仍然可用，而 import motte_eval 只付一次字典查找的代价。
"""
from __future__ import annotations

from importlib import import_module
from typing import Any

__version__ = "0.1.0"

#: 惰性 re-export 表：公开名 -> 定义它的子模块。
_LAZY_EXPORTS: dict[str, str] = {
    "CalibrationCall": ".calibration",
    "CalibrationError": ".calibration",
    "CalibrationPolicy": ".rubrics",
    "CalibrationReport": ".calibration",
    "CalibrationSample": ".calibration",
    "CalibrationSet": ".calibration",
    "Criterion": ".rubrics",
    "CriterionConfusion": ".calibration",
    "HumanReviewRequired": ".calibration",
    "JudgeQualification": ".calibration",
    "ManualRevisionConflict": ".calibration",
    "ManualRevisionRequest": ".calibration",
    "ManualScoreChange": ".calibration",
    "QualificationRegistry": ".calibration",
    "apply_manual_revision_to_store": ".calibration",
    "build_calibration_report": ".calibration",
    "build_calibration_set": ".calibration",
    "build_manual_revision": ".calibration",
    "candidate_sample": ".calibration",
    "manual_revision_pass": ".calibration",
    "pass_gate_eligibility": ".calibration",
    "qualification_record": ".calibration",
    "qualify_judge": ".calibration",
    "repeat_plan": ".calibration",
    "require_human_reviewed": ".calibration",
    "review_sample": ".calibration",
    "JUDGE_PURPOSE": ".judge",
    "JudgeAuthorisation": ".judge",
    "JudgeBudget": ".judge",
    "JudgeBudgetError": ".judge",
    "JudgeCandidateInput": ".judge",
    "JudgeCandidateRef": ".judge",
    "JudgeError": ".judge",
    "JudgeInputBundle": ".judge",
    "JudgeInputError": ".judge",
    "JudgeInputSelector": ".judge",
    "JudgeNotAuthorised": ".judge",
    "JudgeOutcome": ".judge",
    "JudgePairwiseInput": ".judge",
    "JudgePairwiseOutcome": ".judge",
    "JudgePreflight": ".judge",
    "JudgeSpec": ".judge",
    "JudgeSpecError": ".judge",
    "Rubric": ".rubrics",
    "RubricError": ".rubrics",
    "available_rubrics": ".rubrics",
    "build_judge_input": ".judge",
    "build_judge_request": ".judge",
    "build_judge_spec": ".judge",
    "build_pairwise_input": ".judge",
    "build_rubric": ".rubrics",
    "get_rubric": ".rubrics",
    "judge_job_fingerprint": ".judge",
    "judge_metrics": ".judge",
    "judge_spec_sha256": ".judge",
    "pairwise_metrics": ".judge",
    "parse_judge_output": ".judge",
    "parse_pairwise_output": ".judge",
    "policy_for": ".rubrics",
    "preflight_judge": ".judge",
    "scan_candidate_content": ".judge",
    "validate_policy": ".rubrics",
}

__all__ = sorted(_LAZY_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY_EXPORTS})
