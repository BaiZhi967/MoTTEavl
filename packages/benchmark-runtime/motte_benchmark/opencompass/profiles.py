"""C-Eval 外部评测 Profile（冻结口径，M2 需求 5.2）。

Profile 固定 benchmark/dataset 版本、学科选择、split、few-shot 来源分区、
prompt/提取器/聚合版本与 Runner/环境指纹；占位值（latest/TBD 等）一律
拒绝。few-shot 示例只能来自与评测分区不同的合法分区，目标样本 gold 不
进入 subject 输入（由 T05 的配置转换保证，这里先钉住分区约束）。
"""
from __future__ import annotations

from typing import Any

CEVAL_BENCHMARK_ID = "ceval"
CEVAL_BENCHMARK_VERSION = "1"
CEVAL_PROMPT_TEMPLATE_VERSION = "ceval-zh-mcq-v1"
CEVAL_ANSWER_EXTRACTOR = "first-option"
CEVAL_EXTRACTOR_VERSION = "1"
CEVAL_AGGREGATION = "subject-macro-and-sample-weighted"
CEVAL_AGGREGATION_VERSION = "1"

# 可评 split 以所选数据版本为准；val 官方带 gold，test 不带（unscored）。
CEVAL_SPLITS = ("val", "test", "dev")

_PLACEHOLDER_VALUES = {"", "latest", "tbd", "todo", "placeholder", "unpinned", "unknown"}


def _pinned(value: str, field: str) -> str:
    if not isinstance(value, str) or value.strip().lower() in _PLACEHOLDER_VALUES:
        raise ValueError(f"{field} must be pinned to a real value, not a placeholder: {value!r}")
    return value


def ceval_external_profile(
    *,
    dataset_revision: str,
    subjects: tuple[str, ...] | list[str],
    split: str = "val",
    few_shot: int = 0,
    few_shot_split: str = "dev",
    seed: int,
    runner_version: str,
    environment_digest: str,
    max_output_tokens: int = 1024,
) -> dict[str, Any]:
    """构造冻结的 C-Eval 外部 Profile 字典（进入 ExternalJobSpec.profile）。"""
    _pinned(dataset_revision, "dataset_revision")
    _pinned(runner_version, "runner_version")
    _pinned(environment_digest, "environment_digest")
    if split not in CEVAL_SPLITS:
        raise ValueError(f"unknown C-Eval split: {split!r} (known: {CEVAL_SPLITS})")
    if few_shot_split not in CEVAL_SPLITS:
        raise ValueError(f"unknown few-shot split: {few_shot_split!r}")
    if few_shot_split == split:
        raise ValueError(
            "few-shot examples must come from a different partition than the "
            f"evaluated split: both are {split!r}"
        )
    if type(few_shot) is not int or few_shot < 0 or few_shot > 32:
        raise ValueError("few_shot must be an integer in [0, 32]")
    normalized_subjects = tuple(dict.fromkeys(str(subject).strip() for subject in subjects))
    if not normalized_subjects or any(not subject for subject in normalized_subjects):
        raise ValueError("subjects must be a non-empty list of non-empty names")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    if type(max_output_tokens) is not int or max_output_tokens <= 0:
        raise ValueError("max_output_tokens must be a positive integer")
    return {
        "benchmark_id": CEVAL_BENCHMARK_ID,
        "benchmark_version": CEVAL_BENCHMARK_VERSION,
        "dataset_revision": dataset_revision,
        "split": split,
        "selected_subjects": normalized_subjects,
        "few_shot": {"count": few_shot, "source_split": few_shot_split},
        "prompt_template_version": CEVAL_PROMPT_TEMPLATE_VERSION,
        "answer_extractor": CEVAL_ANSWER_EXTRACTOR,
        "extractor_version": CEVAL_EXTRACTOR_VERSION,
        "aggregation": CEVAL_AGGREGATION,
        "aggregation_version": CEVAL_AGGREGATION_VERSION,
        "runner_version": runner_version,
        "environment_digest": environment_digest,
        "seed": seed,
        "max_output_tokens": max_output_tokens,
    }
