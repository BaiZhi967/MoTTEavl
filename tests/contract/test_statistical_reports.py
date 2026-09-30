"""Immutable statistical report identity and self-contained stored integrity."""
from copy import deepcopy
import json
from typing import Any

import pytest
from pydantic import ValidationError

from motte_contracts.hashing import canonical_hash
from motte_contracts.statistical_reports import (
    StatisticalReport,
    StatisticalReportPublishRequest,
    statistical_report_id,
    validate_statistical_report,
)


POLICY = {
    "policy_id": "statistical_policy@1",
    "unit": "task",
    "confidence": 0.95,
    "interval_method": "percentile_bootstrap",
    "bootstrap_iterations": 2000,
    "bootstrap_seed": 20260921,
    "quantile_interpolation": "linear",
    "binary_interval": "normal_approximation",
    "implementation_version": "motte_eval.statistics@1",
    "missing_policy": "keep_visible",
}
REQUIRED_RESULT_FIELDS = (
    "refs", "inputs", "input_digest", "k", "descriptive", "unit", "method",
    "policy_ref", "policy_hash", "implementation_version", "seed", "iterations",
    "missing_policy", "n_selected", "n_pairs", "missing_pairs", "applicable",
    "reason", "statistics",
)


def _body() -> dict[str, Any]:
    refs = {
        side: {
            "run_id": f"run-{side}", "scoring_pass_id": f"pass-{side}",
            "report_schema": "run_report@2", "evidence_hash": f"sha256:{side}",
        }
        for side in ("baseline", "candidate")
    }
    inputs = {
        "refs": deepcopy(refs), "allowed_factors": ["model"], "k": 1,
        "subject_case_metrics": {
            side: [{"case_id": "case-1", "result_present": True,
                    "cost": {"total": 1.0, "currency": "USD"},
                    "metering": {"latency_ms": 10.0}}]
            for side in refs
        },
    }
    result = {
        "refs": refs, "inputs": inputs, "input_digest": canonical_hash(inputs), "k": 1,
        "descriptive": {"baseline": {}, "candidate": {}},
        "unit": "task(case)", "method": "paired_task_cluster_bootstrap",
        "policy_ref": POLICY["policy_id"], "policy_hash": canonical_hash(POLICY),
        "implementation_version": POLICY["implementation_version"],
        "seed": POLICY["bootstrap_seed"], "iterations": POLICY["bootstrap_iterations"],
        "missing_policy": "keep_visible_fail_closed", "n_selected": 1, "n_pairs": 1,
        "missing_pairs": 0, "applicable": False, "reason": "insufficient_tasks",
        "statistics": {
            "n": 1, "mean_diff": 0.0, "median_diff": 0.0, "min_diff": 0.0,
            "max_diff": 0.0, "interval": {
                "low": None, "high": None, "method": "task_cluster_bootstrap",
                "iterations": POLICY["bootstrap_iterations"],
                "seed": POLICY["bootstrap_seed"], "applicable": False,
            },
        },
    }
    return {"schema_version": 1, "policy": deepcopy(POLICY), "result": result}


def _envelope(body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = _body() if body is None else body
    return {"report_id": statistical_report_id(body),
            "published_at": "2026-09-30T00:00:00Z", "body": body}


def _refresh_input_digest(body: dict[str, Any]) -> None:
    body["result"]["input_digest"] = canonical_hash(body["result"]["inputs"])


def test_body_identity_excludes_publication_time():
    body = _body()
    reordered = json.loads(json.dumps(body, sort_keys=True))
    expected = "stat-report-" + canonical_hash(body).removeprefix("sha256:")
    assert statistical_report_id(body) == statistical_report_id(reordered) == expected
    first = _envelope(body)
    later = {**first, "published_at": "2026-10-01T01:02:03.123456+00:00"}
    assert validate_statistical_report(first)["report_id"] == expected
    assert validate_statistical_report(later)["report_id"] == expected
    assert first["published_at"] != later["published_at"]


@pytest.mark.parametrize("change", ["metering", "refs", "k", "policy", "results"])
def test_every_frozen_body_input_changes_identity(change):
    body = _body()
    changed = deepcopy(body)
    if change == "metering":
        changed["result"]["inputs"]["subject_case_metrics"]["baseline"][0][
            "metering"]["latency_ms"] = 20.0
        _refresh_input_digest(changed)
    elif change == "refs":
        for refs in (changed["result"]["refs"], changed["result"]["inputs"]["refs"]):
            refs["baseline"]["scoring_pass_id"] = "pass-new"
        _refresh_input_digest(changed)
    elif change == "k":
        changed["result"]["k"] = changed["result"]["inputs"]["k"] = 2
        _refresh_input_digest(changed)
    elif change == "policy":
        changed["policy"]["confidence"] = 0.9
        changed["result"]["policy_hash"] = canonical_hash(changed["policy"])
    else:
        changed["result"]["statistics"]["mean_diff"] = 0.5
    assert statistical_report_id(changed) != statistical_report_id(body)
    assert validate_statistical_report(_envelope(changed))["body"] == changed


@pytest.mark.parametrize("corruption", [
    "report_id", "input_digest", "refs", "policy_hash", "policy_ref", "seed",
    "iterations", "implementation_version", "input_k", "interval_seed", "interval_iterations",
])
def test_integrity_checks_are_self_contained(corruption):
    body = _body()
    if corruption == "report_id":
        report = _envelope(body)
        report["report_id"] = "stat-report-" + "0" * 64
    else:
        result = body["result"]
        if corruption == "refs":
            result["refs"]["baseline"]["scoring_pass_id"] = "pass-other"
        elif corruption == "input_k":
            result["inputs"]["k"] = 2
            _refresh_input_digest(body)
        elif corruption in ("interval_seed", "interval_iterations"):
            result["statistics"]["interval"][corruption.removeprefix("interval_")] += 1
        elif corruption in ("seed", "iterations"):
            result[corruption] += 1
        else:
            result[corruption] = "different"
        report = _envelope(body)
    with pytest.raises(ValueError):
        validate_statistical_report(report)
    with pytest.raises(ValidationError):
        StatisticalReport.model_validate(report)


def test_old_internally_consistent_policy_remains_readable():
    body = _body()
    policy, result = body["policy"], body["result"]
    policy.update({"policy_id": "statistical_policy@0", "bootstrap_seed": 42,
                   "bootstrap_iterations": 100, "implementation_version": "old.statistics@0",
                   "confidence": 0.9})
    result.update({"policy_ref": policy["policy_id"], "policy_hash": canonical_hash(policy),
                   "seed": policy["bootstrap_seed"], "iterations": policy["bootstrap_iterations"],
                   "implementation_version": policy["implementation_version"]})
    result["statistics"]["interval"].update(seed=42, iterations=100)
    report = _envelope(body)
    assert validate_statistical_report(report) == report
    assert StatisticalReport.model_validate(report).model_dump(mode="json") == report


@pytest.mark.parametrize("field", REQUIRED_RESULT_FIELDS)
def test_missing_required_result_field_rejects_even_with_rehashed_body(field):
    body = _body()
    del body["result"][field]
    with pytest.raises(ValueError):
        validate_statistical_report(_envelope(body))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("location", ["input", "policy", "result"])
def test_raw_nonfinite_json_rejects(value, location):
    body = _body()
    if location == "input":
        body["result"]["inputs"]["subject_case_metrics"]["baseline"][0][
            "metering"]["latency_ms"] = value
    elif location == "policy":
        body["policy"]["confidence"] = value
    else:
        body["result"]["statistics"]["mean_diff"] = value
    with pytest.raises(ValueError):
        statistical_report_id(body)
    report = {"report_id": "stat-report-" + "0" * 64,
              "published_at": "2026-09-30T00:00:00Z", "body": body}
    with pytest.raises(ValueError):
        validate_statistical_report(report)


def test_named_nonfinite_inputs_and_diagnostics_survive_detached_roundtrip():
    body = _body()
    measurements = body["result"]["inputs"]["subject_case_metrics"]["baseline"]
    markers = [{"nonfinite": label} for label in ("nan", "inf", "-inf")]
    measurements[0]["metering"] = {"latency_ms": markers}
    body["result"]["trial_aggregation"] = {"baseline": {"qualifications": ["missing_trial"]}}
    body["result"]["new_diagnostic"] = {"invalid_values": [None, -1, True], "currency": None}
    _refresh_input_digest(body)
    report = _envelope(body)
    original = deepcopy(report)
    validated = validate_statistical_report(report)
    model = StatisticalReport.model_validate(report)
    assert validated == report
    assert json.loads(json.dumps(validated, allow_nan=False)) == report
    validated["body"]["result"]["new_diagnostic"]["invalid_values"].append(99)
    model.body["result"]["inputs"]["subject_case_metrics"]["baseline"].clear()
    assert body["result"]["new_diagnostic"]["invalid_values"] == [None, -1, True]
    assert report == original


@pytest.mark.parametrize("side", ["baseline", "candidate"])
@pytest.mark.parametrize("field", ["run_id", "scoring_pass_id", "report_schema", "evidence_hash"])
@pytest.mark.parametrize("bad", ["", "   ", None, 123])
def test_both_fixed_refs_are_validated(side, field, bad):
    body = _body()
    for refs in (body["result"]["refs"], body["result"]["inputs"]["refs"]):
        refs[side][field] = bad
    _refresh_input_digest(body)
    with pytest.raises(ValueError):
        validate_statistical_report(_envelope(body))


@pytest.mark.parametrize("timestamp", ["", "yesterday", "2026-09-30", "2026-09-30T00:00:00",
                                         "2026-09-30T01:00:00+01:00", 123])
def test_publication_time_requires_utc_rfc3339(timestamp):
    report = _envelope()
    report["published_at"] = timestamp
    with pytest.raises(ValueError):
        validate_statistical_report(report)


@pytest.mark.parametrize("field", ["body", "qualified", "published_at", "report_id", "policy"])
def test_request_rejects_client_publication_fields(field):
    with pytest.raises(ValidationError):
        StatisticalReportPublishRequest.model_validate({
            "baseline_run_id": "base", "candidate_run_id": "candidate", field: {},
        })


@pytest.mark.parametrize("k", [True, False, 0, -1, 1.0, "1"])
def test_request_requires_positive_strict_integer_k(k):
    with pytest.raises(ValidationError):
        StatisticalReportPublishRequest(baseline_run_id="base", candidate_run_id="candidate", k=k)


@pytest.mark.parametrize("field", ["baseline_run_id", "candidate_run_id",
                                    "baseline_pass_id", "candidate_pass_id"])
@pytest.mark.parametrize("bad", ["", " \t ", 123])
def test_request_rejects_blank_or_coerced_refs(field, bad):
    with pytest.raises(ValidationError):
        StatisticalReportPublishRequest.model_validate({
            "baseline_run_id": "base", "candidate_run_id": "candidate", field: bad,
        })


@pytest.mark.parametrize("factors", [[""], ["  "], [1], "model"])
def test_request_rejects_invalid_factors(factors):
    with pytest.raises(ValidationError):
        StatisticalReportPublishRequest(baseline_run_id="base", candidate_run_id="candidate",
                                        allowed_factors=factors)


def test_request_default_and_duplicate_factor_normalization():
    first = StatisticalReportPublishRequest(baseline_run_id="base", candidate_run_id="candidate")
    second = StatisticalReportPublishRequest(baseline_run_id="base", candidate_run_id="candidate",
                                            allowed_factors=["runtime", "model", "runtime"])
    assert first.allowed_factors == ["model"]
    assert second.allowed_factors == ["model", "runtime"]
    assert first.k == 1 and first.baseline_pass_id is first.candidate_pass_id is None
    first.allowed_factors.append("runtime")
    assert StatisticalReportPublishRequest(baseline_run_id="base", candidate_run_id="candidate").allowed_factors == ["model"]


@pytest.mark.parametrize("change", ["extra_envelope", "extra_body", "wrong_schema", "boolean_schema",
                                    "negative_count", "boolean_count", "count_sum", "boolean_k",
                                    "unknown_ref_field", "missing_ref_side", "not_boolean_applicable"])
def test_strict_envelope_body_and_result_shape(change):
    body = _body()
    if change == "extra_body":
        body["qualified"] = True
    elif change == "wrong_schema":
        body["schema_version"] = 2
    elif change == "boolean_schema":
        body["schema_version"] = True
    elif change in ("negative_count", "boolean_count", "count_sum"):
        body["result"]["missing_pairs"] = {"negative_count": -1, "boolean_count": False,
                                           "count_sum": 1}[change]
    elif change == "boolean_k":
        body["result"]["k"] = body["result"]["inputs"]["k"] = True
        _refresh_input_digest(body)
    elif change == "unknown_ref_field":
        for refs in (body["result"]["refs"], body["result"]["inputs"]["refs"]):
            refs["baseline"]["qualified"] = True
        _refresh_input_digest(body)
    elif change == "missing_ref_side":
        del body["result"]["refs"]["candidate"]
        del body["result"]["inputs"]["refs"]["candidate"]
        _refresh_input_digest(body)
    elif change == "not_boolean_applicable":
        body["result"]["applicable"] = "false"
    report = _envelope(body)
    if change == "extra_envelope":
        report["qualified"] = True
    with pytest.raises(ValueError):
        validate_statistical_report(report)


@pytest.mark.parametrize("field", list(POLICY))
def test_frozen_policy_is_complete(field):
    body = _body()
    del body["policy"][field]
    body["result"]["policy_hash"] = canonical_hash(body["policy"])
    with pytest.raises(ValueError):
        validate_statistical_report(_envelope(body))


@pytest.mark.parametrize("field", ["refs", "allowed_factors", "k", "subject_case_metrics"])
def test_frozen_inputs_are_complete(field):
    body = _body()
    del body["result"]["inputs"][field]
    _refresh_input_digest(body)
    with pytest.raises(ValueError):
        validate_statistical_report(_envelope(body))


@pytest.mark.parametrize("bad", [{1: "not-a-json-key"}, {"diagnostic": {1, 2}}])
def test_identity_rejects_non_json_objects(bad):
    body = _body()
    body["result"]["new_diagnostic"] = bad
    with pytest.raises(ValueError):
        statistical_report_id(body)


@pytest.mark.parametrize("field", ["n", "mean_diff", "median_diff", "min_diff", "max_diff", "interval"])
def test_present_paired_statistics_are_complete(field):
    body = _body()
    del body["result"]["statistics"][field]
    with pytest.raises(ValueError):
        validate_statistical_report(_envelope(body))


@pytest.mark.parametrize("field", ["low", "high", "method", "iterations", "seed", "applicable"])
def test_present_interval_is_complete(field):
    body = _body()
    del body["result"]["statistics"]["interval"][field]
    with pytest.raises(ValueError):
        validate_statistical_report(_envelope(body))


def test_not_applicable_result_without_statistics_is_readable():
    body = _body()
    body["result"].update(statistics=None, reason="missing_or_uncertain_case", n_pairs=0,
                           missing_pairs=1)
    assert validate_statistical_report(_envelope(body))["body"] == body


@pytest.mark.parametrize("change", ["interval_boolean", "interval_low", "interval_high",
                                    "interval_applicability", "statistics_count", "absent_statistics"])
def test_paired_result_shape_and_applicability_are_consistent(change):
    body = _body()
    result = body["result"]
    interval = result["statistics"]["interval"]
    if change == "interval_boolean":
        interval["applicable"] = "false"
    elif change in ("interval_low", "interval_high"):
        interval[change.removeprefix("interval_")] = True
    elif change == "interval_applicability":
        interval["applicable"] = True
    elif change == "statistics_count":
        result["statistics"]["n"] = 2
    else:
        result.update(applicable=True, statistics=None)
    with pytest.raises(ValueError):
        validate_statistical_report(_envelope(body))


def test_circular_input_is_invalid_json():
    body = _body()
    body["result"]["diagnostic"] = body
    with pytest.raises(ValueError):
        statistical_report_id(body)
