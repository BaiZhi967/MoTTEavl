"""Offline benchmark import and immutable preparation shared by API/CLI."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from motte_contracts.gsm8k import build_prompt, validate_dataset, validate_scenario


def resolve_benchmark_manifest(scenario: dict[str, Any], manifest: dict[str, Any], resources: Any) -> dict[str, Any]:
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
    resolved = deepcopy(manifest)
    resolved["cases"] = {case["case_id"]: {"prompt": build_prompt(case["input"])} for case in dataset["cases"]}
    resolved["benchmark_snapshot"] = {"scenario": deepcopy(scenario), "dataset": deepcopy(dataset)}
    resolved["benchmark_provenance"] = {
        **deepcopy(dataset["benchmark"]), **deepcopy(dataset["provenance"]),
        "dataset": scenario["dataset"], "cases_sha256": dataset["cases_sha256"],
        "scenario": f"{scenario['name']}@{scenario['version']}",
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
