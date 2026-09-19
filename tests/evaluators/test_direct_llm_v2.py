"""Direct LLM v2 deterministic scorer registry and strict parsers."""
from decimal import Decimal

import pytest

from motte_contracts.direct_llm_v2 import ScorerSpec, scorer_config_sha256
from motte_eval.direct_llm_v2 import (ParseFailure, aggregate_scores, compare, parse_output,
                                      score, validate_expected)
from motte_eval.scorer_registry import (ScorerSpecError, available_scorers, get_scorer,
                                        resolve_scorer)


def scorer_spec(scorer_id, config):
    normalized = get_scorer(scorer_id, "1").normalize_config(config)
    return {
        "id": scorer_id,
        "version": "1",
        "config": normalized,
        "config_sha256": scorer_config_sha256(normalized),
    }


def unchecked_spec(scorer_id, config):
    return {
        "id": scorer_id,
        "version": "1",
        "config": config,
        "config_sha256": scorer_config_sha256(config),
    }


CHOICE = scorer_spec("choice", {"labels": ["A", "B", "C", "D"]})
EXACT = scorer_spec("exact", {})
NUMERIC = scorer_spec("numeric", {})
JSON_EQUAL = scorer_spec("json_equal", {})


def test_registry_is_closed_versioned_and_hashes_only_normalized_config():
    assert available_scorers() == (
        ("choice", "1"), ("exact", "1"), ("json_equal", "1"), ("numeric", "1")
    )
    assert get_scorer("choice", "1").scorer_id == "choice"
    with pytest.raises(ScorerSpecError, match="unsupported scorer"):
        get_scorer("choice", "2")
    with pytest.raises(ScorerSpecError, match="non-empty string"):
        get_scorer("choice", 1)
    with pytest.raises(ScorerSpecError, match="unknown scorer spec fields"):
        resolve_scorer({**CHOICE, "module": "untrusted.scorer"})

    first = resolve_scorer(CHOICE)
    equivalent = resolve_scorer(scorer_spec(
        "choice", {"allow_bare_final_label": False, "labels": ["A", "B", "C", "D"]}
    ))
    changed = resolve_scorer(scorer_spec(
        "choice", {"labels": ["A", "B", "C", "D"], "allow_bare_final_label": True}
    ))
    assert first.config_sha256 == equivalent.config_sha256
    assert first.config_sha256 != changed.config_sha256
    assert first.config_sha256 == scorer_config_sha256(dict(first.config))
    assert first.as_spec() == CHOICE
    assert ScorerSpec.model_validate(first.as_spec()).model_dump() == CHOICE
    with pytest.raises(ScorerSpecError, match="config_sha256 mismatch"):
        resolve_scorer({**CHOICE, "config_sha256": "sha256:" + "0" * 64})


def test_exact_strips_edges_but_is_case_sensitive_and_preserves_internal_whitespace():
    result = score({"content": "  Alpha Beta\n"}, "Alpha Beta", EXACT)
    assert result["outcome"] == "correct" and result["parsed"] == "Alpha Beta"
    assert score({"content": "alpha Beta"}, "Alpha Beta", EXACT)["outcome"] == \
        "wrong_answer"
    assert score({"content": "Alpha  Beta"}, "Alpha Beta", EXACT)["outcome"] == \
        "wrong_answer"
    assert parse_output("\tvalue\n", EXACT) == "value"
    assert compare(parse_output(" x ", EXACT), validate_expected("x", EXACT), EXACT)


def test_exact_optional_normalization_is_explicit_and_ordered():
    normalized = scorer_spec("exact", {
        "normalize_whitespace": True,
        "case_sensitive": False,
    })
    result = score({"content": "  STRASSE\t\n  Berlin  "}, "Straße Berlin", normalized)
    assert result["outcome"] == "correct"
    assert result["parsed"] == "strasse berlin"
    assert normalized["config"] == {
        "normalize_whitespace": True, "case_sensitive": False,
    }


def test_exact_config_expected_and_output_are_strict():
    for config, message in (
        ({"trim": True}, "unknown scorer config"),
        ({"case_sensitive": "yes"}, "boolean"),
        ({"normalize_whitespace": 1}, "boolean"),
    ):
        with pytest.raises(ScorerSpecError, match=message):
            resolve_scorer(unchecked_spec("exact", config))
    for expected in (None, "", "   ", 1):
        with pytest.raises(ValueError, match="non-empty string"):
            validate_expected(expected, EXACT)
    invalid = score({"content": 7}, "7", EXACT)
    assert invalid["outcome"] == "invalid_format"
    assert invalid["details"]["parse_failure"] == {"code": "output_not_text"}


@pytest.mark.parametrize("config,message", [
    ({"labels": ["A"]}, "2-26"),
    ({"labels": ["A", "A"]}, "duplicates"),
    ({"labels": ["A", "b"]}, "uppercase ASCII"),
    ({"labels": ["AA", "B"]}, "uppercase ASCII"),
    ({"labels": ["A", "B"], "allow_bare_final_label": 1}, "boolean"),
])
def test_choice_config_and_expected_are_strict(config, message):
    spec = unchecked_spec("choice", config)
    with pytest.raises((ScorerSpecError, ValueError), match=message):
        resolve_scorer(spec)
    with pytest.raises(ValueError, match="configured label"):
        validate_expected(" Z ", CHOICE)


def test_choice_uses_only_one_strict_marker_on_the_final_nonempty_line():
    result = score(
        {"content": "A and B are discussed in the analysis.\n[ANSWER:C]\n\n"}, "C", CHOICE
    )
    assert result["outcome"] == "correct" and result["parsed"] == "C"
    assert score({"content": "The answer may be C."}, "C", CHOICE)["outcome"] == \
        "invalid_format"
    assert score({"content": "[ANSWER:C]\nexplanation"}, "C", CHOICE)["details"][
        "parse_failure"
    ]["code"] == "marker_not_final_line"
    assert score({"content": "[ANSWER:A]\n[ANSWER:C]"}, "C", CHOICE)["details"][
        "parse_failure"
    ]["code"] == "multiple_markers"
    assert score({"content": "[ANSWER:Z]"}, "C", CHOICE)["details"]["parse_failure"][
        "code"
    ] == "invalid_label"
    assert score({"content": " [ANSWER:C]"}, "C", CHOICE)["outcome"] == "invalid_format"


def test_choice_bare_label_requires_explicit_config_and_remains_final_line_only():
    bare = scorer_spec(
        "choice", {**CHOICE["config"], "allow_bare_final_label": True}
    )
    assert score({"content": "reasoning\nC"}, "C", bare)["outcome"] == "correct"
    assert score({"content": "reasoning C"}, "C", bare)["outcome"] == "invalid_format"
    assert score({"content": "C\ntrailing"}, "C", bare)["outcome"] == "invalid_format"


def test_choice_wrong_answer_is_distinct_from_invalid_format():
    wrong = score({"content": "[ANSWER:B]"}, "C", CHOICE)
    invalid = score({"content": "C"}, "C", CHOICE)
    assert wrong["outcome"] == "wrong_answer" and wrong["parsed"] == "B"
    assert invalid["outcome"] == "invalid_format" and "parsed" not in invalid


def test_numeric_exact_uses_decimal_and_normalizes_legal_formatting():
    for content in ("[ANSWER:1000]", "[ANSWER:1,000.00]", "work\n[ANSWER:+1000.0]"):
        result = score({"content": content}, "1000.000", NUMERIC)
        assert result["outcome"] == "correct" and result["parsed"] == "1000"
    assert score({"content": "[ANSWER:-0.00]"}, 0, NUMERIC)["outcome"] == "correct"
    assert score({"content": "[ANSWER:1000.01]"}, "1000", NUMERIC)["outcome"] == \
        "wrong_answer"
    with pytest.raises(ValueError, match="binary float"):
        validate_expected(0.1, NUMERIC)


@pytest.mark.parametrize("content,code", [
    ("42", "missing_marker"),
    ("[ANSWER:1e3]", "scientific_not_allowed"),
    ("[ANSWER:1/2]", "fraction_not_allowed"),
    ("[ANSWER:NaN]", "non_finite_not_allowed"),
    ("[ANSWER:Infinity]", "non_finite_not_allowed"),
    ("[ANSWER:12,34]", "invalid_number"),
    ("[ANSWER:.5]", "invalid_number"),
    ("[ANSWER:5.]", "invalid_number"),
    ("[ANSWER:1]\n[ANSWER:2]", "multiple_markers"),
])
def test_numeric_default_format_rejections_are_diagnostic(content, code):
    result = score({"content": content}, "1", NUMERIC)
    assert result["outcome"] == "invalid_format"
    assert result["details"]["parse_failure"] == {"code": code}
    assert content not in str(result["details"])


def test_numeric_tolerance_modes_are_explicit_exclusive_and_inclusive():
    absolute = scorer_spec(
        "numeric", {"mode": "absolute_tolerance", "absolute_tolerance": "0.05"}
    )
    relative = scorer_spec("numeric", {"relative_tolerance": "0.10"})
    assert score({"content": "[ANSWER:10.05]"}, "10", absolute)["outcome"] == "correct"
    assert score({"content": "[ANSWER:10.051]"}, "10", absolute)["outcome"] == \
        "wrong_answer"
    assert score({"content": "[ANSWER:90]"}, "100", relative)["outcome"] == "correct"
    assert score({"content": "[ANSWER:89.99]"}, "100", relative)["outcome"] == \
        "wrong_answer"
    assert score({"content": "[ANSWER:0]"}, "0", relative)["outcome"] == "correct"
    assert score({"content": "[ANSWER:0.001]"}, "0", relative)["outcome"] == \
        "wrong_answer"

    invalid_configs = [
        {"absolute_tolerance": "1", "relative_tolerance": "1"},
        {"mode": "exact", "absolute_tolerance": "1"},
        {"mode": "absolute_tolerance"},
        {"mode": "relative_tolerance", "relative_tolerance": "-0.1"},
        {"mode": "absolute_tolerance", "absolute_tolerance": 0.1},
    ]
    for config in invalid_configs:
        with pytest.raises(ScorerSpecError):
            resolve_scorer(unchecked_spec("numeric", config))


def test_numeric_optional_syntax_is_opt_in():
    enabled = scorer_spec("numeric", {
        "allow_scientific": True, "allow_fraction": True,
    })
    assert score({"content": "[ANSWER:1e3]"}, "1000", enabled)["outcome"] == "correct"
    assert score({"content": "[ANSWER:1/2]"}, "0.5", enabled)["outcome"] == "correct"
    no_sign = scorer_spec("numeric", {"allow_sign": False})
    assert score({"content": "[ANSWER:-1]"}, "1", no_sign)["outcome"] == "invalid_format"


def test_numeric_extreme_exponents_are_invalid_format_in_every_mode():
    specs = (
        scorer_spec("numeric", {"allow_scientific": True}),
        scorer_spec("numeric", {
            "allow_scientific": True,
            "absolute_tolerance": "0.01",
        }),
        scorer_spec("numeric", {
            "allow_scientific": True,
            "relative_tolerance": "0.01",
        }),
    )
    for spec in specs:
        result = score({"content": "[ANSWER:1e999999999]"}, "1", spec)
        assert result["outcome"] == "invalid_format"
        assert result["details"]["parse_failure"] == {"code": "exponent_out_of_bounds"}
        assert "parsed" not in result


def test_numeric_parser_enforces_all_resource_bounds_before_serialization():
    scientific = scorer_spec("numeric", {"allow_scientific": True})
    cases = (
        ("9" * 257, "number_too_long"),
        ("9" * 129, "too_many_significant_digits"),
        ("1e-1001", "exponent_out_of_bounds"),
        (f"{'9' * 128}e1000", "normalized_number_too_long"),
    )
    for token, code in cases:
        result = score({"content": f"[ANSWER:{token}]"}, "1", scientific)
        assert result["outcome"] == "invalid_format"
        assert result["details"]["parse_failure"] == {"code": code}
        assert token not in str(result["details"])


def test_numeric_fraction_checks_operands_before_bounded_precision_division():
    fraction = scorer_spec("numeric", {"allow_fraction": True, "allow_scientific": True})
    numerator = "9" * 128
    legal = score({"content": f"[ANSWER:{numerator}/1]"}, "1e128", fraction)
    assert legal["outcome"] == "wrong_answer"
    assert legal["parsed"] == numerator

    oversized = score({"content": f"[ANSWER:{'9' * 129}/1]"}, "1", fraction)
    assert oversized["outcome"] == "invalid_format"
    assert oversized["details"]["parse_failure"] == {
        "code": "too_many_significant_digits"
    }


def test_numeric_bounds_cover_config_expected_and_normal_scientific_semantics():
    invalid_configs = (
        ({"absolute_tolerance": "1e1000000"}, "exponent_out_of_bounds"),
        ({"relative_tolerance": "1e-1000000"}, "exponent_out_of_bounds"),
        ({"absolute_tolerance": "9" * 257}, "number_too_long"),
    )
    for config, code in invalid_configs:
        with pytest.raises(ScorerSpecError, match=code):
            resolve_scorer(unchecked_spec("numeric", config))

    scientific = scorer_spec("numeric", {"allow_scientific": True})
    with pytest.raises(ValueError, match="exponent_out_of_bounds"):
        validate_expected("1e999999999", scientific)
    with pytest.raises(ValueError, match="number_too_long"):
        validate_expected("9" * 257, scientific)
    with pytest.raises(ValueError, match="too_many_significant_digits"):
        validate_expected(Decimal("9" * 129), scientific)

    negative = score({"content": "[ANSWER:-1.25e2]"}, "-125.00", scientific)
    negative_zero = score({"content": "[ANSWER:-0e10]"}, 0, scientific)
    assert negative["outcome"] == "correct" and negative["parsed"] == "-125"
    assert negative_zero["outcome"] == "correct" and negative_zero["parsed"] == "0"


def test_json_equal_is_structural_with_decimal_normalization():
    expected = '{"name":"订单","amount":1.00,"items":[1,2],"ok":true}'
    content = '{ "ok": true, "items": [1.0, 2.00], "amount": 1, "name": "订单" }'
    result = score({"content": content}, expected, JSON_EQUAL)
    assert result["outcome"] == "correct"
    assert result["parsed"] == '{"amount":1,"items":[1,2],"name":"订单","ok":true}'

    reordered_array = '{"name":"订单","amount":1,"items":[2,1],"ok":true}'
    assert score({"content": reordered_array}, expected, JSON_EQUAL)["outcome"] == \
        "wrong_answer"
    bool_as_number = '{"name":"订单","amount":1,"items":[1,2],"ok":1}'
    assert score({"content": bool_as_number}, expected, JSON_EQUAL)["outcome"] == \
        "wrong_answer"


@pytest.mark.parametrize("content,code", [
    ('{"a":1,"a":2}', "duplicate_key"),
    ('{"n":NaN}', "non_finite_number"),
    ('{"n":Infinity}', "non_finite_number"),
    ('```json\n{"a":1}\n```', "invalid_json"),
    ('explanation {"a":1}', "invalid_json"),
    ('{"a":1} trailing', "invalid_json"),
])
def test_json_equal_rejects_ambiguous_or_non_json_output(content, code):
    result = score({"content": content}, '{"a":1}', JSON_EQUAL)
    assert result["outcome"] == "invalid_format"
    assert result["details"]["parse_failure"] == {"code": code}
    assert content not in str(result["details"])


def test_json_equal_rejects_bad_expected_at_validation_time():
    with pytest.raises(ValueError, match="duplicate_key"):
        validate_expected('{"a":1,"a":2}', JSON_EQUAL)
    with pytest.raises(ValueError, match="JSON string"):
        validate_expected({"a": 1}, JSON_EQUAL)
    with pytest.raises(ScorerSpecError, match="unknown scorer config"):
        resolve_scorer(unchecked_spec("json_equal", {"subset": True}))


def test_unified_parse_validate_compare_interface():
    parsed_choice = parse_output("reasoning\n[ANSWER:D]", CHOICE)
    assert parsed_choice == "D"
    assert compare(parsed_choice, validate_expected("D", CHOICE), CHOICE)

    parsed_number = parse_output("[ANSWER:2.00]", NUMERIC)
    expected_number = validate_expected("2", NUMERIC)
    assert parsed_number == Decimal("2.00")
    assert compare(parsed_number, expected_number, NUMERIC)

    parsed_json = parse_output('{"b":[2,1],"a":true}', JSON_EQUAL)
    expected_json = validate_expected('{"a":true,"b":[2.0,1]}', JSON_EQUAL)
    assert compare(parsed_json, expected_json, JSON_EQUAL)

    failure = parse_output("D", CHOICE)
    assert isinstance(failure, ParseFailure)
    assert compare(failure, "D", CHOICE) is False


def test_score_outcomes_and_evidence_are_bounded():
    correct = score({"content": "[ANSWER:A]"}, "A", CHOICE)
    assert correct["outcome"] == "correct"
    assert correct["attempted"] is True and correct["responded"] is True
    assert correct["judged"] is True and correct["passed"] is True
    assert correct["scorer"] == "choice" and correct["scorer_version"] == "1"
    assert correct["details"]["config_sha256"] == CHOICE["config_sha256"]

    failed = score({"error": {"class": "timeout", "message": "secret"}}, "A", CHOICE)
    assert failed["outcome"] == "call_failed" and failed["responded"] is False
    assert failed["judged"] is False and failed["error_class"] == "timeout"
    assert "secret" not in str(failed)

    no_expectation = score({"content": "unstructured output"}, None, CHOICE)
    assert no_expectation["outcome"] == "no_expectation"
    assert no_expectation["judged"] is False and no_expectation["responded"] is True

    skipped = score(None, "A", CHOICE, attempted=False)
    assert skipped["outcome"] == "not_attempted"
    assert skipped["attempted"] is False and skipped["judged"] is False


def test_mixed_specs_dispatch_without_shared_mutable_state():
    rows = [
        (CHOICE, "B", "[ANSWER:B]", "correct"),
        (EXACT, "phase seven", " phase seven\n", "correct"),
        (NUMERIC, "2", "[ANSWER:3]", "wrong_answer"),
        (JSON_EQUAL, '{"a":1}', '{"a":', "invalid_format"),
    ]
    outcomes = [score({"content": content}, expected, spec)["outcome"]
                for spec, expected, content, _ in rows]
    assert outcomes == [expected_outcome for *_, expected_outcome in rows]


def test_aggregate_scores_uses_v2_denominator_matrix():
    outcomes = [
        "correct", "wrong_answer", "invalid_format", "call_failed", "no_expectation",
        "not_attempted",
    ]
    aggregate = aggregate_scores([{"outcome": outcome} for outcome in outcomes], 6)
    assert {outcome: aggregate[outcome] for outcome in outcomes} == {
        outcome: 1 for outcome in outcomes
    }
    assert aggregate["selected"] == 6
    assert aggregate["judged"] == 3
    assert aggregate["attempted"] == aggregate["completed"] == 5
    assert aggregate["accuracy"] == pytest.approx(1 / 3)
    assert aggregate["coverage"] == pytest.approx(3 / 6)
    assert aggregate["completion"] == pytest.approx(5 / 6)
    assert aggregate["attempt_rate"] == pytest.approx(5 / 6)
    assert aggregate["format_failure_rate"] == pytest.approx(1 / 3)
    assert aggregate["denominators"] == {
        "accuracy": "judged", "coverage": "selected", "completion": "selected",
        "attempt_rate": "selected", "format_failure_rate": "judged",
    }
    rates = [aggregate[key] for key in (
        "accuracy", "coverage", "completion", "attempt_rate", "format_failure_rate"
    )]
    assert all(rate is None or 0 <= rate <= 1 for rate in rates)


def test_aggregate_counts_missing_selected_ids_as_not_attempted():
    aggregate = aggregate_scores([
        {"case_id": "a", "outcome": "correct"},
        {"case_id": "b", "outcome": "no_expectation"},
    ], selected_case_ids=["a", "b", "c"])
    assert aggregate["correct"] == 1 and aggregate["no_expectation"] == 1
    assert aggregate["not_attempted"] == 1
    assert aggregate["judged"] == 1
    assert aggregate["attempted"] == aggregate["completed"] == 2
    assert aggregate["coverage"] == pytest.approx(1 / 3)
    assert aggregate["completion"] == aggregate["attempt_rate"] == pytest.approx(2 / 3)


def test_aggregate_without_judged_or_selected_denominators_returns_none():
    unjudged = aggregate_scores([{"outcome": "call_failed"}], selected_count=2)
    assert unjudged["call_failed"] == 1 and unjudged["not_attempted"] == 1
    assert unjudged["judged"] == 0 and unjudged["attempted"] == 1
    assert unjudged["accuracy"] is None and unjudged["format_failure_rate"] is None
    assert unjudged["coverage"] == 0
    assert unjudged["completion"] == unjudged["attempt_rate"] == pytest.approx(1 / 2)

    empty = aggregate_scores([], selected_count=0)
    assert empty["selected"] == 0 and empty["not_attempted"] == 0
    assert all(empty[key] is None for key in (
        "accuracy", "coverage", "completion", "attempt_rate", "format_failure_rate"
    ))


@pytest.mark.parametrize("scores,selected,error", [
    ([{"case_id": "a", "outcome": "correct"},
      {"case_id": "a", "outcome": "wrong_answer"}], ["a", "b"], "duplicate"),
    ([{"case_id": "other", "outcome": "correct"}], ["a"], "not selected"),
    ([{"outcome": "correct"}, {"outcome": "wrong_answer"}], 1, "exceeds"),
    ([{"outcome": "unknown"}], 1, "unknown score outcome"),
    ([{"case_id": "a", "outcome": "correct"}, {"outcome": "wrong_answer"}], 2,
     "all include case_id"),
])
def test_aggregate_fails_closed_on_duplicate_unknown_or_overcomplete_rows(
        scores, selected, error):
    if isinstance(selected, list):
        with pytest.raises(ValueError, match=error):
            aggregate_scores(scores, selected_case_ids=selected)
    else:
        with pytest.raises(ValueError, match=error):
            aggregate_scores(scores, selected_count=selected)
