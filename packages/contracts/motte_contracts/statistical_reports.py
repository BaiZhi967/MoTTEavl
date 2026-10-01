"""Immutable statistical publications: strict requests and self-contained integrity.

Stored policies are validated against their own result bindings, never against
an installed evaluator policy. This module has no evaluation or storage access.
"""
from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any

from pydantic import Field, StrictInt, StrictStr, field_validator, model_validator

from .comparison import RunReportRef
from .hashing import canonical_hash, canonical_json
from .messages import Contract


class StatisticalReportPublishRequest(Contract):
    baseline_run_id: StrictStr
    candidate_run_id: StrictStr
    allowed_factors: list[StrictStr] = Field(default_factory=lambda: ["model"])
    baseline_pass_id: StrictStr | None = None
    candidate_pass_id: StrictStr | None = None
    k: StrictInt = Field(default=1, ge=1)

    @field_validator("baseline_run_id", "candidate_run_id", "baseline_pass_id", "candidate_pass_id")
    @classmethod
    def _nonblank_ref(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Run and Pass IDs must not be blank")
        return value

    @field_validator("allowed_factors")
    @classmethod
    def _normalize_factors(cls, value: list[str]) -> list[str]:
        for factor in value:
            _nonblank(factor, "allowed factor")
        return sorted(set(value))


class StatisticalReport(Contract):
    report_id: StrictStr
    published_at: StrictStr
    body: dict[str, Any]

    @model_validator(mode="before")
    @classmethod
    def _validate_envelope(cls, value: Any) -> dict[str, Any]:
        return validate_statistical_report(value)


def _strict_json_copy(value: Any) -> Any:
    """Reject non-JSON values before canonicalization, then detach all containers."""
    def check(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("JSON object keys must be strings")
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif item is not None and not isinstance(item, (str, bool, int, float)):
            raise ValueError("statistical reports must contain only JSON values")

    try:
        json.dumps(value, allow_nan=False)
        check(value)
        return json.loads(canonical_json(value))
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError("statistical reports require strict finite JSON") from exc


def statistical_report_id(body: dict[str, Any]) -> str:
    """Content identity excludes the server timestamp and envelope ID."""
    if not isinstance(body, dict):
        raise ValueError("statistical report body must be an object")
    detached = _strict_json_copy(body)
    return "stat-report-" + canonical_hash(detached).removeprefix("sha256:")


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _required(value: dict[str, Any], fields: tuple[str, ...], name: str) -> None:
    missing = set(fields) - value.keys()
    if missing:
        raise ValueError(f"{name} missing fields: {','.join(sorted(missing))}")


def _nonblank(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonblank string")


def _integer(value: Any, name: str, minimum: int | None = None) -> None:
    if type(value) is not int or (minimum is not None and value < minimum):
        suffix = f" >= {minimum}" if minimum is not None else ""
        raise ValueError(f"{name} must be an integer{suffix}")


def _refs(value: Any) -> None:
    refs = _object(value, "refs")
    if set(refs) != {"baseline", "candidate"}:
        raise ValueError("refs must contain exactly baseline and candidate")
    for side, ref in refs.items():
        validated = RunReportRef.model_validate(ref)
        for field, item in validated.model_dump().items():
            _nonblank(item, f"{side}.{field}")


def _policy_and_result(body: dict[str, Any]) -> None:
    policy = _object(body["policy"], "policy")
    result = _object(body["result"], "result")
    _required(policy, (
        "policy_id", "unit", "confidence", "interval_method", "bootstrap_iterations",
        "bootstrap_seed", "quantile_interpolation", "binary_interval", "implementation_version",
        "missing_policy",
    ), "policy")
    _required(result, (
        "refs", "inputs", "input_digest", "k", "descriptive", "unit", "method", "policy_ref",
        "policy_hash", "implementation_version", "seed", "iterations", "missing_policy",
        "n_selected", "n_pairs", "missing_pairs", "applicable", "reason", "statistics",
    ), "result")
    for field in ("policy_id", "unit", "interval_method", "quantile_interpolation",
                  "binary_interval", "implementation_version", "missing_policy"):
        _nonblank(policy[field], f"policy.{field}")
    if (type(policy["confidence"]) not in (int, float)
            or not 0 < policy["confidence"] < 1):
        raise ValueError("policy.confidence must be a number between 0 and 1")
    _integer(policy["bootstrap_seed"], "policy.bootstrap_seed")
    _integer(policy["bootstrap_iterations"], "policy.bootstrap_iterations", 1)
    bindings = {
        "policy_ref": policy["policy_id"], "policy_hash": canonical_hash(policy),
        "seed": policy["bootstrap_seed"], "iterations": policy["bootstrap_iterations"],
        "implementation_version": policy["implementation_version"],
    }
    for field, expected in bindings.items():
        if result[field] != expected:
            raise ValueError(f"result.{field} does not match stored policy")
    for field in ("input_digest", "unit", "method", "policy_ref", "policy_hash",
                  "implementation_version", "missing_policy"):
        _nonblank(result[field], f"result.{field}")
    for field in ("seed", "iterations", "k", "n_selected", "n_pairs", "missing_pairs"):
        minimum = 1 if field in ("iterations", "k") else None if field == "seed" else 0
        _integer(result[field], f"result.{field}", minimum)
    if result["n_pairs"] + result["missing_pairs"] != result["n_selected"]:
        raise ValueError("paired and missing counts must cover selected tasks")
    if type(result["applicable"]) is not bool:
        raise ValueError("result.applicable must be a boolean")
    if result["reason"] is not None:
        _nonblank(result["reason"], "result.reason")
    _refs(result["refs"])
    inputs = _object(result["inputs"], "inputs")
    _required(inputs, ("refs", "allowed_factors", "k", "subject_case_metrics"), "inputs")
    _refs(inputs["refs"])
    if result["refs"] != inputs["refs"]:
        raise ValueError("result refs do not match frozen input refs")
    _integer(inputs["k"], "inputs.k", 1)
    if inputs["k"] != result["k"]:
        raise ValueError("result k does not match frozen input k")
    if not isinstance(inputs["allowed_factors"], list):
        raise ValueError("inputs.allowed_factors must be a list")
    for factor in inputs["allowed_factors"]:
        _nonblank(factor, "inputs.allowed_factors")
    metering = _object(inputs["subject_case_metrics"], "subject_case_metrics")
    descriptive = _object(result["descriptive"], "descriptive")
    for side in ("baseline", "candidate"):
        if not isinstance(metering.get(side), list):
            raise ValueError(f"subject_case_metrics.{side} must be a list")
        _object(descriptive.get(side), f"descriptive.{side}")
    if result["input_digest"] != canonical_hash(inputs):
        raise ValueError("input_digest does not match frozen inputs")
    if result["statistics"] is None and result["applicable"]:
        raise ValueError("applicable results require statistics")
    if result["statistics"] is not None:
        statistics = _object(result["statistics"], "statistics")
        _required(statistics, ("n", "mean_diff", "median_diff", "min_diff", "max_diff", "interval"),
                  "statistics")
        _integer(statistics["n"], "statistics.n", 0)
        if statistics["n"] != result["n_pairs"]:
            raise ValueError("statistics.n does not match paired task count")
        for field in ("mean_diff", "median_diff", "min_diff", "max_diff"):
            if statistics[field] is not None and type(statistics[field]) not in (int, float):
                raise ValueError(f"statistics.{field} must be a number or null")
        interval = _object(statistics["interval"], "interval")
        _required(interval, ("low", "high", "method", "iterations", "seed", "applicable"), "interval")
        _nonblank(interval["method"], "interval.method")
        if (type(interval["applicable"]) is not bool
                or interval["applicable"] != result["applicable"]):
            raise ValueError("interval applicability does not match result")
        for field in ("low", "high"):
            if interval[field] is not None and type(interval[field]) not in (int, float):
                raise ValueError(f"interval.{field} must be a number or null")
        for field in ("seed", "iterations"):
            _integer(interval[field], f"interval.{field}", 1 if field == "iterations" else None)
            if interval[field] != result[field]:
                raise ValueError(f"interval.{field} does not match stored policy")


def validate_statistical_report(report: dict[str, Any]) -> dict[str, Any]:
    """Return a detached, internally bound envelope or raise ``ValueError``."""
    envelope = _object(_strict_json_copy(report), "statistical report")
    if set(envelope) != {"report_id", "published_at", "body"}:
        raise ValueError("statistical report envelope requires exactly report_id, published_at, body")
    _nonblank(envelope["report_id"], "report_id")
    timestamp = envelope["published_at"]
    if (not isinstance(timestamp, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)", timestamp)):
        raise ValueError("published_at must be a UTC RFC3339 timestamp")
    try:
        datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise ValueError("published_at must be a valid UTC RFC3339 timestamp") from exc
    body = _object(envelope["body"], "body")
    if set(body) != {"schema_version", "policy", "result"}:
        raise ValueError("body requires exactly schema_version, policy, result")
    if type(body["schema_version"]) is not int or body["schema_version"] != 1:
        raise ValueError("statistical report schema_version must be 1")
    _policy_and_result(body)
    if envelope["report_id"] != statistical_report_id(body):
        raise ValueError("report_id does not match canonical body")
    return envelope
