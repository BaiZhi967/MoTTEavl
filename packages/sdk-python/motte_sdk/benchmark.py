"""Offline benchmark import and immutable preparation shared by API/CLI."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from motte_contracts.gsm8k import (build_prompt, scenario_for, scenario_name,
                                   validate_dataset, validate_scenario)


def next_dataset_version(record: dict[str, Any], resources: Any) -> str:
    """自动版本号：同内容已存在则复用其版本（重复导入保持幂等），否则取下一个数字空号。"""
    known = [dataset for dataset in resources.datasets.list() if dataset.get("name") == record["name"]]
    for dataset in known:
        if dataset.get("cases_sha256") == record["cases_sha256"]:
            return str(dataset["version"])
    numeric = [int(dataset["version"]) for dataset in known
               if isinstance(dataset.get("version"), str) and dataset["version"].isdigit()]
    return str(max(numeric, default=0) + 1)


def persist_benchmark_dataset(record: dict[str, Any], scope: str, resources: Any,
                              *, version: str | None = None) -> dict[str, Any]:
    """写入不可变的数据集版本与配套场景，返回 API/CLI 一致的导入回执。

    ``version`` 为空时自动选版本（见 ``next_dataset_version``），因此"下载最新数据集"这种
    重复操作不会越攒越多版本号。同 name@version 内容不同时资源库抛 ResourceConflictError
    （API 映射 409）；数据集与场景是两次独立写入，场景冲突时数据集可能已落库，按文档用新
    版本号重试。
    """
    record = {**record, "version": version or next_dataset_version(record, resources)}
    scenario = scenario_for(record, name=scenario_name(record["name"], scope), version=record["version"])
    resources.datasets.put(record)
    resources.scenarios.put(scenario)
    return {"imported": f"{record['name']}@{record['version']}",
            "scenario": f"{scenario['name']}@{scenario['version']}",
            "scope": scope,
            "benchmark": record["benchmark"]["id"],
            "cases": len(record["cases"]),
            "revision": record["provenance"]["revision"],
            "source_sha256": record["provenance"]["source_sha256"],
            "cases_sha256": record["cases_sha256"]}


def import_benchmark_split(raw: bytes, *, name: str, version: str | None, revision: str,
                           license_id: str, scope: str, resources: Any, source: str | None = None,
                           synthetic: bool = False) -> dict[str, Any]:
    """校验整份官方 JSONL 并写入不可变数据集 + 场景，返回回执（API 与 CLI 共用）。

    ``version`` 为 None/空串表示自动版本号；``import_official_jsonl`` 需要非空版本占位，
    真正落库前会被 ``persist_benchmark_dataset`` 覆盖。
    """
    from motte_contracts.gsm8k import import_official_jsonl
    from motte_sdk.gsm8k_source import OFFICIAL_SOURCE

    record = import_official_jsonl(raw, name=name, version=version or "1", revision=revision,
                                   license_id=license_id, scope=scope,
                                   source=source or OFFICIAL_SOURCE, synthetic=synthetic)
    return persist_benchmark_dataset(record, scope, resources, version=version or None)


def resolve_benchmark_manifest(scenario: dict[str, Any], manifest: dict[str, Any], resources: Any) -> dict[str, Any]:
    from motte_contracts.gsm8k import CASE_SELECTION_KEY, RUN_SELECTION_KEY, select_cases

    validate_scenario(scenario)
    name, version = scenario["dataset"].rsplit("@", 1)
    dataset = resources.datasets.get(name, version)
    if dataset is None:
        raise ValueError(f"dataset not found: {scenario['dataset']}")
    validate_dataset(dataset)
    forbidden = {"cases", "benchmark_cases", "benchmark_provenance", "benchmark_snapshot", "tools"}
    if forbidden.intersection(manifest):
        raise ValueError("benchmark cases, tools and snapshots cannot be overridden")
    if manifest.get("dataset", scenario["dataset"]) != scenario["dataset"]:
        raise ValueError("manifest dataset does not match scenario")
    # 运行级子集：数据集保持不可变，选中结果与随机种子进运行快照（回放/复核靠它）。
    run_selection = select_cases(dataset, manifest.get(CASE_SELECTION_KEY))
    selected = set(run_selection.pop("case_ids"))
    run_selection.pop("case_ids_sha256", None)
    resolved = deepcopy(manifest)
    resolved["cases"] = {case["case_id"]: {"prompt": build_prompt(case["input"])}
                         for case in dataset["cases"] if case["case_id"] in selected}
    resolved[CASE_SELECTION_KEY] = run_selection
    resolved["benchmark_snapshot"] = {"scenario": deepcopy(scenario), "dataset": deepcopy(dataset)}
    resolved["benchmark_provenance"] = {
        **deepcopy(dataset["benchmark"]), **deepcopy(dataset["provenance"]),
        "dataset": scenario["dataset"], "cases_sha256": dataset["cases_sha256"],
        "scenario": f"{scenario['name']}@{scenario['version']}",
        RUN_SELECTION_KEY: run_selection,
    }
    return resolved


def benchmark_scores(run: dict[str, Any], results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from motte_eval.gsm8k import aggregate_benchmark, score_benchmark_case
    from motte_contracts.gsm8k import SCORER_VERSION

    manifest = run["manifest"]
    if manifest["benchmark_provenance"]["scorer_version"] != SCORER_VERSION:
        raise ValueError("unsupported snapshot scorer version")
    expected = {c["case_id"]: c["expected"] for c in manifest["benchmark_snapshot"]["dataset"]["cases"]}
    rows = {r["case_id"]: r for r in results}
    scores = []
    for case_id in run["case_ids"]:
        row = rows.get(case_id)
        if row is None or row.get("outcome") == "not_attempted":
            score = {"outcome": "not_attempted", "passed": False, "attempted": False, "responded": False,
                     "scorer_version": SCORER_VERSION}
        else:
            score = score_benchmark_case(row.get("result"), expected[case_id])
        scores.append({"case_id": case_id, **score})
    return scores
