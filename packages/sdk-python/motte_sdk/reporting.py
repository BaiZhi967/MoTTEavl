"""Canonical run report assembly shared by API and local CLI."""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from motte_contracts.compat import adapt_legacy_report, adapt_legacy_run
from motte_contracts.report import ReportSummary
from motte_trace.redaction import redact_secrets


def _redact_run(run: dict[str, Any]) -> dict[str, Any]:
    view = deepcopy(run)
    for case in view.get("cases") or []:
        result = case.get("result")
        if not isinstance(result, dict):
            continue
        agent = result.get("agent")
        if isinstance(agent, dict):
            agent["final_output"] = redact_secrets(agent.get("final_output"))
        observation = result.get("observation")
        if isinstance(observation, dict):
            observation["final_output"] = redact_secrets(observation.get("final_output"))
        result["events"] = redact_secrets(result.get("events") or [])
    return view


def build_public_run_report(run: dict[str, Any]) -> dict[str, Any]:
    """Build the exact redacted/compatible report exposed by API and local CLI."""
    return adapt_legacy_report(build_run_report(adapt_legacy_run(_redact_run(run))))


def build_run_report(run: dict[str, Any]) -> dict[str, Any]:
    cases = run.get("cases", [])
    scores = run.get("scores", [])
    benchmark = run.get("manifest", {}).get("benchmark_provenance")
    costs = [
        case["result"]["cost"]
        for case in cases
        if isinstance(case.get("result"), dict)
        and isinstance(case["result"].get("cost"), dict)
    ]
    known_costs = [cost["total"] for cost in costs if cost.get("total") is not None]
    total_cost = sum(known_costs)
    versions = sorted(
        {cost["price_table_version"] for cost in costs if cost.get("price_table_version")}
    )
    scoring_pass_id = run.get("current_scoring_pass_id")
    passed = sum(1 for score in scores if score.get("passed") is True)
    failed = sum(1 for score in scores if score.get("passed") is False)
    judged = passed + failed
    report = {
        "schema_version": 2 if scoring_pass_id else 1,
        "run_id": run["id"],
        "scenario_version": run.get("scenario_version"),
        "status": run["status"],
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": {
            "cases": len(cases),
            "scored": len(scores),
            "passed": passed,
            "failed": failed,
            "pass_rate": round(passed / judged, 4) if judged else None,
            **({"unjudged": len(scores) - judged} if len(scores) != judged else {}),
        },
        "cost": {
            "total": round(total_cost, 8) if known_costs else None,
            "price_table_versions": versions,
        },
        "scoring_pass_id": scoring_pass_id,
        "scores": scores,
        "cases": [
            {"case_id": case["case_id"], "result": case.get("result")} for case in cases
        ],
    }
    if benchmark:
        report["benchmark"] = benchmark
        scoring_pass = run.get("scoring_pass") or {}
        pass_summary = scoring_pass.get("summary") or {}
        aggregate = pass_summary.get("aggregate")
        if not isinstance(aggregate, dict):
            aggregate = {
                key: value
                for key, value in pass_summary.items()
                if key not in {"scores", "passed", "aggregate"}
            }
        report["summary"].update(
            {
                key: value
                for key, value in aggregate.items()
                if key in ReportSummary.model_fields and key != "aggregate"
            }
        )
        report["summary"]["aggregate"] = aggregate
        selected = aggregate.get("selected")
        responded = aggregate.get("responded")
        correct = aggregate.get("correct")
        attempted = aggregate.get("attempted")
        accuracy = aggregate.get("accuracy")
        if isinstance(selected, int):
            report["summary"]["cases"] = selected
        if isinstance(responded, int):
            report["summary"]["scored"] = responded
        if isinstance(correct, int):
            report["summary"]["passed"] = correct
            if benchmark.get("plugin_version") == "2":
                aggregate_judged = aggregate.get("judged")
                if isinstance(selected, int) and isinstance(aggregate_judged, int):
                    report["summary"]["failed"] = aggregate_judged - correct
                    report["summary"]["unjudged"] = selected - aggregate_judged
            elif isinstance(selected, int):
                report["summary"]["failed"] = selected - correct
        if isinstance(accuracy, (int, float)):
            report["summary"]["pass_rate"] = accuracy
        report["cost"].update(
            known_cases=len(known_costs),
            unknown_cases=(attempted - len(known_costs)) if isinstance(attempted, int) else None,
        )
        usage = [
            case["result"].get("usage", {})
            for case in cases
            if isinstance(case.get("result"), dict)
        ]
        report["usage"] = {
            key: sum(item[key] for item in usage if key in item)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if any(key in item for item in usage)
        }
    return report
