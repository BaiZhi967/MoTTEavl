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


#: Terminal-Bench（Harbor）suite 身份：与 motte_sdk.terminalbench 保持一致。
BENCHMARK_ID = "terminal-bench"
SUITE = "terminal-bench-harbor"
SUITE_VERSION = "1"
ADAPTER_ID = "terminal-bench-harbor"
RUNNER_VERSION = "harbor-0.23.0"


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


def _terminal_bench_prepare(
    scenario: dict[str, Any], manifest: dict[str, Any], resources: Any,
) -> dict[str, Any]:
    """Terminal-Bench（Harbor）manifest 已由门面冻结；这里校验身份并补评分身份。"""
    from motte_benchmark.harbor.adapter import ADAPTER_ID
    from motte_benchmark.harbor.parser import PARSER_VERSION

    external = manifest.get("external_benchmark")
    if not isinstance(external, dict) or not external.get("adapter_id"):
        raise ValueError("terminal-bench manifest requires external_benchmark")
    if str(external.get("adapter_id")) != ADAPTER_ID:
        raise ValueError(
            f"terminal-bench manifest adapter_id must be {ADAPTER_ID}, "
            f"got {external.get('adapter_id')!r}",
        )
    plan = (external.get("runner_config") or {}).get("plan") or {}
    if not plan.get("trials"):
        raise ValueError("terminal-bench manifest requires the frozen trial plan")
    task_manifest = manifest.get("task_manifest") or {}
    if not task_manifest.get("task_keys"):
        raise ValueError("terminal-bench manifest requires the prepared task set")
    provenance = manifest.setdefault("benchmark_provenance", {})
    provenance.update({
        "id": provenance.get("id") or BENCHMARK_ID,
        "version": provenance.get("harbor_version") or RUNNER_VERSION,
        "parser_version": PARSER_VERSION,
        "aggregation": provenance.get("aggregation") or "first-trial",
    })
    manifest["evaluation"] = {
        "suite": provenance.get("suite") or SUITE,
        "plugin_version": provenance.get("plugin_version") or SUITE_VERSION,
        "scorer": "harbor-terminal-bench",
        "scorer_version": PARSER_VERSION,
    }
    return manifest


def _terminal_bench_scores(
    run: dict[str, Any], results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Trial 层分数：每个计划 Trial 一行（含无效行，覆盖因此可见）。"""
    from motte_eval.harbor import trial_row

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in results:
        payload = row.get("result") if isinstance(row.get("result"), dict) else {}
        trial_id = str(row.get("trial_id") or payload.get("trial_id") or "")
        if not trial_id or trial_id in seen:
            # 同一 Trial 只能有一行分数：重复行会让质量分母虚高（review R01）。
            continue
        seen.add(trial_id)
        rows.append(trial_row(payload))
    return rows


def _terminal_bench_aggregate(
    run: dict[str, Any], scores: list[dict[str, Any]],
) -> dict[str, Any]:
    """Task/Trial 两层聚合 + 覆盖门禁 + 成本与时长（不重跑任何任务）。"""
    from motte_eval.harbor import (
        cost_summary,
        coverage_gate,
        duration_summary,
        rescore_identity,
        task_aggregate,
    )

    manifest = run.get("manifest") or {}
    provenance = manifest.get("benchmark_provenance") or {}
    external = manifest.get("external_benchmark") or {}
    trials = ((external.get("runner_config") or {}).get("plan") or {}).get("trials") or []
    payloads = _trial_payloads(run)
    planned_per_task: dict[str, int] = {}
    for plan in trials:
        key = str(plan.get("task_key"))
        planned_per_task[key] = planned_per_task.get(key, 0) + 1
    aggregation = str(provenance.get("aggregation") or "first-trial")
    aggregate = task_aggregate(
        trials=payloads, aggregation=aggregation, planned_per_task=planned_per_task,
    )
    aggregate["gate"] = coverage_gate(aggregate)
    aggregate["cost"] = cost_summary(payloads)
    aggregate["durations"] = duration_summary(payloads)
    aggregate["rescore_identity"] = rescore_identity(
        aggregate=aggregate, trials=payloads,
        parser_version=str(provenance.get("parser_version") or "harbor-terminal-bench-parser@1"),
    )
    # 计划外层同样可用的键（报告与前端不依赖内部结构）。
    aggregate["selected_cases"] = aggregate["selected_tasks"]
    aggregate["scored_cases"] = aggregate["scored_tasks"]
    aggregate["coverage"] = aggregate["valid_trial_coverage"]
    aggregate["denominator"] = "planned_trials"
    aggregate["aggregation_policy"] = aggregate["aggregation"]
    return aggregate


def _trial_payloads(run: dict[str, Any]) -> list[dict[str, Any]]:
    """聚合用的 Trial 载荷：优先分派侧注入的 Trial 存储视图。

    ``run["trial_results"]`` 由 ``RunService._append_scoring_pass`` 从 Trial
    存储读出（一个计划 Trial 一条）；``run["cases"]`` 是任务级派生聚合，
    只在兼容旧快照时作为兜底（review R01：两者不能混为一谈）。
    """
    injected = run.get("trial_results")
    if isinstance(injected, list) and injected:
        return [dict(item) for item in injected if isinstance(item, dict)]
    payloads: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in run.get("cases") or []:
        payload = row.get("result") if isinstance(row.get("result"), dict) else {}
        trial_id = str(payload.get("trial_id") or "")
        if not trial_id or trial_id in seen:
            continue
        seen.add(trial_id)
        payloads.append(dict(payload))
    return payloads


def validate_frozen_trial_plans(store: Any, run: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate the complete batch before creating plans or importing any result.

    A matching Trial ID is insufficient: both the frozen plan and any stored
    record must belong to this Run. Prevalidate every unit so a late conflict
    cannot leave earlier units partially created.
    """
    from motte_storage.integrity import RunConflictError
    from motte_storage.trials import validate_plan

    external = (run.get("manifest") or {}).get("external_benchmark") or {}
    raw = ((external.get("runner_config") or {}).get("plan") or {}).get("trials") or []
    plans = [validate_plan(item) for item in raw]
    trials = getattr(store, "trials", None)
    seen: set[str] = set()
    units: set[tuple[str, int]] = set()
    for plan in plans:
        if plan["run_id"] != run.get("id"):
            raise ValueError("frozen trial plan run_id does not match the current Run")
        identity = (plan["task_key"], plan["repeat_index"])
        if plan["trial_id"] in seen or identity in units:
            raise ValueError("frozen trial plan contains a duplicate trial identity")
        seen.add(plan["trial_id"])
        units.add(identity)
        existing = trials.get(plan["trial_id"]) if trials is not None else None
        if existing is not None and (
            existing.get("run_id") != run.get("id") or existing.get("plan") != plan
        ):
            raise RunConflictError(
                f"frozen trial plan conflicts with stored plan: {plan['trial_id']}"
            )
    return plans


def import_terminal_bench_trials(
    store: Any, run: dict[str, Any], results: list[dict[str, Any]],
) -> dict[str, Any]:
    """把采集到的 Trial 结果写入 Trial 存储（先建计划，再逐条幂等落结果）。

    计划来自冻结 manifest（不依赖本次采集），因此"某个 Trial 没跑成"也会
    留下 pending/not_attempted 记录，覆盖统计不会因缺行而虚高。

    review R2-03/R2-09 的两条硬规则：

    - **当前 Run 边界**：目标 Trial 必须属于本 Run 的冻结计划（``run_id`` 与
      冻结计划一致）；未知身份显式拒绝（``unknown_trial`` 也是拒绝），绝不
      写到别的 Run 名下；
    - **逐条隔离**：单条非法 payload 只影响它自己，合法兄弟结果继续落库；
      返回完整 ``accepted`` / ``invalid`` 清单，调用方的派生视图只消费已接受
      的记录。
    """
    plan = validate_frozen_trial_plans(store, run)
    trials = getattr(store, "trials", None)
    run_id = str(run.get("id") or "")
    if trials is None or not plan:
        return {
            "created": 0, "stored": 0, "identical": 0, "conflicts": 0, "skipped": True,
            "accepted": [], "invalid": [],
        }
    created = trials.create_plans([dict(item) for item in plan])
    statuses: dict[str, int] = {}
    for item in created:
        statuses[str(item["status"])] = statuses.get(str(item["status"]), 0) + 1
    conflicts: list[dict[str, Any]] = [
        item for item in created if item["status"] == "conflict"
    ]
    #: 本 Run 冻结计划里的 Trial 身份（跨 Run 写入的唯一合法目标集合）。
    owned: dict[str, dict[str, Any]] = {
        str(item["trial_id"]): dict(item) for item in plan if item.get("trial_id")
    }
    stored = 0
    identical = 0
    replaced: list[dict[str, Any]] = []
    accepted: list[str] = []
    invalid: list[dict[str, Any]] = []
    for row in results:
        payload = row.get("result") if isinstance(row.get("result"), dict) else None
        if not payload:
            continue
        from motte_contracts.trial import canonical_hash

        # 目标 Trial 身份取**记录**（冻结计划的 trial_id），不取 payload 自称的
        # 身份：错配必须由存储的身份校验拒绝，而不是写到另一个 Trial 名下。
        target = str(row.get("trial_id") or payload.get("trial_id") or "")
        if not target:
            invalid.append({
                "trial_id": None,
                "code": "TRIAL_IDENTITY_MISSING",
                "message": "trial payload carries no trial identity",
            })
            continue
        frozen = owned.get(target)
        if frozen is None:
            # 不属于本 Run 的冻结计划：显式拒绝，绝不落到别的 Run 名下（R2-03）。
            invalid.append({
                "trial_id": target,
                "code": "TRIAL_NOT_IN_FROZEN_PLAN",
                "message": (
                    "trial does not belong to this run's frozen plan; the payload is "
                    "rejected and no other run's evidence is touched"
                ),
            })
            continue
        try:
            outcome = trials.put_result(
                target, payload,
                source_hash=canonical_hash(payload),
                parser_version=str(
                    payload.get("parser_version") or "harbor-terminal-bench-parser@1"
                ),
            )
        except Exception as error:  # noqa: BLE001 - 逐条隔离，兄弟结果继续落库
            invalid.append({
                "trial_id": target,
                "code": "TRIAL_RESULT_REJECTED",
                "error_type": type(error).__name__,
                "message": str(error),
            })
            continue
        status = str(outcome.get("status"))
        if status == "stored":
            stored += 1
        elif status == "identical":
            identical += 1
        elif status == "replaced_placeholder":
            # 平台占位处置被真实冻结结果替换（review R2-01）：算已接受，
            # 但替换审计必须保留。
            stored += 1
            replaced.append({
                "trial_id": target,
                "previous": outcome.get("previous"),
            })
        elif status == "conflict":
            conflicts.append(outcome)
        else:
            # unknown_trial / 其他未预期状态都不能静默忽略（R2-03）。
            invalid.append({
                "trial_id": target,
                "code": "TRIAL_RESULT_NOT_STORED",
                "message": f"trial store returned {status!r} for a planned trial",
            })
            continue
        accepted.append(target)
    return {
        "created": statuses.get("created", 0),
        "identical_plans": statuses.get("identical", 0),
        "stored": stored,
        "identical": identical,
        "conflicts": conflicts,
        "replaced_placeholders": replaced,
        "accepted": accepted,
        "invalid": invalid,
        "planned": len(plan),
        "run_id": run_id,
        "skipped": False,
    }


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

    def _external_mcq_prepare(benchmark_id: str, adapter_id: str):
        def prepare(
            scenario: dict[str, Any], manifest: dict[str, Any], resources: Any,
        ) -> dict[str, Any]:
            # job-based 套件：manifest 在创建时已冻结（external_benchmark 钉版本
            # + runner_config + case_expectations）；这里补齐评分身份。
            external = manifest.get("external_benchmark")
            if not isinstance(external, dict) or not external.get("adapter_id"):
                raise ValueError(
                    f"{benchmark_id}-external manifest requires external_benchmark"
                )
            if str(external.get("adapter_id")) != adapter_id:
                raise ValueError(
                    "external_benchmark.adapter_id mismatch: "
                    f"{external.get('adapter_id')!r} != {adapter_id!r}"
                )
            expectations = manifest.get("case_expectations")
            if not isinstance(expectations, dict) or not expectations:
                raise ValueError(
                    f"{benchmark_id}-external manifest requires frozen case_expectations"
                )
            from motte_sdk.benchmark_catalog import (
                EXTERNAL_MCQ_SCORER,
                EXTERNAL_MCQ_SCORER_VERSION,
                benchmark_descriptor,
            )

            descriptor = benchmark_descriptor(benchmark_id)
            provenance = {
                "id": descriptor.benchmark_id,
                "version": descriptor.benchmark_version,
                "dataset_revision": str(external.get("dataset_revision") or ""),
                "selected_count": len(expectations),
                "scorer": EXTERNAL_MCQ_SCORER,
                "scorer_version": EXTERNAL_MCQ_SCORER_VERSION,
                "parser_version": descriptor.parser_version,
            }
            manifest["benchmark_provenance"] = provenance
            return manifest

        return prepare

    def _external_mcq_scores(
        run: dict[str, Any], results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        # gold 以冻结的 case_expectations 为准（review R03）；Runner 报告的
        # gold 只作对账证据，出现分歧记进 details 不改变判定。未尝试的行
        # 不是记录（保持 NOT_ATTEMPTED，不伪装成答错）。
        records = []
        for row in results:
            if row.get("outcome") == "not_attempted":
                continue
            payload = row.get("result") if isinstance(row.get("result"), dict) else {}
            records.append({
                "case_id": row.get("case_id"),
                "prediction": payload.get("prediction"),
                "runner_gold": payload.get("gold"),
            })
        by_case = {record["case_id"]: record for record in records}
        expectations = (run.get("manifest") or {}).get("case_expectations") or {}
        scores = []
        for case_id in run.get("case_ids") or []:
            record = by_case.get(case_id)
            gold = expectations.get(case_id)
            if record is None:
                scores.append({"case_id": case_id, "passed": None,
                               "details": {"code": "NOT_ATTEMPTED"}})
                continue
            prediction = str(record.get("prediction") or "").strip()
            if gold is None or str(gold).strip() == "":
                scores.append({"case_id": case_id, "passed": None,
                               "details": {"code": "NO_EXPECTATION"}})
                continue
            runner_gold = record.get("runner_gold")
            details = {}
            if (
                isinstance(runner_gold, str) and runner_gold.strip()
                and runner_gold.strip().upper() != str(gold).strip().upper()
            ):
                details["gold_source_mismatch"] = {
                    "frozen": gold, "runner_reported": runner_gold,
                    "authority": "case_expectations",
                }
            scores.append({
                "case_id": case_id,
                "passed": bool(prediction and prediction.upper() == str(gold).strip().upper()),
                **({"details": details} if details else {}),
            })
        return scores

    def _external_mcq_aggregate(
        run: dict[str, Any], scores: list[dict[str, Any]],
    ) -> dict[str, Any]:
        # gold 以冻结 case_expectations 为准（与样本级评分同一权威来源，
        # review R03）；退出码优先取分派侧传入的外部结局上下文。
        manifest = run.get("manifest") or {}
        expectations = manifest.get("case_expectations") or {}
        records = []
        for row in run.get("cases") or []:
            if row.get("outcome") == "not_attempted":
                continue
            payload = row.get("result") if isinstance(row.get("result"), dict) else {}
            case_id = row.get("case_id")
            records.append({
                "case_id": case_id,
                "prediction": payload.get("prediction"),
                "gold": expectations.get(case_id, payload.get("gold")),
            })
        outcome = run.get("external_outcome") or {}
        exit_code = outcome.get("runner_exit_code")
        if exit_code is None:
            error = manifest.get("error") or {}
            exit_code = (error.get("details") or {}).get("exit_code") if isinstance(
                error, dict,
            ) else None
        if exit_code is None:
            exit_code = (manifest.get("external_benchmark") or {}).get("runner_exit_code")
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
            "runner_exit_code": exit_code,
            "denominator": "selected_cases",
        }

    for benchmark_id, suite_id, adapter_id in (
        ("ceval", "ceval-external", "ceval-opencompass"),
        ("cmmlu", "cmmlu-external", "cmmlu-opencompass"),
    ):
        register_benchmark_plugin(
            BenchmarkPlugin(
                suite_id=suite_id,
                contract_version="1",
                prepare_manifest=_external_mcq_prepare(benchmark_id, adapter_id),
                score=_external_mcq_scores,
                aggregate=_external_mcq_aggregate,
                adapter_id=adapter_id,
                adapter_version="1",
            )
        )

    register_benchmark_plugin(
        BenchmarkPlugin(
            suite_id=SUITE,
            contract_version=SUITE_VERSION,
            prepare_manifest=_terminal_bench_prepare,
            score=_terminal_bench_scores,
            aggregate=_terminal_bench_aggregate,
            adapter_id=ADAPTER_ID,
            adapter_version="1",
        )
    )
