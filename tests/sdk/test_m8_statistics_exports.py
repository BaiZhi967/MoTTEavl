"""M8: frozen subject-metering descriptors and honest statistical exports."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from copy import deepcopy

import pytest

from motte_contracts.hashing import canonical_hash
from motte_sdk import export
from motte_sdk.comparisons import ComparisonService
from motte_storage.run_store import InMemoryRunStore

def _append_pass(store, run_id, pass_id, case_ids, *, complete=True):
    store.scoring_passes.append({
        "id": pass_id, "run_id": run_id, "scorer_id": "deterministic",
        "scorer_version": "1", "created_at": "2026-09-30T00:00:00+00:00",
        "source": "initial", "source_run_revision": 1,
        "summary": {"judge_cost": {"total": 500, "currency": "USD"}},
    }, [{"case_id": case_id, "metric_id": "accuracy", "passed": True,
         "value": 1.0, "denominator": True, "metric_status": "scored",
         "evaluator_id": "deterministic", "evaluator_version": "1"}
        for case_id in (case_ids if complete else [])])


def _fixture(results, *, terminal=False):
    store = InMemoryRunStore()
    case_ids = [f"c{i}" for i in range(len(results))]
    for run_id in ("base", "candidate"):
        manifest = {"evaluation": {"scorer_id": "deterministic", "scorer_version": "1"}}
        if terminal:
            manifest["benchmark_provenance"] = {"suite": "terminal-bench-harbor"}
        store.runs.create({
            "id": run_id, "schema_version": 2, "revision": 1,
            "scenario_version": "test@1", "status": "completed",
            "manifest": manifest, "requested_manifest": {}, "case_ids": case_ids,
            "created_at": "2026-09-30T00:00:00+00:00",
            "updated_at": "2026-09-30T00:00:00+00:00",
        }, event={"run_id": run_id, "type": "completed", "status": "completed"})
        _append_pass(store, run_id, f"old-{run_id}", case_ids)
        for case_id, result in zip(case_ids, results, strict=True):
            if result is not None:
                store.case_runs.upsert({
                    "run_id": run_id, "case_id": case_id, "result": deepcopy(result),
                })
    return store, ComparisonService(store)


def _fixed(service, *, k=1):
    return service.paired_statistics(
        "base", "candidate", allowed_factors=[], baseline_pass_id="old-base",
        candidate_pass_id="old-candidate", k=k,
    )


def test_subject_cost_distribution_separates_currencies_and_unknowns():
    store, service = _fixture([
        {"cost": {"total": 1.0, "currency": "USD"},
         "judge": {"cost": {"total": 999, "currency": "USD"}}},
        {"cost": {"total": 3.0, "currency": "USD"}},
        {"cost": {"total": 7.0, "currency": "CNY"}},
        {"cost": {"total": 8.0}},
        {"cost": {"total": None, "currency": "USD"}},
        None,
    ])
    # A row outside the fixed selected cases must never enlarge the distribution.
    store.case_runs.upsert({"run_id": "base", "case_id": "unselected",
                            "result": {"cost": {"total": 1000, "currency": "USD"}}})
    view = _fixed(service)
    cost = view["descriptive"]["baseline"]["cost"]
    assert cost["scope"] == "subject"
    assert cost["unit"] == "case"
    assert cost["known_cost_count"] == 3
    assert cost["unknown_cost_count"] == 3
    assert cost["unknown_currency_count"] == 2
    assert cost["by_currency"]["USD"]["count"] == 2
    assert cost["by_currency"]["USD"]["missing"] == 1
    assert cost["by_currency"]["USD"]["mean"] == 2.0
    assert cost["by_currency"]["USD"]["p90"] == pytest.approx(2.8)
    assert cost["by_currency"]["CNY"]["mean"] == 7.0
    assert "total" not in cost and "total_usd" not in cost
    assert len(view["inputs"]["subject_case_metrics"]["baseline"]) == 6


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), -float("inf"), -1, True, "12"])
def test_invalid_cost_and_latency_stay_missing_in_strict_json(bad):
    _, service = _fixture([
        {"cost": {"total": bad, "currency": "USD"}, "metering": {"latency_ms": bad}},
        {"cost": {"total": 0, "currency": "USD"}, "metering": {"latency_ms": 0}},
    ])
    view = _fixed(service)
    baseline = view["descriptive"]["baseline"]
    assert baseline["cost"]["known_cost_count"] == 1
    assert baseline["cost"]["unknown_cost_count"] == 1
    assert baseline["cost"]["by_currency"]["USD"]["missing"] == 1
    assert baseline["latency_ms"]["count"] == 1
    assert baseline["latency_ms"]["missing"] == 1
    assert baseline["latency_ms"]["mean_ms"] == 0
    assert view["input_digest"] == canonical_hash(view["inputs"])
    assert json.loads(json.dumps(view, allow_nan=False)) == view


def test_latency_is_selected_subject_case_measurement_not_judge_or_attempt_samples():
    _, service = _fixture([
        {"metering": {"latency_ms": 10, "attempts": 8},
         "judge": {"metering": {"latency_ms": 10000}}},
        {"metering": {"latency_ms": 30, "attempts": 1}}, None,
    ])
    latency = _fixed(service)["descriptive"]["baseline"]["latency_ms"]
    assert latency == {
        "scope": "subject", "unit": "case", "source": "result.metering.latency_ms",
        "p50_ms": 20.0, "p90_ms": 28.0, "mean_ms": 20.0, "count": 2, "missing": 1,
    }


def test_terminal_task_descriptor_does_not_invent_trial_metering():
    _, service = _fixture([{}, None], terminal=True)
    view = _fixed(service, k=2)
    assert view["k"] == 2
    assert view["descriptive"]["baseline"]["cost"]["unit"] == "task"
    assert view["descriptive"]["baseline"]["latency_ms"]["unit"] == "task"
    assert view["descriptive"]["baseline"]["latency_ms"]["missing"] == 2
    assert view["reason"] == "missing_or_invalid_trial"


def test_fixed_current_drift_and_metering_digest_capture_independent_inputs():
    store, service = _fixture([
        {"cost": {"total": 1, "currency": "USD"}, "metering": {"latency_ms": 10}},
        {"cost": {"total": 2, "currency": "USD"}, "metering": {"latency_ms": 20}},
    ])
    first = _fixed(service)
    frozen = deepcopy(first)
    _append_pass(store, "base", "new-base", ["c0", "c1"], complete=False)
    assert _fixed(service) == first
    # Simulate evidence loss/reimport. ReportRef does not cover case metering;
    # the separate digest must detect it and the already returned report is stable.
    store.case_runs._rows[("base", "c0")][1]["result"]["metering"]["latency_ms"] = 90
    changed = _fixed(service)
    assert changed["refs"] == first["refs"]
    assert changed["input_digest"] != first["input_digest"]
    assert first == frozen
    assert first["inputs"]["subject_case_metrics"]["baseline"][0]["metering"]["latency_ms"] == 10


def test_statistical_junit_records_fixed_inputs_and_skips_insufficient_tasks():
    _, service = _fixture([{"metering": {"latency_ms": 3}}])
    result = _fixed(service, k=3)
    assert result["reason"] == "insufficient_tasks"
    assert result["statistics"]["interval"]["applicable"] is False
    canonical = export.statistics_to_json(result)
    assert canonical == result
    root = ET.fromstring(export.statistics_to_junit(result))
    assert root.attrib["tests"] == "1"
    assert root.attrib["skipped"] == "1"
    assert root.attrib["failures"] == root.attrib["errors"] == "0"
    case = root.find("testcase")
    assert case is not None and case.find("skipped") is not None
    assert case.find("skipped").attrib["message"] == "insufficient_tasks"
    props = {p.attrib["name"]: p.attrib["value"] for p in root.findall("properties/property")}
    for name in ("input_digest", "policy_ref", "policy_hash", "unit", "missing_policy"):
        assert props[name] == str(result[name])
    for name in ("k", "seed", "iterations", "n_selected", "n_pairs", "missing_pairs"):
        assert props[name] == str(result[name])
    assert json.loads(props["refs"]) == result["refs"]
    assert json.loads(root.find("system-out").text) == result


def test_statistical_junit_qualification_never_asserts_quality_pass():
    _, service = _fixture([{}, {}])
    result = _fixed(service)
    root = ET.fromstring(export.statistics_to_junit(result))
    assert root.attrib["skipped"] == "0"
    assert root.find("testcase").attrib["name"] == "paired_interval_applicability"
    props = {p.attrib["name"]: p.attrib["value"] for p in root.findall("properties/property")}
    assert props["interpretation"] == "descriptive_statistics_not_quality_gate"
    assert json.loads(root.find("system-out").text) == result


def test_explicit_judge_cost_never_enters_subject_distribution():
    _, service = _fixture([
        {"cost": {"total": 99, "currency": "USD", "scope": "judge"}},
        {"cost": {"total": 1, "currency": "USD", "scope": "subject"}},
    ])
    cost = _fixed(service)["descriptive"]["baseline"]["cost"]
    assert cost["by_currency"]["USD"]["count"] == 1
    assert cost["by_currency"]["USD"]["mean"] == 1
    assert cost["unknown_cost_count"] == 1


@pytest.mark.parametrize("currency", [None, "", "   ", 123])
def test_unknown_currency_does_not_become_usd_or_zero(currency):
    _, service = _fixture([{"cost": {"total": 5, "currency": currency}}])
    cost = _fixed(service)["descriptive"]["baseline"]["cost"]
    assert cost["by_currency"] == {}
    assert cost["known_cost_count"] == 0
    assert cost["unknown_cost_count"] == cost["unknown_currency_count"] == 1


def test_exports_are_detached_and_never_reread_metering_or_current():
    store, service = _fixture([{}, {}])
    view = _fixed(service)
    original = deepcopy(view)
    encoded = export.statistics_to_json(view)
    xml = export.statistics_to_junit(view)
    encoded["inputs"]["subject_case_metrics"]["baseline"][0]["cost"] = {"total": 99}
    _append_pass(store, "base", "new-base", ["c0", "c1"], complete=False)
    assert view == original
    assert export.statistics_to_junit(view) == xml


def test_nonfinite_observations_keep_distinct_input_identity_without_json_nan():
    digests = set()
    for value in (None, float("nan"), float("inf"), -float("inf")):
        _, service = _fixture([{"metering": {"latency_ms": value}}])
        result = _fixed(service)
        digests.add(result["input_digest"])
        json.dumps(result, allow_nan=False)
    assert len(digests) == 4
