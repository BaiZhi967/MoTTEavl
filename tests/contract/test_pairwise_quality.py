"""Pure software contracts only; no Judge, provider or ledger execution."""

from copy import deepcopy
import importlib

import pytest
from pydantic import ValidationError

from motte_contracts.comparison import ReportSnapshot
from motte_contracts.hashing import canonical_hash
from motte_contracts.metrics import METRIC_REGISTRY, METRIC_REGISTRY_VERSION, lookup_metric


def contracts():
    return importlib.import_module("motte_contracts.pairwise_quality")


def role(case="case-1", pair="pair-1"):
    return {
        "case_id": case,
        "pair_id": pair,
        "challenger_candidate_id": "attempt:z-" + pair,
        "reference_candidate_id": "attempt:a-" + pair,
        "challenger_attempt_id": "z-" + pair,
        "reference_attempt_id": "a-" + pair,
        "candidate_input_sha256": {
            "attempt:z-" + pair: "sha256:" + "1" * 64,
            "attempt:a-" + pair: "sha256:" + "2" * 64,
        },
    }


def quality_payload(values=(1.0, 0.5, 0.0)):
    roles = [role("case-" + str(i), "pair-" + str(i)) for i in range(len(values))]
    valid = sum(value is not None for value in values)
    complete = bool(values) and valid == len(values)
    return {
        "source_pass_id": "source-pass",
        "job_id": "subject-job",
        "plan_sha256": "sha256:" + "3" * 64,
        "ledger_sha256": "sha256:" + "4" * 64,
        "roles_sha256": canonical_hash(roles),
        "roles": roles,
        "planned_pairs": len(values),
        "valid_pairs": valid,
        "planned_calls": len(values),
        "settled_calls": valid,
        "pair_values": {item["pair_id"]: value for item, value in zip(roles, values, strict=True)},
        "value": sum(values) / len(values) if complete else None,
        "coverage": valid / len(values) if values else None,
        "reasons": [] if complete else ["incomplete_pairwise_evidence"],
    }


def old_report():
    return dict(
        snapshot_id="old-snapshot",
        ref={
            "run_id": "old-run",
            "scoring_pass_id": "old-pass",
            "report_schema": "report-v1",
            "evidence_hash": "sha256:" + "1" * 64,
        },
        created_at="2026-09-30T00:00:00Z",
        suite="gsm8k",
        denominator="selected_cases",
        counts={
            "selected": 1,
            "attempted": 1,
            "judged": 1,
            "scored": 1,
            "call_failed": 0,
            "unknown": 0,
            "not_attempted": 0,
            "needs_review": 0,
            "no_expectation": 0,
        },
        coverage=1,
        metric_values={"accuracy": 1},
        metric_registry_version="metric-registry@1",
    )


def pairwise_report():
    module = importlib.import_module("motte_contracts.comparison")
    quality = contracts().PairwiseQualitySnapshot.seal(quality_payload())
    raw = old_report()
    raw.update(
        metric_registry_version="metric-registry@2",
        pairwise_quality=quality,
        metric_values={"accuracy": None, "pairwise_challenger_score": quality.value},
    )
    raw["ref"].update(scoring_pass_id="source-pass", report_schema="report-pairwise-v1")
    return module.PairwiseReportSnapshot.model_validate(raw)


def test_roles_cannot_follow_presentation_or_lexical_order():
    model = contracts().PairwiseRoleBinding
    original = model.model_validate(role())
    assert original.challenger_candidate_id > original.reference_candidate_id
    changed = original.model_dump(mode="json")
    changed["challenger_candidate_id"], changed["reference_candidate_id"] = (
        changed["reference_candidate_id"],
        changed["challenger_candidate_id"],
    )
    changed["challenger_attempt_id"], changed["reference_attempt_id"] = (
        changed["reference_attempt_id"],
        changed["challenger_attempt_id"],
    )
    reversed_roles = model.model_validate(changed)
    assert canonical_hash(original.model_dump(mode="json")) != canonical_hash(
        reversed_roles.model_dump(mode="json")
    )
    assert model.model_validate(original.model_dump(mode="json")) == original
    # Presentation is not a role selector in the pure binding contract.
    with pytest.raises(ValidationError):
        model.model_validate({**role(), "presentation_order": ["A", "B"]})


@pytest.mark.parametrize(
    "corruption",
    [
        "same_candidate",
        "same_attempt",
        "missing_digest",
        "extra_digest",
        "bad_digest",
        "empty_case",
    ],
)
def test_invalid_or_unselected_role_identity_is_rejected(corruption):
    raw = role()
    if corruption == "same_candidate":
        raw["reference_candidate_id"] = raw["challenger_candidate_id"]
    elif corruption == "same_attempt":
        raw["reference_attempt_id"] = raw["challenger_attempt_id"]
    elif corruption == "missing_digest":
        raw["candidate_input_sha256"].pop(raw["reference_candidate_id"])
    elif corruption == "extra_digest":
        raw["candidate_input_sha256"]["unselected"] = "sha256:" + "0" * 64
    elif corruption == "bad_digest":
        raw["candidate_input_sha256"][raw["reference_candidate_id"]] = "unbound"
    else:
        raw["case_id"] = ""
    with pytest.raises(ValidationError):
        contracts().PairwiseRoleBinding.model_validate(raw)


@pytest.mark.parametrize("identity", ["source_pass_id", "job_id", "plan_sha256", "ledger_sha256"])
def test_pairwise_snapshot_identity_covers_all_sources(identity):
    model = contracts().PairwiseQualitySnapshot
    first = model.seal(quality_payload())
    raw = first.model_dump(mode="json")
    raw[identity] = "sha256:" + "f" * 64 if identity.endswith("sha256") else "another-source"
    with pytest.raises(ValidationError):
        model.model_validate(raw)
    changed = model.seal(raw)
    assert changed.content_sha256 != first.content_sha256
    assert first.content_sha256 == canonical_hash(
        first.model_dump(mode="json", exclude={"content_sha256"})
    )


def test_role_reversal_and_frozen_candidate_input_change_snapshot_identity():
    model = contracts().PairwiseQualitySnapshot
    raw = quality_payload()
    first = model.seal(raw)
    binding = raw["roles"][0]
    binding["challenger_candidate_id"], binding["reference_candidate_id"] = (
        binding["reference_candidate_id"],
        binding["challenger_candidate_id"],
    )
    binding["challenger_attempt_id"], binding["reference_attempt_id"] = (
        binding["reference_attempt_id"],
        binding["challenger_attempt_id"],
    )
    raw["roles_sha256"] = canonical_hash(raw["roles"])
    reversed_roles = model.seal(raw)
    assert first.content_sha256 != reversed_roles.content_sha256
    raw["roles"][0]["candidate_input_sha256"][binding["challenger_candidate_id"]] = (
        "sha256:" + "e" * 64
    )
    raw["roles_sha256"] = canonical_hash(raw["roles"])
    assert model.seal(raw).content_sha256 != reversed_roles.content_sha256


@pytest.mark.parametrize("duplicate", ["case", "pair"])
def test_one_pair_per_selected_case(duplicate):
    raw = quality_payload()
    field = "case_id" if duplicate == "case" else "pair_id"
    raw["roles"][1][field] = raw["roles"][0][field]
    raw["roles_sha256"] = canonical_hash(raw["roles"])
    with pytest.raises(ValidationError):
        contracts().PairwiseQualitySnapshot.seal(raw)


@pytest.mark.parametrize("identity", ["candidate", "attempt"])
def test_different_cases_cannot_reuse_the_same_saved_role_identity(identity):
    raw = quality_payload()
    first, second = raw["roles"][:2]
    if identity == "candidate":
        digest = second["candidate_input_sha256"].pop(second["challenger_candidate_id"])
        second["challenger_candidate_id"] = first["challenger_candidate_id"]
        second["candidate_input_sha256"][second["challenger_candidate_id"]] = digest
    else:
        second["challenger_attempt_id"] = first["challenger_attempt_id"]
    raw["roles_sha256"] = canonical_hash(raw["roles"])
    with pytest.raises(ValidationError):
        contracts().PairwiseQualitySnapshot.seal(raw)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 1.0),
        ("metric_id", "accuracy"),
        ("metric_version", "2"),
        ("algorithm", "pairwise-quality@2"),
        ("qualified", True),
    ],
)
def test_metric_identity_is_fixed_and_cannot_be_asserted_by_flags(field, value):
    with pytest.raises(ValidationError):
        contracts().PairwiseQualitySnapshot.seal({**quality_payload(), field: value})


@pytest.mark.parametrize(
    "field", ["planned_pairs", "valid_pairs", "planned_calls", "settled_calls"]
)
@pytest.mark.parametrize("value", [-1, True, 1.5, "3"])
def test_pair_counts_are_strict_nonnegative_independent_units(field, value):
    raw = quality_payload()
    raw[field] = value
    with pytest.raises(ValidationError):
        contracts().PairwiseQualitySnapshot.seal(raw)


@pytest.mark.parametrize(
    "corruption",
    [
        "pair_count",
        "valid_count",
        "call_count",
        "settled_count",
        "coverage",
        "roles_digest",
        "pair_keys",
        "partial_value",
        "unequal_pair_weight",
    ],
)
def test_snapshot_inconsistencies_cannot_assert_available_quality(corruption):
    raw = quality_payload()
    if corruption == "pair_count":
        raw["planned_pairs"] = 4
    elif corruption == "valid_count":
        raw["valid_pairs"] = 2
    elif corruption == "call_count":
        raw["planned_calls"] = 2
    elif corruption == "settled_count":
        raw["settled_calls"] = 4
    elif corruption == "coverage":
        raw["coverage"] = 0.5
    elif corruption == "roles_digest":
        raw["roles_sha256"] = "sha256:" + "0" * 64
    elif corruption == "pair_keys":
        raw["pair_values"].pop("pair-0")
    elif corruption == "partial_value":
        raw["settled_calls"] = 2
    else:
        raw["value"] = 0.8
    with pytest.raises(ValidationError):
        contracts().PairwiseQualitySnapshot.seal(raw)


@pytest.mark.parametrize("field", ["value", "coverage", "pair_value"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), -0.1, 1.1, True])
def test_quality_ratios_are_finite_nonboolean(field, value):
    raw = quality_payload()
    if field == "pair_value":
        raw["pair_values"]["pair-0"] = value
    else:
        raw[field] = value
    with pytest.raises(ValidationError):
        contracts().PairwiseQualitySnapshot.seal(raw)


def test_partial_and_empty_plans_preserve_pair_denominator():
    model = contracts().PairwiseQualitySnapshot
    partial = model.seal(quality_payload((1.0, None, 0.0)))
    assert (partial.planned_pairs, partial.valid_pairs, partial.coverage, partial.value) == (
        3,
        2,
        2 / 3,
        None,
    )
    empty = model.seal(quality_payload(()))
    assert empty.planned_pairs == 0 and empty.coverage is None and empty.value is None
    complete = quality_payload()
    complete.update(planned_calls=5, settled_calls=5)
    assert model.seal(complete).value == 0.5  # Calls never replace the pair denominator.


def test_valid_pairs_cannot_exceed_their_settled_planned_calls():
    raw = quality_payload((1.0, None, None))
    raw["settled_calls"] = 0
    with pytest.raises(ValidationError):
        contracts().PairwiseQualitySnapshot.seal(raw)


def test_snapshot_serialization_is_detached_and_roundtrips():
    model = contracts().PairwiseQualitySnapshot
    raw = quality_payload()
    original = deepcopy(raw)
    snapshot = model.seal(raw)
    raw["roles"][0]["candidate_input_sha256"]["attempt:z-pair-0"] = "sha256:" + "f" * 64
    raw["pair_values"]["pair-0"] = None
    assert snapshot == model.seal(original)
    output = snapshot.model_dump(mode="json")
    output["roles"][0]["candidate_input_sha256"]["attempt:z-pair-0"] = "sha256:" + "f" * 64
    output["pair_values"]["pair-0"] = None
    assert model.model_validate_json(snapshot.model_dump_json()) == snapshot
    assert snapshot.content_sha256 == snapshot.compute_content_sha256()


def test_roleless_historical_snapshot_is_readable_but_never_complete():
    model = contracts().PairwiseQualitySnapshot
    raw = quality_payload((None, None, None))
    raw.update(
        roles=[],
        roles_sha256=canonical_hash([]),
        settled_calls=3,
        reasons=["missing_pairwise_roles"],
    )
    historical = model.seal(raw)
    assert historical.planned_pairs == 3 and historical.valid_pairs == 0
    assert historical.coverage == 0 and historical.value is None and historical.roles == ()
    assert set(historical.pair_values) == {"pair-0", "pair-1", "pair-2"}
    with pytest.raises(ValidationError):
        model.seal({**raw, "value": 0.5})
    with pytest.raises(ValidationError):
        model.seal({**raw, "reasons": []})


@pytest.mark.parametrize(
    "field,reason",
    [("plan_sha256", "plan_digest_unavailable"), ("ledger_sha256", "ledger_digest_unavailable")],
)
def test_unknown_source_digest_is_explicit_unavailable_not_a_dummy_hash(field, reason):
    model = contracts().PairwiseQualitySnapshot
    raw = quality_payload((None, None, None))
    raw.update({field: None, "reasons": [reason]})
    snapshot = model.seal(raw)
    assert getattr(snapshot, field) is None and snapshot.valid_pairs == 0 and snapshot.value is None
    with pytest.raises(ValidationError):
        model.seal({**raw, "reasons": []})
    complete = quality_payload()
    complete.update({field: None, "reasons": [reason]})
    with pytest.raises(ValidationError):
        model.seal(complete)


def test_historical_missing_job_identity_is_explicitly_unavailable():
    model = contracts().PairwiseQualitySnapshot
    raw = quality_payload((None, None, None))
    raw.update(job_id=None, reasons=["subject_job_unavailable"])
    snapshot = model.seal(raw)
    assert snapshot.job_id is None and snapshot.value is None and snapshot.valid_pairs == 0
    with pytest.raises(ValidationError):
        model.seal({**raw, "reasons": []})
    with pytest.raises(ValidationError):
        model.seal({**quality_payload(), "job_id": None, "reasons": ["subject_job_unavailable"]})
    with pytest.raises(ValidationError):
        model.seal({**raw, "source_pass_id": None})


def test_mutated_nested_model_instances_are_revalidated_and_detached():
    module = contracts()
    binding = module.PairwiseRoleBinding.model_validate(role("case-0", "pair-0"))
    raw = quality_payload((1.0,))
    raw["roles"] = [binding]
    snapshot = module.PairwiseQualitySnapshot.seal(raw)
    binding.candidate_input_sha256[binding.challenger_candidate_id] = "sha256:" + "f" * 64
    assert (
        snapshot.roles[0].candidate_input_sha256[snapshot.roles[0].challenger_candidate_id]
        == "sha256:" + "1" * 64
    )
    corrupted = snapshot.model_copy(update={"pair_values": {"pair-0": 0.0}})
    with pytest.raises(ValidationError):
        module.PairwiseQualitySnapshot.model_validate(corrupted)


@pytest.mark.parametrize("readback", ["attribute", "iteration", "dict", "model_dump"])
@pytest.mark.parametrize("kind", ["role", "quality", "report"])
def test_all_public_mutable_readbacks_preserve_frozen_content(kind, readback):
    if kind == "role":
        model = contracts().PairwiseRoleBinding.model_validate(role())
        field = "candidate_input_sha256"
        key, value = model.challenger_candidate_id, "sha256:" + "f" * 64
    elif kind == "quality":
        model = contracts().PairwiseQualitySnapshot.seal(quality_payload())
        field, key, value = "pair_values", "pair-0", 0.0
    else:
        model = pairwise_report()
        field, key, value = "metric_values", "accuracy", 1.0
    before = model.model_dump_json()
    if readback == "attribute":
        mutable = getattr(model, field)
    elif readback == "iteration":
        mutable = next(value for name, value in model if name == field)
    elif readback == "dict":
        mutable = dict(model)[field]
    else:
        mutable = model.model_dump()[field]
    mutable[key] = value
    assert model.model_dump_json() == before


@pytest.mark.parametrize("readback", ["attribute", "iteration", "dict"])
def test_report_and_quality_nested_role_readbacks_are_detached(readback):
    report = pairwise_report()
    before = report.model_dump_json()
    quality = (
        report.pairwise_quality if readback == "attribute" else dict(report)["pairwise_quality"]
    )
    roles = quality.roles if readback == "attribute" else dict(quality)["roles"]
    binding = roles[0]
    digests = (
        binding.candidate_input_sha256
        if readback == "attribute"
        else dict(binding)["candidate_input_sha256"]
    )
    digests[binding.challenger_candidate_id] = "sha256:" + "f" * 64
    assert report.model_dump_json() == before
    assert type(report).model_validate(report) == report


@pytest.mark.parametrize("corruption", ["source_digest", "content_digest", "nested_role"])
def test_existing_report_instances_revalidate_every_nested_evidence_hash(corruption):
    report = pairwise_report()
    quality = report.pairwise_quality
    if corruption == "source_digest":
        quality = quality.model_copy(update={"ledger_sha256": "sha256:" + "f" * 64})
    elif corruption == "content_digest":
        quality = quality.model_copy(update={"content_sha256": "sha256:" + "f" * 64})
    else:
        binding = quality.roles[0]
        invalid = binding.model_copy(
            update={"candidate_input_sha256": {"foreign": "sha256:" + "f" * 64}}
        )
        with pytest.raises(ValidationError):
            type(binding).model_validate(invalid)
        quality = quality.model_copy(update={"roles": (invalid, *quality.roles[1:])})
    invalid_report = report.model_copy(update={"pairwise_quality": quality})
    with pytest.raises(ValidationError):
        type(report).model_validate(invalid_report)


@pytest.mark.parametrize(
    "metric", ["accuracy", "judged_accuracy", "valid_trial_pass_rate", "cost.per_success_usd"]
)
@pytest.mark.parametrize("qualified", [False, True])
def test_pairwise_reports_reject_every_registered_boolean_success_alias(metric, qualified):
    report = pairwise_report()
    key = metric + "@1" if qualified else metric
    assert lookup_metric(key, registry_version="metric-registry@2") is not None
    raw = report.model_dump(mode="json")
    raw["metric_values"][key] = 1.0
    with pytest.raises(ValidationError):
        type(report).model_validate(raw)


def test_typed_report_has_bound_quality_without_changing_boolean_counts():
    report = pairwise_report()
    assert report.counts == ReportSnapshot.model_validate(old_report()).counts
    assert report.pairwise_quality.planned_pairs == 3 and report.counts["selected"] == 1
    assert report.metric_values["accuracy"] is None
    assert type(report).model_validate_json(report.model_dump_json()) == report
    assert "pairwise_quality" in type(report).model_json_schema()["required"]


@pytest.mark.parametrize(
    "corruption",
    ["schema", "registry", "source", "boolean_accuracy", "bare_pair_value", "missing_quality"],
)
def test_pairwise_report_rejects_detached_or_laundered_projection(corruption):
    report = pairwise_report()
    raw = report.model_dump(mode="json")
    if corruption == "schema":
        raw["ref"]["report_schema"] = "report-v1"
    elif corruption == "registry":
        raw["metric_registry_version"] = "metric-registry@1"
    elif corruption == "source":
        raw["ref"]["scoring_pass_id"] = "different-pass"
    elif corruption == "boolean_accuracy":
        raw["metric_values"]["accuracy"] = 1.0
    elif corruption == "bare_pair_value":
        raw["metric_values"]["pairwise_challenger_score"] = 1.0
    else:
        raw.pop("pairwise_quality")
    with pytest.raises(ValidationError):
        type(report).model_validate(raw)


def test_registry_v1_is_unchanged():
    assert METRIC_REGISTRY_VERSION == "metric-registry@1"
    assert (
        canonical_hash(
            {key: value.model_dump(mode="json") for key, value in METRIC_REGISTRY.items()}
        )
        == "sha256:9cae469df1b5b14849bf98eb984f0d20c316a37d5e338cf23c788acc704733c8"
    )
    assert lookup_metric("pairwise_challenger_score@1") is None
    assert (
        lookup_metric("pairwise_challenger_score@1", registry_version="metric-registry@1") is None
    )
    new = lookup_metric("pairwise_challenger_score@1", registry_version="metric-registry@2")
    assert (new.unit, new.direction, new.aggregation, new.denominator, new.missing_policy) == (
        "ratio",
        "gte",
        "mean",
        "planned_pairs",
        "fail_closed",
    )
    for key, definition in METRIC_REGISTRY.items():
        assert lookup_metric(key, registry_version="metric-registry@2") == definition
    assert (
        lookup_metric("pairwise_challenger_score@2", registry_version="metric-registry@2") is None
    )
    assert lookup_metric("accuracy@2") is None
    assert lookup_metric("accuracy", registry_version="unknown") is None


def test_old_report_serialization_and_golden_hash_are_unchanged():
    snapshot = ReportSnapshot.model_validate(old_report())
    assert "pairwise_quality" not in snapshot.model_dump(mode="json")
    assert (
        canonical_hash(snapshot.model_dump(mode="json"))
        == "sha256:00a537f21d97c4fa3fe290e18585fdf4ac4f349f79fba5753e805e4422ee9e8d"
    )
    assert (
        snapshot.compute_snapshot_id()
        == "sha256:a966adb175356e4cb194cdd5fa672dbe53d7a9720d0effa22ddcac7c78e852aa"
    )
