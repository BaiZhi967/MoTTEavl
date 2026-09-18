"""评测套件分发的单一来源：数据集/场景记录 → 属于哪个套件、按哪套契约校验。

目前登记两个套件：

- ``gsm8k``：官方数学基准，记录用顶层 ``benchmark`` 键（``motte_contracts.gsm8k``）。
- ``direct-llm``：通用直连评测，记录用顶层 ``eval`` 键（``motte_contracts.direct_llm``）。

新增套件 = 在 ``SUITES`` 里加一项；资源存储的不可变/严格校验、运行创建期的 manifest 展开、
运行后的评分都通过本模块分发，不在调用点写 ``if suite == ...``。
"""
from __future__ import annotations

from typing import Any, Callable

from . import direct_llm, gsm8k
from .selection import CASE_SELECTION_KEY, RUN_SELECTION_KEY

# 运行快照键：两种套件共用同一组 manifest 键，service/api 的执行与报告链路因此无需分叉。
SNAPSHOT_KEY = "benchmark_snapshot"
PROVENANCE_KEY = "benchmark_provenance"
RESERVED_KEYS = frozenset({SNAPSHOT_KEY, PROVENANCE_KEY, "benchmark_cases"})


def _gsm8k_match(record: Any) -> bool:
    return gsm8k.is_benchmark(record)


def _direct_llm_match(record: Any) -> bool:
    return direct_llm.is_dataset(record) or direct_llm.is_scenario(record)


# suite id → (归属判定, 数据集校验, 场景校验)
SUITES: dict[str, tuple[Callable[[Any], bool], Callable[[dict[str, Any]], None],
                        Callable[[dict[str, Any]], None]]] = {
    gsm8k.SUITE: (_gsm8k_match, gsm8k.validate_dataset, gsm8k.validate_scenario),
    direct_llm.SUITE: (_direct_llm_match, direct_llm.validate_dataset, direct_llm.validate_scenario),
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
    suite = provenance.get("suite")
    if suite in SUITES:
        return str(suite)
    return gsm8k.SUITE


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


__all__ = ["CASE_SELECTION_KEY", "PROVENANCE_KEY", "RESERVED_KEYS", "RUN_SELECTION_KEY",
           "SNAPSHOT_KEY", "SUITES", "is_managed", "suite_of", "suite_of_run",
           "validate_dataset", "validate_scenario"]
