"""Direct LLM 评测的导入、内置样例数据集与运行准备（API/CLI 共用，零网络）。"""
from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from motte_contracts.direct_llm import (EVAL_KEY, PROMPT_VERSION, SCORER_VERSION, SUITE,
                                        effective_scorers, normalize_scorer, scenario_for,
                                        validate_dataset, validate_scenario)
from motte_contracts.selection import CASE_SELECTION_KEY, RUN_SELECTION_KEY
from motte_sdk.datasets import next_dataset_version

BUILTIN_ENV = "MOTTE_BUILTIN_DATASET_DIR"
BUILTIN_SUBDIR = Path("datasets") / "direct-llm"
# 内置样例由本仓库维护，license 用这个标记而不是第三方许可证。
BUILTIN_LICENSE = "internal-sample"
# 运行快照键与 GSM8K 共用（service/api 的执行与报告链路因此无需分叉）。
SNAPSHOT_KEY = "benchmark_snapshot"
PROVENANCE_KEY = "benchmark_provenance"
RESERVED_KEYS = frozenset({SNAPSHOT_KEY, PROVENANCE_KEY, "benchmark_cases", "tools", "cases"})


class BuiltinUnavailable(ValueError):
    """内置样例目录缺失（例如从 wheel 安装运行）。错误信息里给出可用的覆盖方式。"""


@dataclass(frozen=True)
class BuiltinDataset:
    id: str
    label: str
    description: str
    scorer: str
    file: str


# 仓库内置样例：与 datasets/direct-llm/ 下的文件一一对应。
BUILTIN_DATASETS: tuple[BuiltinDataset, ...] = (
    BuiltinDataset(
        id="direct-llm-exact-answer", label="单值问答（exact）",
        description="答案唯一的短问答，逐字精确匹配，考察指令遵循与精确性。",
        scorer="exact", file="direct-llm-exact-answer.jsonl"),
    BuiltinDataset(
        id="direct-llm-classify", label="四分类打标（contains）",
        description="把工单分到 billing/technical/account/other，输出含标签即通过。",
        scorer="contains", file="direct-llm-classify.jsonl"),
    BuiltinDataset(
        id="direct-llm-json-extract", label="结构化抽取（regex）",
        description="抽取 JSON 字段并用正则校验键值形状，含缺失字段填 null 与一题 contains 覆盖。",
        scorer="regex", file="direct-llm-json-extract.jsonl"),
)


def builtin_dir() -> Path:
    """内置样例目录：环境变量覆盖 > 源码树内 datasets/direct-llm。

    仓库以 editable 方式安装（uv workspace），因此从本模块回溯源码树在开发与 CI 下都成立；
    若换成非 editable 安装，用 ``MOTTE_BUILTIN_DATASET_DIR`` 指向样例目录即可。
    """
    override = os.environ.get(BUILTIN_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / BUILTIN_SUBDIR


def builtin_by_id(dataset_id: str) -> BuiltinDataset | None:
    return next((item for item in BUILTIN_DATASETS if item.id == dataset_id), None)


def builtin_source(dataset_id: str) -> bytes:
    """读取内置样例的原始 JSONL；未知 id 或缺文件都抛结构化错误。"""
    entry = builtin_by_id(dataset_id)
    if entry is None:
        raise BuiltinUnavailable(
            f"unknown builtin dataset: {dataset_id} "
            f"(known: {', '.join(item.id for item in BUILTIN_DATASETS)})")
    path = builtin_dir() / entry.file
    if not path.is_file():
        raise BuiltinUnavailable(
            f"builtin dataset directory not found: {builtin_dir()} "
            f"(set {BUILTIN_ENV} to the directory holding {entry.file})")
    return path.read_bytes()


def builtin_catalog() -> list[dict[str, Any]]:
    """内置样例清单 + 实际题数与导入契约版本（供 Web/CLI 直接展示）。"""
    from motte_contracts.direct_llm import import_direct_llm_jsonl

    items = []
    for entry in BUILTIN_DATASETS:
        item = {"id": entry.id, "label": entry.label, "description": entry.description,
                "scorer": entry.scorer, "source": f"builtin:{entry.id}", "importable": True}
        try:
            record = import_direct_llm_jsonl(builtin_source(entry.id), name=entry.id, version="1",
                                             license_id=BUILTIN_LICENSE, scorer=entry.scorer,
                                             source=item["source"])
            item["cases"] = len(record["cases"])
        except ValueError as error:  # 样例坏了要看得见，但不该让清单端点整体 500
            item["importable"] = False
            item["error"] = str(error)
        items.append(item)
    return items


def persist_direct_llm_dataset(record: dict[str, Any], resources: Any,
                               *, version: str | None = None) -> dict[str, Any]:
    """写入不可变数据集 + 配套场景，返回 API/CLI 一致的导入回执。

    ``version`` 为空时自动选版本（同内容复用，否则下一个空号）；场景名与数据集名一致。
    """
    record = {**record, "version": version or next_dataset_version(record, resources)}
    scenario = scenario_for(record, version=record["version"])
    resources.datasets.put(record)
    resources.scenarios.put(scenario)
    return {"imported": f"{record['name']}@{record['version']}",
            "scenario": f"{scenario['name']}@{scenario['version']}",
            "suite": SUITE,
            "scorer": record[EVAL_KEY]["scorer"],
            "cases": len(record["cases"]),
            "source": record["provenance"]["source"],
            "source_sha256": record["provenance"]["source_sha256"],
            "cases_sha256": record["cases_sha256"]}


def import_direct_llm_split(raw: bytes, *, name: str, version: str | None, license_id: str,
                           scorer: str | None = None, source: str = "local-jsonl",
                           resources: Any, synthetic: bool = False) -> dict[str, Any]:
    """校验整份 JSONL 并落库（API 与 CLI 共用）。``version`` 为 None/空串表示自动版本号。"""
    from motte_contracts.direct_llm import import_direct_llm_jsonl

    record = import_direct_llm_jsonl(raw, name=name, version=version or "1", license_id=license_id,
                                     scorer=scorer, source=source, synthetic=synthetic)
    return persist_direct_llm_dataset(record, resources, version=version or None)


def import_builtin_dataset(dataset_id: str, *, resources: Any, name: str | None = None,
                           version: str | None = None, license_id: str = BUILTIN_LICENSE,
                           scorer: str | None = None) -> dict[str, Any]:
    """导入仓库内置样例：默认沿用注册表里的数据集名与评分器（调用方显式传值才覆盖）。"""
    entry = builtin_by_id(dataset_id)
    if entry is None:
        raise BuiltinUnavailable(
            f"unknown builtin dataset: {dataset_id} "
            f"(known: {', '.join(item.id for item in BUILTIN_DATASETS)})")
    return import_direct_llm_split(builtin_source(dataset_id), name=name or entry.id, version=version,
                                   license_id=license_id, scorer=scorer or entry.scorer,
                                   source=f"builtin:{entry.id}", resources=resources)


def resolve_direct_llm_manifest(scenario: dict[str, Any], manifest: dict[str, Any],
                                resources: Any) -> dict[str, Any]:
    """创建期展开：把数据集题目投影成 manifest.cases，并把快照/溯源写进运行快照。

    输出预算与 GSM8K 不同：数据集给默认值 1024，但允许本次运行显式覆盖（``parameters.
    max_output_tokens``）。覆盖值只在快照里生效一次，运行期由 ``resolve.prepare_run`` 统一写进
    provider 配置，因此运行产物仍然自洽可复核。
    """
    from motte_contracts.selection import select_cases

    validate_scenario(scenario)
    name, _, version = str(scenario["dataset"]).rpartition("@")
    dataset = resources.datasets.get(name, version)
    if dataset is None:
        raise ValueError(f"dataset not found: {scenario['dataset']}")
    validate_dataset(dataset)
    if RESERVED_KEYS.intersection(manifest):
        raise ValueError("direct-llm cases, tools and snapshots cannot be overridden")
    if manifest.get("dataset", scenario["dataset"]) != scenario["dataset"]:
        raise ValueError("manifest dataset does not match scenario")
    preset_budget = dataset[EVAL_KEY]["max_output_tokens"]
    budget = (manifest.get("parameters") or {}).get("max_output_tokens")
    if budget is not None and (type(budget) is not int or budget <= 0):
        raise ValueError("parameters.max_output_tokens must be a positive integer")
    run_selection = select_cases(dataset, manifest.get(CASE_SELECTION_KEY))
    selected = set(run_selection.pop("case_ids"))
    run_selection.pop("case_ids_sha256", None)
    resolved = deepcopy(manifest)
    # Direct LLM 的题面原样透传（PROMPT_VERSION=verbatim），不加任何套件后缀。
    resolved["cases"] = {case["case_id"]: {"prompt": case["input"]}
                         for case in dataset["cases"] if case["case_id"] in selected}
    resolved[CASE_SELECTION_KEY] = run_selection
    resolved[SNAPSHOT_KEY] = {"scenario": deepcopy(scenario), "dataset": deepcopy(dataset)}
    resolved[PROVENANCE_KEY] = {
        **deepcopy(dataset[EVAL_KEY]), **deepcopy(dataset["provenance"]),
        "suite": SUITE, "dataset": scenario["dataset"], "cases_sha256": dataset["cases_sha256"],
        "scenario": f"{scenario['name']}@{scenario['version']}",
        "prompt_version": PROMPT_VERSION,
        "max_output_tokens": budget if budget is not None else preset_budget,
        RUN_SELECTION_KEY: run_selection,
    }
    return resolved


def direct_llm_scores(run: dict[str, Any], results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from motte_eval.direct_llm import score_answer_case

    manifest = run["manifest"]
    provenance = manifest[PROVENANCE_KEY]
    if provenance.get("scorer_version") != SCORER_VERSION:
        raise ValueError("unsupported snapshot scorer version")
    dataset = manifest[SNAPSHOT_KEY]["dataset"]
    scorers = effective_scorers(dataset)
    expected = {case["case_id"]: case.get("expected") for case in dataset["cases"]}
    rows = {row["case_id"]: row for row in results}
    scores: list[dict[str, Any]] = []
    for case_id in run["case_ids"]:
        row = rows.get(case_id)
        if row is None or row.get("outcome") == "not_attempted":
            score: dict[str, Any] = {
                "outcome": "not_attempted", "passed": False, "attempted": False,
                "responded": False, "judged": False,
                "scorer": scorers.get(case_id, normalize_scorer(None)),
                "scorer_version": SCORER_VERSION}
        else:
            score = score_answer_case(row.get("result"), expected.get(case_id), scorers.get(case_id))
        scores.append({"case_id": case_id, **score})
    return scores
