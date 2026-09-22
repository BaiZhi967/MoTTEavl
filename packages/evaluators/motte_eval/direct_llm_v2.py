"""Pure deterministic scorers for the Direct LLM v2 evaluator.

This module owns parsing and comparison only. It performs no I/O and does not
load scorer code dynamically. Scorer specs are plain dictionaries resolved by
``motte_eval.scorer_registry``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, DecimalException, localcontext
from typing import Any, Mapping, Sequence

_MARKER = re.compile(r"\[ANSWER:([^\]\r\n]*)\]")
_ASCII_LABEL = re.compile(r"[A-Z]")
_NONFINITE_NUMBERS = frozenset({"NaN", "+NaN", "-NaN", "Infinity", "+Infinity", "-Infinity"})
_MAX_NUMBER_LENGTH = 256
_MAX_NUMBER_SIGNIFICANT_DIGITS = 128
_MAX_NUMBER_ABSOLUTE_EXPONENT = 1_000
_MAX_NORMALIZED_NUMBER_LENGTH = 1_024
_NUMERIC_DIVISION_PRECISION = _MAX_NUMBER_SIGNIFICANT_DIGITS
_NUMERIC_OPERATION_PRECISION = 256
_NUMERIC_OPERATION_EXPONENT_LIMIT = 4_096
_MAX_JSON_DEPTH = 128


def _numeric_context(precision: int) -> Context:
    return Context(
        prec=precision,
        rounding=ROUND_HALF_EVEN,
        Emin=-_NUMERIC_OPERATION_EXPONENT_LIMIT,
        Emax=_NUMERIC_OPERATION_EXPONENT_LIMIT,
    )


@dataclass(frozen=True)
class ParseFailure:
    """Stable parse failure evidence that never embeds model output."""

    code: str


class _DuplicateJsonKey(ValueError):
    pass


class _NonFiniteJson(ValueError):
    pass


class _NumericOperationError(ArithmeticError):
    def __init__(self, code: str = "numeric_operation_failed") -> None:
        super().__init__(code)
        self.code = code


def _unknown_config(config: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(config) - allowed)
    if unknown:
        raise ValueError(f"unknown scorer config fields: {', '.join(unknown)}")


def _last_nonempty_line(content: str) -> str | None:
    lines = [line for line in content.splitlines() if line.strip()]
    return lines[-1] if lines else None


def _decimal_text(value: Decimal) -> str:
    if value.is_nan():
        return "-NaN" if value.is_signed() else "NaN"
    if value.is_infinite():
        return "-Infinity" if value.is_signed() else "Infinity"
    if value.is_zero():
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _numeric_bounds_failure(value: Decimal) -> ParseFailure | None:
    if not value.is_finite():
        return None
    decimal_tuple = value.as_tuple()
    if len(decimal_tuple.digits) > _MAX_NUMBER_SIGNIFICANT_DIGITS:
        return ParseFailure("too_many_significant_digits")
    exponent = decimal_tuple.exponent
    if not isinstance(exponent, int) or abs(exponent) > _MAX_NUMBER_ABSOLUTE_EXPONENT:
        return ParseFailure("exponent_out_of_bounds")
    try:
        normalized = _decimal_text(value)
    except DecimalException:
        return ParseFailure("numeric_operation_failed")
    if len(normalized) > _MAX_NORMALIZED_NUMBER_LENGTH:
        return ParseFailure("normalized_number_too_long")
    return None


def _bounded_numeric_text(value: Decimal) -> str:
    failure = _numeric_bounds_failure(value)
    if failure is not None:
        raise _NumericOperationError(failure.code)
    try:
        return _decimal_text(value)
    except DecimalException as error:
        raise _NumericOperationError() from error


def _normalize_exact_text(value: str, config: Mapping[str, Any]) -> str:
    """Apply exact@1 normalization in a fixed, reviewable order."""
    normalized = value.strip()
    if config["normalize_whitespace"]:
        normalized = " ".join(normalized.split())
    if not config["case_sensitive"]:
        normalized = normalized.casefold()
    return normalized


class ExactScorer:
    """Bare-text equality after strip, optional whitespace folding, and optional casefold."""

    scorer_id = "exact"
    version = "1"

    def normalize_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        _unknown_config(config, {"normalize_whitespace", "case_sensitive"})
        normalized: dict[str, Any] = {}
        for field, default in (("normalize_whitespace", False), ("case_sensitive", True)):
            value = config.get(field, default)
            if type(value) is not bool:
                raise ValueError(f"{field} must be a boolean")
            normalized[field] = value
        return normalized

    def validate_expected(self, expected: Any, config: Mapping[str, Any]) -> str:
        if not isinstance(expected, str) or not expected.strip():
            raise ValueError("exact expected must be a non-empty string")
        return _normalize_exact_text(expected, config)

    def parse_output(self, content: Any, config: Mapping[str, Any]) -> str | ParseFailure:
        if not isinstance(content, str):
            return ParseFailure("output_not_text")
        return _normalize_exact_text(content, config)

    def compare(self, parsed: Any, expected: Any, config: Mapping[str, Any]) -> bool:
        return isinstance(parsed, str) and isinstance(expected, str) and parsed == expected

    def serialize_parsed(self, parsed: Any) -> Any:
        return parsed


class ChoiceScorer:
    scorer_id = "choice"
    version = "1"

    def normalize_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        _unknown_config(config, {"labels", "allow_bare_final_label"})
        labels = config.get("labels")
        if not isinstance(labels, (list, tuple)):
            raise ValueError("choice labels must be a list")
        if not 2 <= len(labels) <= 26:
            raise ValueError("choice labels must contain 2-26 values")
        if any(not isinstance(label, str) or _ASCII_LABEL.fullmatch(label) is None
               for label in labels):
            raise ValueError("choice labels must be single uppercase ASCII letters")
        if len(set(labels)) != len(labels):
            raise ValueError("choice labels must not contain duplicates")
        allow_bare = config.get("allow_bare_final_label", False)
        if type(allow_bare) is not bool:
            raise ValueError("allow_bare_final_label must be a boolean")
        return {"labels": list(labels), "allow_bare_final_label": allow_bare}

    def validate_expected(self, expected: Any, config: Mapping[str, Any]) -> str:
        labels = config["labels"]
        if not isinstance(expected, str) or expected not in labels:
            raise ValueError("choice expected must be exactly one configured label")
        return expected

    def parse_output(self, content: Any, config: Mapping[str, Any]) -> str | ParseFailure:
        if not isinstance(content, str):
            return ParseFailure("output_not_text")
        final_line = _last_nonempty_line(content)
        if final_line is None:
            return ParseFailure("empty_output")
        markers = _MARKER.findall(content)
        if len(markers) > 1:
            return ParseFailure("multiple_markers")
        if not markers:
            if config["allow_bare_final_label"] and final_line in config["labels"]:
                return final_line
            return ParseFailure("missing_marker")
        marker = markers[0]
        if final_line != f"[ANSWER:{marker}]":
            return ParseFailure("marker_not_final_line")
        if marker not in config["labels"]:
            return ParseFailure("invalid_label")
        return marker

    def compare(self, parsed: Any, expected: Any, config: Mapping[str, Any]) -> bool:
        return isinstance(parsed, str) and parsed == expected

    def serialize_parsed(self, parsed: Any) -> Any:
        return parsed


def _config_decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{field} must be a decimal string, integer, or Decimal")
    if isinstance(value, int):
        decimal = Decimal(value)
    elif isinstance(value, Decimal):
        decimal = value
    elif isinstance(value, str):
        if len(value) > _MAX_NUMBER_LENGTH:
            raise ValueError(f"{field} exceeds numeric bounds: number_too_long")
        try:
            decimal = Decimal(value)
        except DecimalException as error:
            raise ValueError(f"{field} must be a decimal value") from error
    else:
        raise ValueError(f"{field} must be a decimal string, integer, or Decimal")
    if not decimal.is_finite() or decimal < 0:
        raise ValueError(f"{field} must be finite and non-negative")
    failure = _numeric_bounds_failure(decimal)
    if failure is not None:
        raise ValueError(f"{field} exceeds numeric bounds: {failure.code}")
    return decimal


def _numeric_pattern(config: Mapping[str, Any]) -> re.Pattern[str]:
    unsigned = r"(?:(?:\d{1,3}(?:,\d{3})+)|\d+)" if config["allow_thousands"] else r"\d+"
    decimal = unsigned + r"(?:\.\d+)?"
    if config["allow_scientific"]:
        decimal += r"(?:[eE][+-]?\d+)?"
    sign = r"[+-]?" if config["allow_sign"] else ""
    return re.compile(sign + decimal)


def _parse_numeric_token(token: str, config: Mapping[str, Any]) -> Decimal | ParseFailure:
    if len(token) > _MAX_NUMBER_LENGTH:
        return ParseFailure("number_too_long")
    if token in _NONFINITE_NUMBERS:
        if not config["allow_non_finite"]:
            return ParseFailure("non_finite_not_allowed")
        if token[:1] in {"+", "-"} and not config["allow_sign"]:
            return ParseFailure("invalid_number")
        try:
            return Decimal(token)
        except DecimalException:
            return ParseFailure("invalid_number")
    if "/" in token:
        if not config["allow_fraction"]:
            return ParseFailure("fraction_not_allowed")
        sign = r"[+-]?" if config["allow_sign"] else ""
        match = re.fullmatch(rf"({sign}\d+)/(\d+)", token)
        if match is None:
            return ParseFailure("invalid_fraction")
        try:
            numerator = Decimal(match.group(1))
            denominator = Decimal(match.group(2))
            for operand in (numerator, denominator):
                failure = _numeric_bounds_failure(operand)
                if failure is not None:
                    return failure
            if denominator == 0:
                return ParseFailure("division_by_zero")
            with localcontext(_numeric_context(_NUMERIC_DIVISION_PRECISION)):
                parsed = numerator / denominator
        except DecimalException:
            return ParseFailure("numeric_operation_failed")
        return _numeric_bounds_failure(parsed) or parsed
    if ("e" in token.lower()) and not config["allow_scientific"]:
        return ParseFailure("scientific_not_allowed")
    if _numeric_pattern(config).fullmatch(token) is None:
        return ParseFailure("invalid_number")
    try:
        parsed = Decimal(token.replace(",", ""))
    except DecimalException:
        return ParseFailure("invalid_number")
    return _numeric_bounds_failure(parsed) or parsed


class NumericScorer:
    scorer_id = "numeric"
    version = "1"

    def normalize_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        _unknown_config(config, {
            "mode", "absolute_tolerance", "relative_tolerance", "allow_sign",
            "allow_thousands", "allow_scientific", "allow_fraction", "allow_non_finite",
        })
        absolute_value = config.get("absolute_tolerance")
        relative_value = config.get("relative_tolerance")
        absolute = absolute_value is not None
        relative = relative_value is not None
        if absolute and relative:
            raise ValueError("absolute_tolerance and relative_tolerance are mutually exclusive")
        mode = config.get("mode")
        if mode is None:
            mode = "absolute_tolerance" if absolute else (
                "relative_tolerance" if relative else "exact"
            )
        if mode not in {"exact", "absolute_tolerance", "relative_tolerance"}:
            raise ValueError("numeric mode must be exact, absolute_tolerance, or relative_tolerance")
        if mode == "exact" and (absolute or relative):
            raise ValueError("exact mode cannot include a tolerance")
        if mode == "absolute_tolerance" and (not absolute or relative):
            raise ValueError("absolute_tolerance mode requires only absolute_tolerance")
        if mode == "relative_tolerance" and (not relative or absolute):
            raise ValueError("relative_tolerance mode requires only relative_tolerance")
        absolute_tolerance = (
            _config_decimal(absolute_value, "absolute_tolerance") if absolute else None
        )
        relative_tolerance = (
            _config_decimal(relative_value, "relative_tolerance") if relative else None
        )
        normalized: dict[str, Any] = {
            "mode": mode,
            "absolute_tolerance": (
                None if absolute_tolerance is None else _bounded_numeric_text(absolute_tolerance)
            ),
            "relative_tolerance": (
                None if relative_tolerance is None else _bounded_numeric_text(relative_tolerance)
            ),
        }
        for field, default in (
            ("allow_sign", True), ("allow_thousands", True),
            ("allow_scientific", False), ("allow_fraction", False),
            ("allow_non_finite", False),
        ):
            value = config.get(field, default)
            if type(value) is not bool:
                raise ValueError(f"{field} must be a boolean")
            normalized[field] = value
        if normalized["allow_non_finite"] and mode != "exact":
            raise ValueError("non-finite numbers are only supported in exact mode")
        return normalized

    def validate_expected(self, expected: Any, config: Mapping[str, Any]) -> Decimal:
        if isinstance(expected, bool) or isinstance(expected, float):
            raise ValueError("numeric expected must not be a boolean or binary float")
        if isinstance(expected, Decimal):
            parsed: Decimal | ParseFailure = expected
        elif isinstance(expected, int):
            parsed = Decimal(expected)
        elif isinstance(expected, str):
            parsed = _parse_numeric_token(expected, config)
        else:
            raise ValueError("numeric expected must be a decimal string, integer, or Decimal")
        if isinstance(parsed, ParseFailure):
            raise ValueError(f"invalid numeric expected: {parsed.code}")
        if not parsed.is_finite() and not config["allow_non_finite"]:
            raise ValueError("invalid numeric expected: non_finite_not_allowed")
        failure = _numeric_bounds_failure(parsed)
        if failure is not None:
            raise ValueError(f"invalid numeric expected: {failure.code}")
        return parsed

    def parse_output(self, content: Any, config: Mapping[str, Any]) -> Decimal | ParseFailure:
        if not isinstance(content, str):
            return ParseFailure("output_not_text")
        final_line = _last_nonempty_line(content)
        if final_line is None:
            return ParseFailure("empty_output")
        markers = _MARKER.findall(content)
        if len(markers) > 1:
            return ParseFailure("multiple_markers")
        if not markers:
            return ParseFailure("missing_marker")
        token = markers[0]
        if final_line != f"[ANSWER:{token}]":
            return ParseFailure("marker_not_final_line")
        return _parse_numeric_token(token, config)

    def compare(self, parsed: Any, expected: Any, config: Mapping[str, Any]) -> bool:
        if not isinstance(parsed, Decimal) or not isinstance(expected, Decimal):
            return False
        if parsed.is_nan() or expected.is_nan():
            return parsed.as_tuple() == expected.as_tuple()
        mode = config["mode"]
        if mode == "exact":
            return parsed == expected
        if not parsed.is_finite() or not expected.is_finite():
            return False
        tolerance_field = (
            "absolute_tolerance" if mode == "absolute_tolerance" else "relative_tolerance"
        )
        try:
            with localcontext(_numeric_context(_NUMERIC_OPERATION_PRECISION)):
                tolerance = Decimal(config[tolerance_field])
                difference = abs(parsed - expected)
                if mode == "absolute_tolerance":
                    return difference <= tolerance
                return difference <= tolerance * abs(expected)
        except DecimalException as error:
            raise _NumericOperationError() from error

    def serialize_parsed(self, parsed: Any) -> Any:
        if not isinstance(parsed, Decimal):
            raise _NumericOperationError("invalid_number")
        return _bounded_numeric_text(parsed)


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise _NonFiniteJson(value)


def _within_json_depth(value: Any) -> bool:
    pending: list[tuple[Any, int]] = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        if depth > _MAX_JSON_DEPTH:
            return False
        if isinstance(current, dict):
            pending.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            pending.extend((child, depth + 1) for child in current)
    return True


def _parse_json(content: Any) -> Any | ParseFailure:
    if not isinstance(content, str):
        return ParseFailure("output_not_text")
    try:
        parsed = json.loads(content, parse_int=Decimal, parse_float=Decimal,
                             parse_constant=_reject_json_constant, object_pairs_hook=_json_object)
    except RecursionError:
        return ParseFailure("max_depth_exceeded")
    except _DuplicateJsonKey:
        return ParseFailure("duplicate_key")
    except _NonFiniteJson:
        return ParseFailure("non_finite_number")
    except (json.JSONDecodeError, TypeError, ValueError):
        return ParseFailure("invalid_json")
    if not _within_json_depth(parsed):
        return ParseFailure("max_depth_exceeded")
    return parsed


def _json_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, Decimal) or isinstance(right, Decimal):
        return isinstance(left, Decimal) and isinstance(right, Decimal) and left == right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    if isinstance(left, list) or isinstance(right, list):
        return (isinstance(left, list) and isinstance(right, list)
                and len(left) == len(right)
                and all(_json_equal(a, b) for a, b in zip(left, right, strict=True)))
    if isinstance(left, dict) or isinstance(right, dict):
        return (isinstance(left, dict) and isinstance(right, dict)
                and left.keys() == right.keys()
                and all(_json_equal(left[key], right[key]) for key in left))
    return type(left) is type(right) and left == right


def _canonical_json(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return "[" + ",".join(_canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(
            f"{json.dumps(key, ensure_ascii=False)}:{_canonical_json(value[key])}"
            for key in sorted(value)
        ) + "}"
    raise TypeError(f"unsupported parsed JSON type: {type(value).__name__}")


class JsonEqualScorer:
    scorer_id = "json_equal"
    version = "1"

    def normalize_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        _unknown_config(config, set())
        return {}

    def validate_expected(self, expected: Any, config: Mapping[str, Any]) -> Any:
        if not isinstance(expected, str):
            raise ValueError("json_equal expected must be a JSON string")
        parsed = _parse_json(expected)
        if isinstance(parsed, ParseFailure):
            raise ValueError(f"invalid JSON expected: {parsed.code}")
        return parsed

    def parse_output(self, content: Any, config: Mapping[str, Any]) -> Any | ParseFailure:
        return _parse_json(content)

    def compare(self, parsed: Any, expected: Any, config: Mapping[str, Any]) -> bool:
        return _json_equal(parsed, expected)

    def serialize_parsed(self, parsed: Any) -> Any:
        return _canonical_json(parsed)


def _resolve(spec: Mapping[str, Any]):
    from .scorer_registry import resolve_scorer

    return resolve_scorer(spec)


def validate_expected(expected: Any, scorer_spec: Mapping[str, Any]) -> Any:
    resolved = _resolve(scorer_spec)
    return resolved.implementation.validate_expected(expected, resolved.config)


def parse_output(content: Any, scorer_spec: Mapping[str, Any]) -> Any | ParseFailure:
    resolved = _resolve(scorer_spec)
    return resolved.implementation.parse_output(content, resolved.config)


def compare(parsed: Any, expected: Any, scorer_spec: Mapping[str, Any]) -> bool:
    """Compare values returned by ``parse_output`` and ``validate_expected``."""
    resolved = _resolve(scorer_spec)
    if isinstance(parsed, ParseFailure):
        return False
    try:
        return resolved.implementation.compare(parsed, expected, resolved.config)
    except (_NumericOperationError, DecimalException):
        return False


def score(
    result: Any, expected: Any, scorer_spec: Mapping[str, Any], *, attempted: bool | None = None,
) -> dict[str, Any]:
    """Score one result and return bounded, replay-safe evidence."""
    resolved = _resolve(scorer_spec)
    details: dict[str, Any] = {"config_sha256": resolved.config_sha256}
    has_expected = expected is not None
    normalized_expected = (
        resolved.implementation.validate_expected(expected, resolved.config)
        if has_expected else None
    )
    inferred_not_attempted = isinstance(result, dict) and result.get("outcome") == "not_attempted"
    if attempted is None:
        attempted = not inferred_not_attempted
    elif type(attempted) is not bool:
        raise ValueError("attempted must be a boolean")
    base: dict[str, Any] = {
        "passed": False,
        "attempted": attempted,
        "responded": False,
        "judged": has_expected and attempted,
        "scorer": resolved.scorer_id,
        "scorer_version": resolved.version,
        "details": details,
    }
    if not attempted:
        return {**base, "outcome": "not_attempted", "judged": False}
    error = result.get("error") if isinstance(result, dict) else None
    if error:
        error_class = error.get("class") if isinstance(error, dict) else None
        return {
            **base, "outcome": "call_failed", "error_class": error_class, "judged": False,
        }
    base["responded"] = True
    if not has_expected:
        return {**base, "outcome": "no_expectation", "judged": False}
    content = result.get("content") if isinstance(result, dict) else result
    parsed = resolved.implementation.parse_output(content, resolved.config)
    if isinstance(parsed, ParseFailure):
        details["parse_failure"] = {"code": parsed.code}
        return {**base, "outcome": "invalid_format"}
    try:
        passed = resolved.implementation.compare(parsed, normalized_expected, resolved.config)
        serialized = resolved.implementation.serialize_parsed(parsed)
    except _NumericOperationError as error:
        details["parse_failure"] = {"code": error.code}
        return {**base, "outcome": "invalid_format"}
    except DecimalException:
        details["parse_failure"] = {"code": "numeric_operation_failed"}
        return {**base, "outcome": "invalid_format"}
    return {
        **base,
        "outcome": "correct" if passed else "wrong_answer",
        "passed": passed,
        "parsed": serialized,
    }


def score_answer_case(
    result: Any, expected: Any, scorer_spec: Mapping[str, Any], *, attempted: bool | None = None,
) -> dict[str, Any]:
    return score(result, expected, scorer_spec, attempted=attempted)


_SCORE_OUTCOMES = (
    "correct", "wrong_answer", "invalid_format", "call_failed", "no_expectation",
    "not_attempted",
)
_JUDGED_OUTCOMES = frozenset({"correct", "wrong_answer", "invalid_format"})
_TERMINAL_OUTCOMES = frozenset(set(_SCORE_OUTCOMES) - {"not_attempted"})


def aggregate_scores(
    scores: Sequence[Mapping[str, Any]],
    selected_case_ids: Sequence[str] | int | None = None,
    *,
    selected_count: int | None = None,
) -> dict[str, Any]:
    """Aggregate v2 scores using selected cases as every execution-rate denominator.

    ``selected_case_ids`` enables strict membership and duplicate checks. Passing an integer as the
    second positional argument is shorthand for ``selected_count``. Missing score rows are counted
    as ``not_attempted``; unknown outcomes, duplicate rows, and over-complete inputs fail closed.
    """
    selected_ids: tuple[str, ...] | None = None
    if type(selected_case_ids) is int:
        if selected_count is not None:
            raise ValueError("provide selected_case_ids or selected_count, not both")
        selected_count = selected_case_ids
    elif selected_case_ids is not None:
        if selected_count is not None:
            raise ValueError("provide selected_case_ids or selected_count, not both")
        if (isinstance(selected_case_ids, (str, bytes))
                or not isinstance(selected_case_ids, Sequence)):
            raise ValueError("selected_case_ids must be a sequence of non-empty strings")
        selected_ids = tuple(selected_case_ids)
        if any(not isinstance(case_id, str) or not case_id for case_id in selected_ids):
            raise ValueError("selected_case_ids must be a sequence of non-empty strings")
        if len(set(selected_ids)) != len(selected_ids):
            raise ValueError("selected_case_ids must not contain duplicates")
        selected_count = len(selected_ids)
    if type(selected_count) is not int or selected_count < 0:
        raise ValueError("selected_count must be a non-negative integer")

    rows = list(scores)
    if len(rows) > selected_count:
        raise ValueError("score count exceeds selected_count")
    if any(not isinstance(row, Mapping) for row in rows):
        raise ValueError("each score must be an object")
    case_id_presence = ["case_id" in row for row in rows]
    if any(case_id_presence) and not all(case_id_presence):
        raise ValueError("scores must either all include case_id or all use positional identity")
    seen_case_ids: set[str] = set()
    selected_set = set(selected_ids) if selected_ids is not None else None
    counts = {outcome: 0 for outcome in _SCORE_OUTCOMES}
    for row in rows:
        case_id = row.get("case_id")
        if selected_set is not None:
            if not isinstance(case_id, str) or case_id not in selected_set:
                raise ValueError(f"score case_id is not selected: {case_id!r}")
        elif case_id is not None and not isinstance(case_id, str):
            raise ValueError("score case_id must be a string")
        if isinstance(case_id, str):
            if case_id in seen_case_ids:
                raise ValueError(f"duplicate score case_id: {case_id}")
            seen_case_ids.add(case_id)
        outcome = row.get("outcome")
        if outcome not in counts:
            raise ValueError(f"unknown score outcome: {outcome!r}")
        counts[outcome] += 1

    missing = selected_count - len(rows)
    counts["not_attempted"] += missing
    judged = sum(counts[outcome] for outcome in _JUDGED_OUTCOMES)
    attempted = sum(counts[outcome] for outcome in _TERMINAL_OUTCOMES)
    completed = attempted
    correct = counts["correct"]
    invalid_format = counts["invalid_format"]
    return {
        **counts,
        "selected": selected_count,
        "judged": judged,
        "attempted": attempted,
        "completed": completed,
        "accuracy": correct / judged if judged else None,
        "coverage": judged / selected_count if selected_count else None,
        "completion": completed / selected_count if selected_count else None,
        "attempt_rate": attempted / selected_count if selected_count else None,
        "format_failure_rate": invalid_format / judged if judged else None,
        "denominators": {
            "accuracy": "judged",
            "coverage": "selected",
            "completion": "selected",
            "attempt_rate": "selected",
            "format_failure_rate": "judged",
        },
    }
