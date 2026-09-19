"""Benchmark plugin registry for preparation, scoring, and aggregation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BenchmarkPlugin:
    suite_id: str
    contract_version: str
    prepare_manifest: Callable[[dict[str, Any], dict[str, Any], Any], dict[str, Any]]
    score: Callable[[dict[str, Any], list[dict[str, Any]]], list[dict[str, Any]]]
    aggregate: Callable[[dict[str, Any], list[dict[str, Any]]], dict[str, Any]]
    adapter_id: str
    adapter_version: str


_PLUGINS: dict[tuple[str, str], BenchmarkPlugin] = {}
_BUILTINS_LOADED = False


def register_benchmark_plugin(plugin: BenchmarkPlugin) -> BenchmarkPlugin:
    key = (plugin.suite_id, plugin.contract_version)
    if key in _PLUGINS:
        raise ValueError(
            f"benchmark plugin already registered: {plugin.suite_id}@{plugin.contract_version}"
        )
    _PLUGINS[key] = plugin
    return plugin


def unregister_benchmark_plugin(suite_id: str, contract_version: str) -> None:
    _PLUGINS.pop((suite_id, contract_version), None)


def registered_benchmark_plugins() -> tuple[BenchmarkPlugin, ...]:
    _load_builtins()
    return tuple(sorted(_PLUGINS.values(), key=lambda item: (item.suite_id, item.contract_version)))


def benchmark_plugin(suite_id: str, contract_version: str = "1") -> BenchmarkPlugin:
    _load_builtins()
    try:
        return _PLUGINS[(suite_id, contract_version)]
    except KeyError:
        known = ", ".join(f"{p.suite_id}@{p.contract_version}" for p in _PLUGINS.values())
        raise ValueError(
            f"unsupported benchmark plugin: {suite_id}@{contract_version} (known: {known})"
        ) from None


def plugin_for_scenario(scenario: dict[str, Any] | None) -> tuple[str, str] | None:
    if not isinstance(scenario, dict):
        return None
    from motte_contracts import suites as contract_suites

    identity = contract_suites.validated_scenario_identity(scenario)
    if identity is None:
        explicit = scenario.get("suite")
        if not isinstance(explicit, str) or not explicit:
            return None
        if explicit in contract_suites.SUITES:
            raise ValueError(f"invalid managed benchmark scenario: {explicit}")
        suite = explicit
        if "plugin_version" in scenario:
            marker = scenario["plugin_version"]
            if not isinstance(marker, str) or not marker:
                raise ValueError("explicit plugin_version must be a non-empty string")
            version = marker
        else:
            version = "1"
    else:
        suite, version = identity
    benchmark_plugin(suite, version)
    return suite, version


def suite_for_run(run: dict[str, Any]) -> tuple[str, str] | None:
    provenance = (run.get("manifest") or {}).get("benchmark_provenance")
    if not isinstance(provenance, dict):
        return None
    if "suite" not in provenance:
        # The only persisted managed shape before suite discrimination was GSM8K.
        return "gsm8k", "1"
    suite = provenance.get("suite")
    version = str(provenance.get("plugin_version") or "1")
    if not isinstance(suite, str) or not suite:
        raise ValueError("benchmark provenance has an invalid explicit suite")
    benchmark_plugin(suite, version)
    return suite, version


def prepare_with_plugin(
    suite_id: str,
    scenario: dict[str, Any],
    manifest: dict[str, Any],
    resources: Any,
    *,
    contract_version: str = "1",
) -> dict[str, Any]:
    plugin = benchmark_plugin(suite_id, contract_version)
    resolved = plugin.prepare_manifest(scenario, manifest, resources)
    provenance = resolved.get("benchmark_provenance")
    if not isinstance(provenance, dict):
        raise ValueError(f"benchmark plugin {suite_id} did not produce provenance")
    provenance["suite"] = plugin.suite_id
    provenance["plugin_version"] = plugin.contract_version
    resolved["evaluation"] = evaluation_descriptor(plugin, provenance)
    return resolved


def evaluation_descriptor(plugin: BenchmarkPlugin, provenance: dict[str, Any]) -> dict[str, Any]:
    scorer_version = str(provenance.get("scorer_version") or "unknown")
    return {
        "benchmark_id": str(provenance.get("id") or plugin.suite_id),
        "benchmark_version": str(provenance.get("version") or "1"),
        "adapter_id": plugin.adapter_id,
        "adapter_version": plugin.adapter_version,
        "scorer_id": str(provenance.get("scorer") or scorer_version),
        "scorer_version": scorer_version,
    }


def score_with_plugin(run: dict[str, Any], results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    identity = suite_for_run(run)
    if identity is None:
        raise ValueError("run has no benchmark provenance")
    return benchmark_plugin(*identity).score(run, results)


def aggregate_with_plugin(run: dict[str, Any], scores: list[dict[str, Any]]) -> dict[str, Any]:
    identity = suite_for_run(run)
    if identity is None:
        raise ValueError("run has no benchmark provenance")
    return benchmark_plugin(*identity).aggregate(run, scores)


def _load_builtins() -> None:
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    _BUILTINS_LOADED = True

    from motte_contracts.direct_llm import SUITE as DIRECT_LLM_SUITE
    from motte_contracts.gsm8k import SUITE as GSM8K_SUITE
    from motte_contracts.selection import run_selected_count
    from motte_eval.direct_llm import aggregate_answers
    from motte_eval.gsm8k import aggregate_benchmark
    from motte_sdk.benchmark import benchmark_scores, resolve_benchmark_manifest
    from motte_sdk.direct_llm import direct_llm_scores, resolve_direct_llm_manifest
    from motte_sdk.direct_llm_v2 import (
        aggregate_direct_llm_v2,
        direct_llm_v2_scores,
        resolve_direct_llm_v2_manifest,
    )

    def selected_count(run: dict[str, Any]) -> int:
        provenance = run["manifest"]["benchmark_provenance"]
        selected = run_selected_count(provenance)
        if selected is None:
            raise ValueError("run snapshot has no selected case count")
        return selected

    register_benchmark_plugin(
        BenchmarkPlugin(
            suite_id=GSM8K_SUITE,
            contract_version="1",
            prepare_manifest=resolve_benchmark_manifest,
            score=benchmark_scores,
            aggregate=lambda run, scores: {
                **aggregate_benchmark(scores, selected_count(run)),
                "denominator": "selected_cases",
            },
            adapter_id="gsm8k-official-jsonl",
            adapter_version="1",
        )
    )
    register_benchmark_plugin(
        BenchmarkPlugin(
            suite_id=DIRECT_LLM_SUITE,
            contract_version="1",
            prepare_manifest=resolve_direct_llm_manifest,
            score=direct_llm_scores,
            aggregate=lambda run, scores: {
                **aggregate_answers(scores, selected_count(run)),
                "denominator": "judged_cases",
            },
            adapter_id="direct-llm-jsonl",
            adapter_version="1",
        )
    )
    from motte_contracts.agent_tasks import SUITE as AGENT_TASKS_SUITE
    from motte_sdk.agent_tasks import (
        aggregate_agent_tasks,
        agent_tasks_scores,
        resolve_agent_tasks_manifest,
    )

    register_benchmark_plugin(
        BenchmarkPlugin(
            suite_id=AGENT_TASKS_SUITE,
            contract_version="1",
            prepare_manifest=resolve_agent_tasks_manifest,
            score=agent_tasks_scores,
            aggregate=aggregate_agent_tasks,
            adapter_id="builtin-agent-file-tasks",
            adapter_version="1",
        )
    )
    register_benchmark_plugin(
        BenchmarkPlugin(
            suite_id=DIRECT_LLM_SUITE,
            contract_version="2",
            prepare_manifest=resolve_direct_llm_v2_manifest,
            score=direct_llm_v2_scores,
            aggregate=aggregate_direct_llm_v2,
            adapter_id="direct-llm-jsonl",
            adapter_version="2",
        )
    )

    from motte_eval.ceval import diagnostic_metrics as ceval_diagnostic_metrics

    def _ceval_external_manifest(
        manifest: dict[str, Any], requested: dict[str, Any], resources: Any,
    ) -> dict[str, Any]:
        # job-based 套件的 manifest 在创建时已冻结（external_benchmark 钉版本）。
        external = manifest.get("external_benchmark")
        if not isinstance(external, dict) or not external.get("adapter_id"):
            raise ValueError("ceval-external manifest requires external_benchmark")
        return manifest

    def _ceval_external_scores(
        run: dict[str, Any], results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        records = []
        for row in results:
            payload = row.get("result") if isinstance(row.get("result"), dict) else {}
            records.append({
                "case_id": row.get("case_id"),
                "prediction": payload.get("prediction"),
                "gold": payload.get("gold"),
            })
        by_case = {record["case_id"]: record for record in records}
        scores = []
        for case_id in run.get("case_ids") or []:
            record = by_case.get(case_id)
            if record is None:
                scores.append({"case_id": case_id, "passed": None,
                               "details": {"code": "NOT_ATTEMPTED"}})
                continue
            prediction = str(record.get("prediction") or "").strip()
            gold = record.get("gold")
            if gold is None or str(gold).strip() == "":
                scores.append({"case_id": case_id, "passed": None,
                               "details": {"code": "NO_EXPECTATION"}})
                continue
            scores.append({
                "case_id": case_id,
                "passed": bool(prediction and prediction.upper() == str(gold).strip().upper()),
            })
        return scores

    def _ceval_external_aggregate(
        run: dict[str, Any], scores: list[dict[str, Any]],
    ) -> dict[str, Any]:
        records = []
        for row in run.get("cases") or []:
            payload = row.get("result") if isinstance(row.get("result"), dict) else {}
            records.append({
                "case_id": row.get("case_id"),
                "prediction": payload.get("prediction"),
                "gold": payload.get("gold"),
            })
        exit_code = ((run.get("manifest") or {}).get("external_benchmark") or {}).get(
            "runner_exit_code",
        )
        metrics = ceval_diagnostic_metrics(
            selected_case_ids=run.get("case_ids") or [],
            records=records,
            runner_exit_code=exit_code,
        )
        return {
            "selected": metrics["selected"],
            "attempted": metrics["attempted"],
            "correct": metrics["correct"],
            "wrong": metrics["wrong"],
            "not_attempted": metrics["not_attempted"],
            "accuracy": metrics["diagnostic.selected_case_accuracy"],
            "coverage": metrics["diagnostic.observed_call_coverage"],
            "unscored": metrics["unscored"],
            "denominator": "selected_cases",
        }

    register_benchmark_plugin(
        BenchmarkPlugin(
            suite_id="ceval-external",
            contract_version="1",
            prepare_manifest=_ceval_external_manifest,
            score=_ceval_external_scores,
            aggregate=_ceval_external_aggregate,
            adapter_id="ceval-opencompass",
            adapter_version="1",
        )
    )
