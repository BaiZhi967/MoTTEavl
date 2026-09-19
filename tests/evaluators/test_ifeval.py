"""Independent IFEval preview rules, aggregation, and official gate."""

from __future__ import annotations

import json

import pytest

import motte_eval.ifeval as ifeval_module
from motte_eval.ifeval import (
    IFEVAL_EVALUATOR_NOT_APPROVED,
    IFEVAL_FULL_CASE_COUNT,
    IFEVAL_SOURCE_STATUS,
    IFEvalCase,
    IFEvalOfficialGateError,
    RuleSpec,
    aggregate_ifeval,
    available_rules,
    build_rule_spec,
    evaluate_cases,
    evaluate_rule,
    require_official_ifeval_ready,
    resolve_rule,
    rule_config_sha256,
)


def _rule(rule_id: str, config: dict | None = None) -> RuleSpec:
    defaults = {
        "contains": {"value": "required"},
        "starts_with": {"value": "BEGIN"},
        "ends_with": {"value": "END"},
        "json_object": {},
        "max_words": {"max_words": 5},
    }
    return build_rule_spec(rule_id, defaults[rule_id] if config is None else config)


def _case(case_id: str, *rules: RuleSpec) -> IFEvalCase:
    return IFEvalCase(case_id=case_id, prompt=f"Prompt {case_id}", rules=tuple(rules))


def _approval() -> dict[str, str]:
    return {
        "source_id": "ifeval",
        "source_revision": "a" * 40,
        "license_approval_sha256": "sha256:" + "b" * 64,
        "evaluator_revision": "c" * 40,
        "parity_evidence_sha256": "sha256:" + "d" * 64,
    }


def test_registry_is_closed_versioned_and_config_hashes_normalized_config():
    assert available_rules() == (
        ("contains", "1"),
        ("ends_with", "1"),
        ("json_object", "1"),
        ("max_words", "1"),
        ("starts_with", "1"),
    )
    first = build_rule_spec("contains", {"value": "x"})
    equivalent = build_rule_spec("contains", {"case_sensitive": True, "value": "x"})
    changed = build_rule_spec("contains", {"value": "x", "case_sensitive": False})
    assert first.as_dict() == equivalent.as_dict()
    assert first.config_sha256 == rule_config_sha256(dict(first.config))
    assert first.config_sha256 != changed.config_sha256
    assert dict(resolve_rule(first).config) == {"value": "x", "case_sensitive": True}


def test_unknown_rule_version_config_and_hash_fail_closed():
    with pytest.raises(ValueError, match="unsupported"):
        build_rule_spec("regex", {})
    with pytest.raises(ValueError, match="unsupported"):
        build_rule_spec("contains", {"value": "x"}, version="2")
    with pytest.raises(ValueError, match="unknown rule config"):
        build_rule_spec("contains", {"value": "x", "pattern": ".*"})
    valid = _rule("contains")
    tampered = {**valid.as_dict(), "config_sha256": "sha256:" + "0" * 64}
    with pytest.raises(ValueError, match="config_sha256 mismatch"):
        resolve_rule(tampered)

    rogue = RuleSpec("regex", "1", {}, rule_config_sha256({}))
    with pytest.raises(ValueError, match="unsupported"):
        _case("missing-output", rogue)
    unnormalized_config = {"value": "x"}
    unnormalized = RuleSpec(
        "contains", "1", unnormalized_config, rule_config_sha256(unnormalized_config)
    )
    with pytest.raises(ValueError, match="mismatch after normalization"):
        _case("missing-output", unnormalized)


def test_case_contract_rejects_expected_extra_and_duplicate_rules():
    rule = _rule("contains")
    with pytest.raises(ValueError, match="schema mismatch"):
        evaluate_cases(
            [{"case_id": "c1", "prompt": "p", "rules": [rule], "expected": "secret"}],
            {},
        )
    with pytest.raises(ValueError, match="duplicate rule"):
        IFEvalCase(case_id="c1", prompt="p", rules=(rule, rule))


def test_contains_has_explicit_strict_and_loose_whitespace_behavior():
    evidence = evaluate_rule(
        "c1",
        _rule("contains", {"value": "Alpha Beta", "case_sensitive": True}),
        "Alpha \n Beta",
    )
    assert evidence["strict_passed"] is False
    assert evidence["loose_passed"] is True


def test_starts_with_and_ends_with_loose_mode_strip_outer_whitespace():
    starts = evaluate_rule("c1", _rule("starts_with"), "  BEGIN value")
    ends = evaluate_rule("c1", _rule("ends_with"), "value END\n")
    assert (starts["strict_passed"], starts["loose_passed"]) == (False, True)
    assert (ends["strict_passed"], ends["loose_passed"]) == (False, True)


def test_json_object_rule_is_strict_about_fences_duplicates_and_keys():
    rule = _rule("json_object", {"required_keys": ["answer"], "allow_extra_keys": False})
    fenced = evaluate_rule("c1", rule, '```json\n{"answer": 1}\n```')
    assert (fenced["strict_passed"], fenced["loose_passed"]) == (False, True)
    duplicate = evaluate_rule("c1", rule, '{"answer":1,"answer":2}')
    assert duplicate["strict_passed"] is False and duplicate["loose_passed"] is False
    extra = evaluate_rule("c1", rule, '{"answer":1,"extra":2}')
    assert extra["strict_passed"] is False and extra["loose_passed"] is False


def test_max_words_loose_mode_ignores_standalone_punctuation_tokens():
    evidence = evaluate_rule("c1", _rule("max_words", {"max_words": 2}), "one -- two")
    assert (evidence["strict_passed"], evidence["loose_passed"]) == (False, True)
    compact = evaluate_rule("c1", _rule("max_words", {"max_words": 1}), "one,two")
    assert (compact["strict_passed"], compact["loose_passed"]) == (True, True)
    with pytest.raises(ValueError, match="positive integer"):
        build_rule_spec("max_words", {"max_words": True})


def test_rule_evidence_contains_only_bounded_normalized_output_summary():
    secret = "sensitive full model output required"
    evidence = evaluate_rule("c1", _rule("contains"), secret)
    summary = evidence["normalized_output"]
    assert summary == {
        "sha256": summary["sha256"],
        "utf8_bytes": len(secret.encode("utf-8")),
        "characters": len(secret),
        "lines": 1,
        "words": 5,
        "empty": False,
    }
    assert summary["sha256"].startswith("sha256:")
    assert secret not in json.dumps(evidence)


def test_case_prompt_results_are_all_rules_in_each_mode():
    case = _case("c1", _rule("contains"), _rule("ends_with"))
    report = evaluate_cases([case], {"c1": "required but not the ending"})
    row = report["cases"][0]
    assert row["outcome"] == "judged"
    assert row["prompt_strict"] is False and row["prompt_loose"] is False
    assert [rule["strict_passed"] for rule in row["rules"]] == [True, False]


def test_missing_case_and_rule_are_explicit_not_attempted_and_keep_denominators():
    first = _case("c1", _rule("contains"), _rule("ends_with"))
    second = _case("c2", _rule("starts_with"))
    one_rule = evaluate_rule("c1", first.rules[0], "required")
    report = aggregate_ifeval([first, second], [one_rule])

    assert report["cases"][0]["outcome"] == "incomplete"
    assert report["cases"][0]["rules"][1]["outcome"] == "not_attempted"
    assert report["cases"][1]["outcome"] == "not_attempted"
    assert report["cases"][1]["rules"][0]["outcome"] == "not_attempted"
    instruction = report["aggregate"]["instruction_level_strict"]
    prompt = report["aggregate"]["prompt_level_strict"]
    assert instruction == {
        "selected": 3,
        "judged": 1,
        "passed": 1,
        "rate": 1.0,
        "coverage": pytest.approx(1 / 3),
    }
    assert prompt == {"selected": 2, "judged": 0, "passed": 0, "rate": None, "coverage": 0.0}


def test_aggregate_exposes_four_independent_metrics_and_never_direct_accuracy():
    case = _case(
        "c1", _rule("contains", {"value": "Alpha"}), _rule("starts_with", {"value": "Alpha"})
    )
    missing = _case("c2", _rule("ends_with"))
    report = evaluate_cases([case, missing], {"c1": " Alpha \n Beta"})
    aggregate = report["aggregate"]

    assert set(aggregate) == {
        "prompt_level_strict",
        "prompt_level_loose",
        "instruction_level_strict",
        "instruction_level_loose",
    }
    assert aggregate["prompt_level_strict"] == {
        "selected": 2,
        "judged": 1,
        "passed": 0,
        "rate": 0.0,
        "coverage": 0.5,
    }
    assert aggregate["prompt_level_loose"] == {
        "selected": 2,
        "judged": 1,
        "passed": 1,
        "rate": 1.0,
        "coverage": 0.5,
    }
    assert aggregate["instruction_level_strict"] == {
        "selected": 3,
        "judged": 2,
        "passed": 1,
        "rate": 0.5,
        "coverage": pytest.approx(2 / 3),
    }
    assert aggregate["instruction_level_loose"] == {
        "selected": 3,
        "judged": 2,
        "passed": 2,
        "rate": 1.0,
        "coverage": pytest.approx(2 / 3),
    }
    assert "accuracy" not in json.dumps(report)


def test_duplicate_cases_evidence_and_unknown_membership_fail_closed():
    case = _case("c1", _rule("contains"))
    evidence = evaluate_rule("c1", case.rules[0], "required")
    with pytest.raises(ValueError, match="duplicate IFEval case_id"):
        evaluate_cases([case, case], {})
    with pytest.raises(ValueError, match="duplicate rule evidence"):
        aggregate_ifeval([case], [evidence, evidence])
    with pytest.raises(ValueError, match="not selected"):
        evaluate_cases([case], {"unknown": "value"})
    with pytest.raises(ValueError, match="not selected"):
        aggregate_ifeval([case], [{**evidence, "case_id": "unknown"}])


def test_synthetic_full_541_report_remains_preview_and_pending():
    rule = _rule("contains", {"value": "ok"})
    cases = [_case(f"ifeval-{index:03d}", rule) for index in range(IFEVAL_FULL_CASE_COUNT)]
    outputs = {case.case_id: "ok" for case in cases}
    report = evaluate_cases(cases, outputs)

    assert len(report["cases"]) == 541
    assert report["full_case_count"] == 541
    assert report["source_status"] == IFEVAL_SOURCE_STATUS == "pending"
    assert report["official"] is False
    for metric in report["aggregate"].values():
        assert metric == {
            "selected": 541,
            "judged": 541,
            "passed": 541,
            "rate": 1.0,
            "coverage": 1.0,
        }


def test_official_gate_defaults_malformed_and_untrusted_verifiers_fail_closed():
    with pytest.raises(IFEvalOfficialGateError) as missing:
        require_official_ifeval_ready()
    assert missing.value.code == IFEVAL_EVALUATOR_NOT_APPROVED
    with pytest.raises(IFEvalOfficialGateError):
        require_official_ifeval_ready(_approval())
    with pytest.raises(IFEvalOfficialGateError):
        require_official_ifeval_ready(_approval(), verifier=lambda evidence: False)
    with pytest.raises(IFEvalOfficialGateError):
        require_official_ifeval_ready(_approval(), verifier=lambda evidence: 1)
    with pytest.raises(IFEvalOfficialGateError):
        require_official_ifeval_ready(
            {**_approval(), "evaluator_revision": "main"}, verifier=lambda evidence: True
        )

    def broken_verifier(evidence):
        raise RuntimeError("secret verifier failure")

    with pytest.raises(IFEvalOfficialGateError, match=IFEVAL_EVALUATOR_NOT_APPROVED):
        require_official_ifeval_ready(_approval(), verifier=broken_verifier)


def test_official_gate_accepts_only_injected_trusted_evidence_and_returns_frozen_descriptor():
    seen = {}

    def trusted_verifier(evidence):
        seen.update(evidence)
        with pytest.raises(TypeError):
            evidence["source_id"] = "tampered"
        return True

    assert not hasattr(ifeval_module, "_READY_ATTESTATION")
    assert not hasattr(ifeval_module, "_IFEvalReadyDescriptor")
    assert not hasattr(ifeval_module, "_official_gate_factory")
    assert require_official_ifeval_ready.__closure__ is None
    descriptor = require_official_ifeval_ready(_approval(), verifier=trusted_verifier)
    assert seen == _approval()
    assert descriptor.status == "ready" and descriptor.full_case_count == 541
    assert descriptor.descriptor_sha256.startswith("sha256:")
    with pytest.raises(IFEvalOfficialGateError):
        type(descriptor)(**descriptor.as_dict(), _gate_attestation=object())

    post_init = type(descriptor).__post_init__
    closure = dict(zip(post_init.__code__.co_freevars, post_init.__closure__, strict=True))
    real_token = closure["gate_attestation"].cell_contents
    forged = {**descriptor.as_dict(), "evaluator_revision": "e" * 40}
    forged_payload = {key: value for key, value in forged.items() if key != "descriptor_sha256"}
    forged["descriptor_sha256"] = rule_config_sha256(forged_payload)
    with pytest.raises(IFEvalOfficialGateError):
        type(descriptor)(**forged, _gate_attestation=real_token)
    with pytest.raises((AttributeError, TypeError)):
        descriptor.status = "forged"
