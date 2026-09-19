"""评测套件分发的单一来源：数据集/场景记录 → 属于哪个套件、按哪套契约校验。

目前登记两个套件：

- ``gsm8k``：官方数学基准，记录用顶层 ``benchmark`` 键（``motte_contracts.gsm8k``）。
- ``direct-llm``：通用直连评测，按 ``eval.version`` 分派 v1/v2 严格契约。

新增套件 = 在 ``SUITES`` 里加一项；资源存储的不可变/严格校验、运行创建期的 manifest 展开、
运行后的评分都通过本模块分发，不在调用点写 ``if suite == ...``。
"""

from __future__ import annotations

from typing import Any, Callable

from . import agent_tasks, direct_llm, direct_llm_v2, gsm8k
from .selection import CASE_SELECTION_KEY, RUN_SELECTION_KEY

# 运行快照键：两种套件共用同一组 manifest 键，service/api 的执行与报告链路因此无需分叉。
SNAPSHOT_KEY = "benchmark_snapshot"
PROVENANCE_KEY = "benchmark_provenance"
RESERVED_KEYS = frozenset({SNAPSHOT_KEY, PROVENANCE_KEY, "benchmark_cases"})


def _gsm8k_match(record: Any) -> bool:
    return gsm8k.is_benchmark(record)


def _agent_tasks_match(record: Any) -> bool:
    return isinstance(record, dict) and record.get("suite") == agent_tasks.SUITE


def _validate_agent_tasks_dataset(record: dict[str, Any]) -> None:
    agent_tasks.normalize_agent_tasks_dataset(record)


def _validate_agent_tasks_scenario(record: dict[str, Any]) -> None:
    if record.get("suite") != agent_tasks.SUITE:
        raise ValueError("agent-tasks scenario requires suite=agent-tasks")
    if record.get("plugin_version") != agent_tasks.PLUGIN_VERSION:
        raise ValueError(
            f"unsupported agent-tasks plugin_version: {record.get('plugin_version')!r}"
        )


def _direct_llm_match(record: Any) -> bool:
    if direct_llm.is_dataset(record) or direct_llm.is_scenario(record):
        return True
    if direct_llm_v2.is_dataset(record) or direct_llm_v2.is_scenario(record):
        return True
    if not isinstance(record, dict):
        return False
    eval_spec = record.get(direct_llm.EVAL_KEY)
    if not isinstance(eval_spec, dict) or eval_spec.get("suite") != direct_llm.SUITE:
        return False
    # Any explicit Direct identity/version marker stays managed so unknown combinations cannot
    # escape strict validation by changing more than one field at once.
    explicit_identity = "id" in eval_spec or "version" in eval_spec
    explicit_marker = "contract_version" in record or "plugin_version" in record
    return explicit_identity or explicit_marker


def _direct_llm_version(record: dict[str, Any]) -> int | None:
    eval_spec = record.get(direct_llm.EVAL_KEY)
    version = eval_spec.get("version") if isinstance(eval_spec, dict) else None
    return version if type(version) is int else None


def _validate_direct_llm_dataset(record: dict[str, Any]) -> None:
    version = _direct_llm_version(record)
    if version == direct_llm.DATASET_VERSION:
        direct_llm.validate_dataset(record)
        return
    if version == direct_llm_v2.DATASET_VERSION:
        direct_llm_v2.validate_dataset(record)
        return
    raise ValueError(f"unsupported direct-llm dataset contract version: {version!r}")


def _validate_direct_llm_scenario(record: dict[str, Any]) -> None:
    version = _direct_llm_version(record)
    if version == direct_llm.DATASET_VERSION:
        direct_llm.validate_scenario(record)
        return
    if version == direct_llm_v2.DATASET_VERSION:
        direct_llm_v2.validate_scenario(record)
        return
    raise ValueError(f"unsupported direct-llm scenario contract version: {version!r}")


# suite id → (归属判定, 数据集校验, 场景校验)
SUITES: dict[
    str,
    tuple[
        Callable[[Any], bool], Callable[[dict[str, Any]], None], Callable[[dict[str, Any]], None]
    ],
] = {
    gsm8k.SUITE: (_gsm8k_match, gsm8k.validate_dataset, gsm8k.validate_scenario),
    agent_tasks.SUITE: (
        _agent_tasks_match, _validate_agent_tasks_dataset, _validate_agent_tasks_scenario,
    ),
    direct_llm.SUITE: (
        _direct_llm_match,
        _validate_direct_llm_dataset,
        _validate_direct_llm_scenario,
    ),
}


def suite_of(record: Any) -> str | None:
    """记录归属的套件 id；不属于任何已登记套件时返回 None（按普通资源处理）。"""
    for suite, (matches, _, _) in SUITES.items():
        if matches(record):
            return suite
    return None


def is_managed(record: Any) -> bool:
    return suite_of(record) is not None


def suite_of_run(run: dict[str, Any]) -> str | None:
    """运行归属的套件：快照里的 ``suite`` 为准，旧 GSM8K 运行没有该字段时按 gsm8k 兼容。"""
    provenance = (run.get("manifest") or {}).get(PROVENANCE_KEY)
    if not isinstance(provenance, dict):
        return None
    if "suite" not in provenance:
        return gsm8k.SUITE
    suite = provenance.get("suite")
    if suite in SUITES:
        return str(suite)
    raise ValueError(f"unsupported explicit eval suite: {suite!r}")


def validate_dataset(record: dict[str, Any]) -> None:
    suite = suite_of(record)
    if suite is None:
        raise ValueError("unsupported dataset preset/version")
    # 已经判定归属，直接走该套件的严格校验（判定与校验必须来自同一个登记项）。
    SUITES[suite][1](record)


def validate_scenario(record: dict[str, Any]) -> None:
    suite = suite_of(record)
    if suite is None:
        raise ValueError("unsupported scenario preset/version")
    SUITES[suite][2](record)


def _validated_identity(record: Any, *, scenario: bool) -> tuple[str, str] | None:
    if not isinstance(record, dict):
        return None
    suite = suite_of(record)
    if suite is None:
        return None
    if scenario:
        validate_scenario(record)
    else:
        validate_dataset(record)
    version = _direct_llm_version(record) if suite == direct_llm.SUITE else 1
    if type(version) is not int:
        raise ValueError(f"validated {suite} record has no integer contract version")
    return suite, str(version)


def validated_dataset_identity(record: Any) -> tuple[str, str] | None:
    """Return a managed dataset identity only after its versioned contract validates."""
    return _validated_identity(record, scenario=False)


def validated_scenario_identity(record: Any) -> tuple[str, str] | None:
    """Return a managed scenario identity only after its versioned contract validates."""
    return _validated_identity(record, scenario=True)


__all__ = [
    "CASE_SELECTION_KEY",
    "PROVENANCE_KEY",
    "RESERVED_KEYS",
    "RUN_SELECTION_KEY",
    "SNAPSHOT_KEY",
    "SUITES",
    "is_managed",
    "suite_of",
    "suite_of_run",
    "validate_dataset",
    "validate_scenario",
    "validated_dataset_identity",
    "validated_scenario_identity",
]
