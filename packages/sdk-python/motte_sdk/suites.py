"""评测套件的运行时分发：创建期 manifest 展开、运行后评分、报告聚合。

套件归属由契约层 ``motte_contracts.suites`` 判定（记录形状是唯一事实源），本模块只负责把归属
映射到具体实现。调用点（resolve/service/api）因此不再写 ``if suite == ...``。
"""
from __future__ import annotations

from typing import Any

from motte_contracts import suites as contract_suites
from motte_contracts.direct_llm import SUITE as DIRECT_LLM_SUITE
from motte_contracts.gsm8k import SUITE as GSM8K_SUITE
from motte_contracts.selection import run_selected_count

PROVENANCE_KEY = contract_suites.PROVENANCE_KEY
SNAPSHOT_KEY = contract_suites.SNAPSHOT_KEY
RESERVED_KEYS = contract_suites.RESERVED_KEYS
CASE_SELECTION_KEY = contract_suites.CASE_SELECTION_KEY


def resolve_managed_manifest(scenario: dict[str, Any], manifest: dict[str, Any],
                             resources: Any) -> dict[str, Any]:
    """套件场景的创建期展开（基准资源只在这里展开一次，Worker 不再碰资源库）。"""
    suite = contract_suites.suite_of(scenario)
    if suite == GSM8K_SUITE:
        from motte_sdk.benchmark import resolve_benchmark_manifest

        return resolve_benchmark_manifest(scenario, manifest, resources)
    if suite == DIRECT_LLM_SUITE:
        from motte_sdk.direct_llm import resolve_direct_llm_manifest

        return resolve_direct_llm_manifest(scenario, manifest, resources)
    raise ValueError(f"scenario is not a managed eval suite: {scenario.get('name')!r}")


def managed_scores(run: dict[str, Any], results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按运行快照里的 suite 评分（旧 GSM8K 运行没有 suite 字段，回落 gsm8k）。"""
    if contract_suites.suite_of_run(run) == DIRECT_LLM_SUITE:
        from motte_sdk.direct_llm import direct_llm_scores

        return direct_llm_scores(run, results)
    from motte_sdk.benchmark import benchmark_scores

    return benchmark_scores(run, results)


def managed_aggregate(run: dict[str, Any], scores: list[dict[str, Any]]) -> dict[str, Any]:
    """套件口径的严格聚合；``denominator`` 说明 accuracy 的分母是什么。"""
    provenance = run["manifest"][PROVENANCE_KEY]
    selected = run_selected_count(provenance)
    if selected is None:
        raise ValueError("run snapshot has no selected case count")
    if contract_suites.suite_of_run(run) == DIRECT_LLM_SUITE:
        from motte_eval.direct_llm import aggregate_answers

        return {**aggregate_answers(scores, selected), "denominator": "judged_cases"}
    from motte_eval.gsm8k import aggregate_benchmark

    return {**aggregate_benchmark(scores, selected), "denominator": "selected_cases"}
