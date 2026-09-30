"""Software-only fixtures for durable provenance, never real human/model acceptance."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest
from pydantic import TypeAdapter, ValidationError

from motte_contracts.identity import canonical_sha256
from motte_eval.calibration import (
    CalibrationSample, HumanReviewRequired, build_calibration_report, build_calibration_set,
    candidate_sample, qualification_record, review_sample, sample_content_sha256,
)
from motte_eval.calibration_records import (
    CalibrationCandidate, CalibrationExecution, CalibrationImport, CalibrationPair,
    CalibrationQualificationSource, CalibrationRef, CalibrationReportRecord,
    CalibrationSourceBinding, CalibrationVersion, ExpectedCalibrationOutcome,
    HumanReviewInput, HumanReviewRecord, PairwiseLabel, QualificationBinding,
    calibration_execution_id, calibration_review_id, review_calibration_version,
    verify_review_chain,
)
from motte_eval.judge import build_judge_spec
from motte_eval.rubrics import calibration_policy_sha256, policy_for
from motte_sdk.scoring_jobs import JudgeProviderSnapshot

TIME = "2026-09-30T01:00:00+00:00"
LATER = "2026-09-30T02:00:00+00:00"
HASH = "sha256:" + "a" * 64


def spec_fixture():
    return build_judge_spec(
        judge_profile_id="software-fixture", model="scripted-model",
        rubric_id="answer-quality", rubric_version="1", mode="pairwise",
        budget={"max_calls": 4},
    )


def sample_fixture(*, source="human"):
    spec = spec_fixture()
    raw = dict(
        sample_id="sample-1", kind="clear_pass", source=source,
        candidate_output="Software-only human-input fixture, not actual human review",
        rubric_id=spec.rubric_id, rubric_version=spec.rubric_version, model=spec.model,
        judge_spec_sha256=spec.spec_sha256,
    )
    draft = CalibrationSample.model_construct(**raw, content_sha256=HASH)
    return CalibrationSample.model_validate({**draft.model_dump(),
                                             "content_sha256": sample_content_sha256(draft)})


def pair_fixture():
    return CalibrationPair(candidates=[
        CalibrationCandidate(candidate_id="stable-left", content="First software fixture",
                             evidence_allowlist=["event:fixture-left"]),
        CalibrationCandidate(candidate_id="stable-right", content="Second software fixture",
                             evidence_allowlist=["event:fixture-right"]),
    ])


def import_fixture(*, source="human", calibration_id="fixture-calibration"):
    spec = spec_fixture()
    calibration = build_calibration_set(
        calibration_id, "1", rubric_id=spec.rubric_id, rubric_version=spec.rubric_version,
        model=spec.model, samples=[sample_fixture(source=source)],
        judge_spec_sha256=spec.spec_sha256, config={"judge_spec": spec.model_dump(mode="json")},
    )
    return CalibrationImport(calibration=calibration, pairs={"sample-1": pair_fixture()})


def review_fixture(**changes):
    raw = dict(
        sample_id="sample-1", annotator="software-fixture-annotator",
        reviewer="software-fixture-reviewer", reviewed_at=TIME,
        reason="Software protocol fixture only; no human acceptance claim",
        expected_outcome={"kind": "scored"},
        pairwise_gold={key: {"kind": "candidate", "candidate_id": "stable-left"}
                       for key in spec_fixture().criteria},
    )
    raw.update(changes)
    return HumanReviewInput(**raw)


def reviewed_fixture():
    parent = import_fixture().to_version()
    child, reviews = review_calibration_version(
        parent, new_version="2", reviews=[review_fixture()], recorded_at=TIME,
    )
    return parent, child, reviews


def test_import_cannot_assert_reviewed_or_qualified():
    raw = import_fixture().model_dump(mode="json")
    for key in ("qualified", "gate_eligible", "review_ids", "parent_ref", "report"):
        with pytest.raises(ValidationError):
            CalibrationImport.model_validate({**raw, key: True})
    sample = review_sample(sample_fixture(), annotator="fixture", reviewer="fixture",
                           expected_criteria={}, expected_status="scored", reviewed_at=TIME)
    calibration = build_calibration_set(
        "fixture-calibration", "1", rubric_id=sample.rubric_id,
        rubric_version=sample.rubric_version, model=sample.model, samples=[sample],
        judge_spec_sha256=spec_fixture().spec_sha256,
        config={"judge_spec": spec_fixture().model_dump(mode="json")},
    )
    with pytest.raises(ValidationError, match="unreviewed"):
        CalibrationImport(calibration=calibration, pairs={"sample-1": pair_fixture()})
    assert import_fixture(source="synthetic_candidate").to_version().review_ids == []


def test_review_rejects_synthetic_source():
    sample = candidate_sample("s", "clear_pass", "fixture", rubric_id="answer-quality",
                              rubric_version="1", model="scripted-model")
    with pytest.raises(HumanReviewRequired, match="synthetic"):
        review_sample(sample, annotator="fixture", reviewer="fixture", reviewed_at=TIME,
                      expected_criteria={"task_completion": True})
    with pytest.raises(HumanReviewRequired, match="synthetic"):
        review_calibration_version(import_fixture(source="synthetic_candidate").to_version(),
                                   new_version="2", reviews=[review_fixture()], recorded_at=TIME)


def test_pairwise_gold_has_no_boolean_coercion():
    assert PairwiseLabel(kind="tie", candidate_id=None).candidate_id is None
    for raw in (True, False, {"kind": "candidate", "candidate_id": None},
                {"kind": "tie", "candidate_id": "stable-left"},
                {"kind": "candidate", "candidate_id": True},
                {"kind": "candidate", "candidate_id": " "}):
        with pytest.raises(ValidationError):
            PairwiseLabel.model_validate(raw)
    parent = import_fixture().to_version()
    for gold in ({}, {"task_completion": {"kind": "tie"}},
                 {key: {"kind": "candidate", "candidate_id": "outsider"}
                  for key in spec_fixture().criteria}):
        with pytest.raises(ValueError):
            review_calibration_version(parent, new_version="2",
                                       reviews=[review_fixture(pairwise_gold=gold)], recorded_at=TIME)


def test_non_scored_gold_is_explicit_and_not_tie():
    adapter = TypeAdapter(ExpectedCalibrationOutcome)
    for status in ("failed", "indeterminate", "timeout", "ok", None):
        with pytest.raises(ValidationError):
            adapter.validate_python({"kind": "non_scored", "status": status})
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "scored", "status": "missing_evidence"})
    parent = import_fixture().to_version()
    for status in ("missing_evidence", "refused", "malformed", "forged_evidence",
                   "missing_criterion"):
        review = review_fixture(pairwise_gold={},
                                expected_outcome={"kind": "non_scored", "status": status})
        child, records = review_calibration_version(
            parent, new_version="2", reviews=[review], recorded_at=TIME,
        )
        assert child.pairwise_gold == {}
        assert child.expected_outcomes["sample-1"].status == status
        assert child.calibration.samples[0].expected_criteria == {}
        assert child.calibration.samples[0].expected_status == (
            "insufficient_evidence" if status == "missing_evidence" else "evaluator_error"
        )
        verify_review_chain(parent, child, records)
    with pytest.raises(ValidationError):
        review_fixture(expected_outcome={"kind": "non_scored", "status": "missing_evidence"})


def test_digest_covers_pairs_gold_and_review_sources():
    parent, child, _ = reviewed_fixture()
    assert child.calibration.content_sha256 != child.content_sha256
    assert child.reference == CalibrationRef(calibration_id="fixture-calibration", version="2",
                                             content_sha256=child.content_sha256)
    raw = child.model_dump(mode="json")
    variants = []
    for candidate in (0, 1):
        changed = deepcopy(raw)
        changed["pairs"]["sample-1"]["candidates"][candidate]["content"] += " changed"
        variants.append(changed)
    changed = deepcopy(raw)
    changed["pairs"]["sample-1"]["candidates"][0]["evidence_allowlist"].append("event:more")
    variants.append(changed)
    changed = deepcopy(raw)
    changed["pairwise_gold"]["sample-1"][spec_fixture().criteria[0]] = {"kind": "tie", "candidate_id": None}
    variants.append(changed)
    for field in ("calibration_id", "version", "content_sha256"):
        changed = deepcopy(raw)
        changed["parent_ref"][field] = HASH if field == "content_sha256" else "different"
        variants.append(changed)
    changed = deepcopy(raw)
    changed["review_ids"] = ["calreview-" + "b" * 64]
    variants.append(changed)
    for changed in variants:
        with pytest.raises(ValidationError):
            CalibrationVersion.model_validate(changed)
    changed = parent.model_dump(mode="json")
    changed["calibration"]["samples"][0]["candidate_output"] = "tampered nested sample"
    with pytest.raises(ValidationError, match="sample content_sha256"):
        CalibrationVersion.seal(changed)


def test_forbidden_owners_digests_and_pair_shapes():
    pair = pair_fixture().model_dump(mode="json")
    for field in ("run_id", "job_id", "owner_kind", "calibration_job_id"):
        with pytest.raises(ValidationError):
            CalibrationPair.model_validate({**pair, field: "forged"})
        altered = deepcopy(pair)
        altered["candidates"][0][field] = "forged"
        with pytest.raises(ValidationError):
            CalibrationPair.model_validate(altered)
    pair["candidates"][1]["candidate_id"] = pair["candidates"][0]["candidate_id"]
    with pytest.raises(ValidationError, match="distinct"):
        CalibrationPair.model_validate(pair)
    for digest in ("x" * 71, "sha256:" + "g" * 64):
        with pytest.raises(ValidationError):
            CalibrationRef(calibration_id="cal", version="1", content_sha256=digest)
    raw = import_fixture().model_dump(mode="json")
    raw["pairs"] = {"unknown-sample": raw["pairs"]["sample-1"]}
    with pytest.raises(ValidationError, match="sample"):
        CalibrationImport.model_validate(raw)


@pytest.mark.parametrize("changes", [
    {"reason": " "}, {"annotator": " "}, {"reviewer": " "},
    {"reviewed_at": "2026-09-30T01:00:00"}, {"reviewed_at": "now"},
    {"expected_criteria": {"task_completion": 1}}, {"credentials": "forbidden"},
])
def test_human_input_is_declared_strict_provenance(changes):
    with pytest.raises(ValidationError):
        review_fixture(**changes)


def test_review_is_content_addressed_audited_and_receipt_time_independent():
    parent, child, reviews = reviewed_fixture()
    same, later_reviews = review_calibration_version(parent, new_version="2",
                                                     reviews=[review_fixture()], recorded_at=LATER)
    assert child == same
    assert reviews[0].review_id == later_reviews[0].review_id
    assert reviews[0].recorded_at != later_reviews[0].recorded_at
    assert reviews[0].review_id == calibration_review_id(parent.reference, review_fixture())
    verify_review_chain(parent, child, reviews)
    for field in ("before_sample_sha256", "after_sample_sha256"):
        raw = reviews[0].model_dump(mode="json")
        raw[field] = HASH
        bad = HumanReviewRecord.model_validate(raw)
        with pytest.raises(ValueError, match="digest"):
            verify_review_chain(parent, child, [bad])
    for field in ("reason", "reviewer", "annotator", "reviewed_at"):
        updated = review_fixture(**{field: LATER if field == "reviewed_at" else "other"})
        assert calibration_review_id(parent.reference, updated) != reviews[0].review_id
    with pytest.raises(ValueError):
        verify_review_chain(parent, child, [])


def execution_fixture():
    version = reviewed_fixture()[1]
    return CalibrationExecution.seal(dict(
        request_key="fixture-namespace/request-1", request_fingerprint=HASH,
        version=version, spec=spec_fixture(),
        provider_snapshot=JudgeProviderSnapshot.minimal("scripted-model").model_dump(mode="json"),
        policy=policy_for("answer-quality", "1"),
        plan=[{"call_id": "call-1", "sample_id": "sample-1", "child_job_id": "child-1"}],
        child_job_ids=["child-1"], recorded_at=TIME,
    ))


def test_execution_freezes_snapshots_plan_and_namespaced_request_identity():
    execution = execution_fixture()
    assert execution.execution_id == calibration_execution_id(execution.request_key)
    assert calibration_execution_id("other/request-1") != execution.execution_id
    assert execution.plan_sha256 == canonical_sha256(execution.plan)
    assert execution.source_binding.calibration_content_sha256 == execution.version.content_sha256
    for path, value in ((["version", "content_sha256"], HASH),
                        (["spec", "model"], "other"),
                        (["provider_snapshot", "model"], "other"),
                        (["policy", "max_error_rate"], .99),
                        (["plan_sha256"], HASH), (["execution_id"], "wrong")):
        raw = execution.model_dump(mode="json")
        target = raw
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(ValidationError):
            CalibrationExecution.model_validate(raw)


def report_fixture():
    execution = execution_fixture()
    report = build_calibration_report(execution.version.calibration, observed={})
    return CalibrationReportRecord.seal(dict(
        execution_id=execution.execution_id, ledger_sha256=HASH,
        source=execution.source_binding, report=report, pairwise_confusion=[], recorded_at=TIME,
    )), execution


def test_report_and_qualification_bind_both_hash_domains_and_ignore_receipt_times():
    report, execution = report_fixture()
    report.verify_execution(execution)
    assert report.report.calibration_sha256 == execution.version.calibration.content_sha256
    assert report.source.calibration_content_sha256 == execution.version.content_sha256
    later = CalibrationReportRecord.seal({**report.model_dump(), "recorded_at": LATER})
    assert later.report_id == report.report_id and later.content_sha256 == report.content_sha256
    qualification = CalibrationQualificationSource.seal(dict(
        qualification=qualification_record(report.report),
        binding={**report.source.model_dump(), "report_id": report.report_id,
                 "report_sha256": report.content_sha256}, recorded_at=TIME,
    ))
    qualification.verify_report(report)
    assert qualification.binding.qualification_id == qualification.qualification.qualification_id
    changed = CalibrationQualificationSource.seal({**qualification.model_dump(), "recorded_at": LATER})
    assert changed.qualification.qualification_id == qualification.qualification.qualification_id
    for field in QualificationBinding.model_fields:
        raw = qualification.binding.model_dump()
        raw.pop(field)
        with pytest.raises(ValidationError):
            QualificationBinding.model_validate(raw)
    raw = report.model_dump(mode="json")
    raw["report"]["calibration_sha256"] = execution.version.content_sha256
    swapped = CalibrationReportRecord.seal(raw)
    with pytest.raises(ValueError, match="set digest"):
        swapped.verify_execution(execution)


def test_qualification_source_rejects_cross_report_and_policy_identity():
    report, _ = report_fixture()
    raw = report.source.model_dump()
    for field in CalibrationSourceBinding.model_fields:
        changed = dict(raw)
        changed[field] = HASH if "sha256" in field else "other"
        assert canonical_sha256(changed) != canonical_sha256(raw)
    assert report.source.policy_sha256 == calibration_policy_sha256(policy_for("answer-quality", "1"))
    with pytest.raises(ValidationError):
        CalibrationReportRecord.seal({**report.model_dump(), "source": {**raw, "model": "other"}})


@pytest.mark.parametrize("field,value", [
    ("credentials", "never-persist-this-fixture"),
    ("api_key", "never-persist-this-fixture"),
    ("unexpected_provider_field", True),
    ("schema_version", 2),
])
def test_execution_rejects_secret_or_unknown_provider_snapshot_fields(field, value):
    raw = execution_fixture().model_dump(mode="json")
    snapshot = raw["provider_snapshot"]
    snapshot[field] = value
    snapshot["snapshot_sha256"] = canonical_sha256({
        key: item for key, item in snapshot.items() if key not in {"snapshot_sha256", "frozen_at"}
    })
    with pytest.raises(ValidationError):
        CalibrationExecution.seal(raw)


def test_all_qualification_identity_components_change_the_real_content_address():
    report, _ = report_fixture()
    source = CalibrationQualificationSource.seal(dict(
        qualification=qualification_record(report.report),
        binding={**report.source.model_dump(), "report_id": report.report_id,
                 "report_sha256": report.content_sha256}, recorded_at=TIME,
    ))
    for field in QualificationBinding.model_fields:
        raw = source.model_dump(mode="json")
        if field == "qualification_id":
            raw["binding"][field] = "forged-id"
            with pytest.raises(ValidationError, match="qualification ID"):
                CalibrationQualificationSource.model_validate(raw)
            continue
        new_value = "sha256:" + "e" * 64 if "sha256" in field else "changed-identity"
        raw["binding"][field] = new_value
        if field in raw["qualification"]:
            raw["qualification"][field] = new_value
        changed = CalibrationQualificationSource.seal(raw)
        assert changed.content_sha256 != source.content_sha256, field
        assert changed.binding.qualification_id != source.binding.qualification_id, field
        with pytest.raises(ValueError):
            changed.verify_report(report)


def test_review_replay_checks_child_ref_complete_input_and_preserves_parent():
    parent, child, reviews = reviewed_fixture()
    original = parent.model_dump(mode="json")
    raw = reviews[0].model_dump(mode="json")
    raw["child_ref"]["content_sha256"] = HASH
    with pytest.raises(ValueError, match="child reference"):
        verify_review_chain(parent, child, [HumanReviewRecord.model_validate(raw)])
    raw = reviews[0].model_dump(mode="json")
    raw["review"]["reason"] += " altered"
    with pytest.raises(ValidationError, match="complete human input"):
        HumanReviewRecord.model_validate(raw)
    assert parent.model_dump(mode="json") == original
    assert not parent.calibration.samples[0].is_human_reviewed
    revised, revised_reviews = review_calibration_version(
        child, new_version="3", reviews=[review_fixture(pairwise_gold={
            key: {"kind": "tie"} for key in spec_fixture().criteria
        })], recorded_at=LATER,
    )
    verify_review_chain(child, revised, revised_reviews)
    assert revised.review_ids[:len(child.review_ids)] == child.review_ids


@pytest.mark.parametrize("method", ["seal", "readback"])
@pytest.mark.parametrize("frozen_at", [
    {"api_key": "software-fixture-secret"},
    [{"authorization": "software-fixture-secret"}],
])
def test_provider_audit_fields_cannot_hide_secrets(method, frozen_at):
    raw = execution_fixture().model_dump(mode="json")
    provider_digest = raw["provider_snapshot"]["snapshot_sha256"]
    raw["provider_snapshot"]["frozen_at"] = frozen_at
    # Model a valid outer digest, so a stale outer hash cannot mask the read-side bug.
    raw["content_sha256"] = canonical_sha256({
        key: value for key, value in raw.items() if key not in {"content_sha256", "recorded_at"}
    })
    assert raw["provider_snapshot"]["snapshot_sha256"] == provider_digest
    with pytest.raises(ValidationError, match="credentials or secrets"):
        if method == "seal":
            CalibrationExecution.seal(raw)
        else:
            CalibrationExecution.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize("method", ["seal", "readback"])
@pytest.mark.parametrize("frozen_at", [{}, [], 12, True])
def test_provider_audit_time_matches_existing_string_or_none_contract(method, frozen_at):
    raw = execution_fixture().model_dump(mode="json")
    raw["provider_snapshot"]["frozen_at"] = frozen_at
    raw["content_sha256"] = canonical_sha256({
        key: value for key, value in raw.items() if key not in {"content_sha256", "recorded_at"}
    })
    with pytest.raises(ValidationError):
        JudgeProviderSnapshot.model_validate(raw["provider_snapshot"])
    with pytest.raises(ValidationError, match="valid string"):
        if method == "seal":
            CalibrationExecution.seal(raw)
        else:
            CalibrationExecution.model_validate_json(json.dumps(raw))


def test_provider_audit_time_preserves_sdk_compatible_receipt_independent_identity():
    raw = execution_fixture().model_dump(mode="json")
    expected_digest = raw["provider_snapshot"]["snapshot_sha256"]
    for frozen_at in (None, TIME, LATER):
        raw["provider_snapshot"]["frozen_at"] = frozen_at
        sdk_snapshot = JudgeProviderSnapshot.model_validate(raw["provider_snapshot"])
        execution = CalibrationExecution.seal(raw)
        reloaded = CalibrationExecution.model_validate_json(execution.model_dump_json())
        assert reloaded == execution
        assert execution.provider_snapshot["frozen_at"] == frozen_at
        assert execution.provider_snapshot["snapshot_sha256"] == sdk_snapshot.snapshot_sha256
        assert execution.provider_snapshot["snapshot_sha256"] == expected_digest
