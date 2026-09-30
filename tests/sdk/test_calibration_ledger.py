"""Scripted software fixtures only, never real human review or model acceptance."""
from copy import deepcopy
import importlib
import importlib.util
import json

import pytest

from motte_eval.calibration_records import CalibrationImport, HumanReviewInput
from motte_sdk.judge_calibrations import JudgeCalibrationService
from motte_sdk.scoring_jobs import ScoringJobService
from motte_storage.calibrations import CalibrationConflict, CalibrationCorrupt
from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore
from tests.sdk.test_judge_calibrations import (
    TIME, calibration_environment, import_data, run_request,
)


def ledger_module():
    assert importlib.util.find_spec("motte_sdk.calibration_ledger") is not None, (
        "authoritative calibration ledger reconstruction is missing"
    )
    return importlib.import_module("motte_sdk.calibration_ledger")


class ScriptedCalibrationTransport:
    """In-memory wire responses consumed by the real frozen adapter; no HTTP."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def post_json_detailed(self, path, body):
        from motte_provider.transport import TransportOutcome
        self.calls.append(deepcopy(body))
        return TransportOutcome(
            url="memory://software-calibration", path=path, request_body=body,
            status=200, attempts=1, response_body={
                "id": f"resp-{len(self.calls)}", "model": body["model"],
                "choices": [{"message": {"content": self.responses[len(self.calls) - 1]},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 30},
            },
        )


def completed_environment(tmp_path, *, memory=True, approved_missing=True, execute=True, store=None):
    """30 labelled software fixtures, six honest missing-evidence cases, real Worker ledger."""
    store = store or (InMemoryRunStore() if memory else SQLiteRunStore(tmp_path / "ledger.db"))
    service, scoring, resources, provider, factory, _, _ = calibration_environment(
        tmp_path, count=1, store=store,
    )
    request = run_request(request_key="ledger-software-only")
    imported, reviews = import_data(request, count=30)
    raw = imported.model_dump(mode="json")
    raw["calibration"]["calibration_id"] = "ledger-software-only"
    from motte_eval.calibration import build_calibration_set
    raw["calibration"].pop("content_sha256")
    raw["calibration"].pop("schema_version")
    raw["calibration"]["samples"] = imported.calibration.samples
    raw["calibration"] = build_calibration_set(**raw["calibration"]).model_dump(mode="json")
    for sample in imported.calibration.samples:
        if sample.kind == "missing_evidence":
            for side in raw["pairs"][sample.sample_id]["candidates"]:
                side["evidence_allowlist"] = []
    parent = service.import_version(CalibrationImport.model_validate(raw))
    fixed_reviews = []
    for review in reviews:
        value = review.model_dump(mode="json")
        kind = next(s.kind for s in imported.calibration.samples if s.sample_id == review.sample_id)
        if kind == "missing_evidence" and approved_missing:
            value.update(expected_outcome={"kind": "non_scored", "status": "missing_evidence"},
                         pairwise_gold={})
        elif kind == "borderline":
            value["pairwise_gold"] = {key: {"kind": "tie"} for key in value["pairwise_gold"]}
        fixed_reviews.append(HumanReviewInput.model_validate(value))
    version = service.review(parent.reference, new_version="reviewed", reviews=fixed_reviews)
    execution = service.submit(version.reference, request)
    samples = {sample.sample_id: sample for sample in version.calibration.samples}
    responses = []
    for plan in execution.plan:
        kind = samples[plan["sample_id"]].kind
        winner = "tie" if kind in {"borderline", "missing_evidence"} else (
            "A" if plan["presentation_order"][0] == "a-left" else "B"
        )
        responses.append(json.dumps({"winner": winner, "criteria": [
            {"criterion_id": key, "preference": winner, "reason": "scripted software fixture",
             "evidence": [] if kind == "missing_evidence" else ["event:10", "event:20"]}
            for key in version.spec.criteria
        ]}))
    from motte_provider.openai_compatible import OpenAICompatibleProvider
    from motte_provider.pricing import parse_price_table
    from motte_sdk.scoring_jobs import JudgeProviderSnapshot
    snapshot = JudgeProviderSnapshot.model_validate(execution.provider_snapshot)
    assert snapshot.adapter_id == OpenAICompatibleProvider.kind
    provider = OpenAICompatibleProvider(
        ScriptedCalibrationTransport(responses), snapshot.model,
        parameters=snapshot.parameters, max_output_tokens=snapshot.max_output_tokens,
        price_table=parse_price_table(snapshot.price_table),
        identity_policy=snapshot.identity_policy or "report_only",
        identity_aliases=snapshot.identity_aliases,
        identity_alias_version=snapshot.identity_alias_version,
    )
    factory.provider = provider
    if execute:
        from apps.worker.motte_worker.runtime import WorkerLoop
        from motte_sdk.service import RunService
        worker = WorkerLoop(RunService(store), scoring_jobs=scoring)
        for _ in execution.child_job_ids:
            assert worker.claim_and_execute()["status"] == "completed"
    return service, scoring, provider, version, request, execution


def first_call(scoring, execution):
    job = scoring.jobs._rows[execution.child_job_ids[0]]
    call = job["calls"][0]
    invocation = scoring.store.invocations._rows[call["invocation_id"]]
    return job, call, invocation


@pytest.mark.parametrize("memory", [True, False])
def test_report_reconstructs_from_raw_ledger(tmp_path, memory):
    ledger = ledger_module()
    service, scoring, provider, version, _, execution = completed_environment(tmp_path, memory=memory)
    before = len(provider.calls)
    record = ledger.reconstruct_calibration_report(service.store, execution.execution_id)
    assert record.report.gate_eligible and record.report.qualified
    assert record.report.call_count == 120
    assert record.report.per_criterion == []
    assert record.report.generated_at is None
    assert record.source == execution.source_binding
    assert record.report.disagreement_rate == 0
    assert record.report.error_rate == record.report.refusal_rate == 0
    assert record.report.missing_evidence_rate == .20
    assert len(record.pairwise_calls) == 120
    assert all(item.call.criteria == {} for item in record.pairwise_calls)
    assert len(provider.calls) == before


def test_false_qualified_payload_is_ignored(tmp_path):
    ledger = ledger_module()
    service, scoring, _, _, _, execution = completed_environment(tmp_path)
    baseline = ledger.reconstruct_calibration_report(service.store, execution.execution_id)
    for job in scoring.jobs._rows.values():
        job["result"] = {"qualified": True, "observed": {"all": "fabricated"}}
        job["qualified"] = True
        job["cost_total_usd"] = 0
    rebuilt = ledger.reconstruct_calibration_report(service.store, execution.execution_id)
    assert rebuilt.content_sha256 == baseline.content_sha256
    assert rebuilt.report.cost["total_usd"] == .0216  # frozen $1/$2 per million, 120/30 tokens


@pytest.mark.parametrize("mutation", [
    "missing_invocation", "wrong_job", "wrong_owner", "wrong_input", "wrong_provider",
    "wrong_spec", "wrong_order", "duplicate_call", "extra_call", "missing_call",
    "response_content", "response_id", "reservation", "unsafe_retry",
])
def test_cross_job_duplicate_or_missing_invocation_is_ineligible(tmp_path, mutation):
    ledger = ledger_module()
    service, scoring, _, _, _, execution = completed_environment(tmp_path)
    job, call, invocation = first_call(scoring, execution)
    if mutation == "missing_invocation":
        service.store.invocations._rows.pop(invocation["id"])
    elif mutation == "wrong_job":
        invocation["job_id"] = execution.child_job_ids[1]
    elif mutation == "wrong_owner":
        invocation["owner"]["sample_id"] = "someone-else"
    elif mutation in {"wrong_input", "wrong_provider", "wrong_spec"}:
        field = {"wrong_input": "input_sha256", "wrong_provider": "provider_snapshot_sha256",
                 "wrong_spec": "judge_spec_sha256"}[mutation]
        invocation["request_summary"][field] = "sha256:" + "0" * 64
    elif mutation == "wrong_order":
        invocation["request_summary"]["presentation_order"].reverse()
    elif mutation == "duplicate_call":
        job["calls"].append(deepcopy(call))
    elif mutation == "extra_call":
        job["calls"].append({**deepcopy(call), "call_id": "unplanned-success"})
    elif mutation == "missing_call":
        job["calls"].pop(0)
    elif mutation == "response_content":
        invocation["result_summary"]["raw_response"]["content"] = "forged"
    elif mutation == "response_id":
        invocation["result_summary"]["response_id"] = "other-response"
    elif mutation == "reservation":
        call["reservation"]["completion_tokens"] += 1
    elif mutation == "unsafe_retry":
        invocation["result_summary"]["attempts"] = 2
    record = ledger.reconstruct_calibration_report(service.store, execution.execution_id)
    assert not record.report.gate_eligible
    assert any(reason.startswith("ledger:") for reason in record.report.reasons)
    assert record.report.repeat_stability["samples"] == 30
    assert record.report.position_swap["pairs"] == 30


def test_swapped_tie_and_criteria_are_distinct(tmp_path):
    ledger = ledger_module()
    service, scoring, _, _, _, execution = completed_environment(tmp_path)
    report = ledger.reconstruct_calibration_report(service.store, execution.execution_id)
    tied = [row for row in report.pairwise_calls if row.call.sample_id == "sample-02"]
    assert all(row.winner.kind == "tie" for row in tied)
    assert all(all(label.kind == "tie" for label in row.preferences.values()) for row in tied)
    assert report.report.position_swap["consistent"] == 30
    # Same overall winner, different one-criterion preference: must not count as stable.
    plan = next(p for p in execution.plan if p["sample_id"] == "sample-02" and p["call_kind"] == "repeat")
    job = scoring.jobs._rows[plan["child_job_id"]]
    call = next(c for c in job["calls"] if c["call_id"] == plan["call_id"])
    inv = service.store.invocations._rows[call["invocation_id"]]
    content = json.loads(call["raw_response"]["content"])
    content["criteria"][0]["preference"] = "A"
    content = json.dumps(content)
    call["raw_response"]["content"] = inv["result_summary"]["raw_response"]["content"] = content
    changed = ledger.reconstruct_calibration_report(service.store, execution.execution_id)
    assert changed.report.repeat_stability["rate"] == 29 / 30
    assert not changed.report.gate_eligible


def test_expected_missing_evidence_keeps_denominator_and_rate(tmp_path):
    ledger = ledger_module()
    service, _, _, _, _, execution = completed_environment(tmp_path)
    report = ledger.reconstruct_calibration_report(service.store, execution.execution_id)
    for field, count in [("repeat_stability", "samples"), ("position_swap", "pairs")]:
        data = getattr(report.report, field)
        assert data[count] == 30 and data["rate"] == 1
        assert len(data["consistent_expected_non_scored"]) == 6
    absent = [row for row in report.pairwise_calls if row.call.sample_id == "sample-03"]
    assert all(row.call.status == "missing_evidence" and row.winner is None for row in absent)
    assert report.report.missing_evidence_rate == .20


def test_unapproved_non_scored_consistency_cannot_qualify(tmp_path):
    ledger = ledger_module()
    service, _, _, _, _, execution = completed_environment(tmp_path, approved_missing=False)
    report = ledger.reconstruct_calibration_report(service.store, execution.execution_id)
    assert not report.report.gate_eligible
    assert report.report.position_swap["rate"] == .8
    assert report.report.position_swap["pairs"] == 30
    assert report.report.missing_evidence_rate == .20


@pytest.mark.parametrize("outcome", ["failed", "indeterminate"])
def test_partial_failed_or_unknown_call_is_ineligible(tmp_path, outcome):
    ledger_module()
    service, scoring, _, _, _, execution = completed_environment(tmp_path)
    job, call, inv = first_call(scoring, execution)
    call["outcome"] = inv["outcome"] = outcome
    call["cost_usd"] = inv["result_summary"]["cost_usd"] = None
    job["status"] = outcome if outcome == "indeterminate" else "failed"
    report = service.publish_report(execution.execution_id)
    assert not report.report.gate_eligible
    assert report.report.cost["total_usd"] is None
    assert not report.report.cost["known"]
    assert service.store.calibrations.get_report(report.report_id) == report


def test_publish_requires_terminal_children_and_names_missing_source(tmp_path):
    ledger_module()
    service, scoring, _, _, _, execution = completed_environment(tmp_path, execute=False)
    with pytest.raises(CalibrationConflict, match="terminal"):
        service.publish_report(execution.execution_id)
    assert service.store.calibrations.list_reports(execution.execution_id) == []
    scoring.jobs._rows.pop(execution.child_job_ids[-1])
    with pytest.raises(CalibrationCorrupt, match="CALIBRATION_SOURCE_CORRUPT"):
        service.publish_report(execution.execution_id)
    assert not service.store.calibrations._rows["reports"]


def test_report_replay_after_restart_has_identical_digest(tmp_path):
    ledger = ledger_module()
    service, scoring, provider, version, _, execution = completed_environment(tmp_path, memory=False)
    first = service.publish_report(execution.execution_id)
    reopened = SQLiteRunStore(tmp_path / "ledger.db")
    restarted = JudgeCalibrationService(reopened, None, scoring_jobs=ScoringJobService(reopened),
                                       clock=lambda: "2026-10-01T01:00:00+00:00")
    second = restarted.publish_report(execution.execution_id)
    assert first == second
    qualifications = [r for r in reopened.calibrations.iter_records() if "qualification" in r]
    assert len(qualifications) == 1
    from motte_eval.calibration_records import QualificationBinding
    binding = QualificationBinding.model_validate(qualifications[0]["binding"])
    assert ledger.verify_qualification_source(reopened, binding)["gate_eligible"]
    assert len(provider.calls) == 120


def test_qualification_reconstructs_instead_of_trusting_stored_flags(tmp_path):
    ledger = ledger_module()
    service, scoring, _, _, _, execution = completed_environment(tmp_path)
    report = service.publish_report(execution.execution_id)
    source = next(r for r in service.store.calibrations.iter_records() if "qualification" in r)
    from motte_eval.calibration_records import QualificationBinding
    binding = QualificationBinding.model_validate(source["binding"])
    assert ledger.verify_qualification_source(service.store, binding)["gate_eligible"]
    _, _, inv = first_call(scoring, execution)
    inv["request_summary"]["input_sha256"] = "sha256:" + "f" * 64
    verified = ledger.verify_qualification_source(service.store, binding)
    assert not verified["gate_eligible"] and verified["experimental"]
    assert report.report.qualified  # the old flags are still true, but are no authority


def test_publication_holds_guard_during_reconstruction_and_insert(tmp_path, monkeypatch):
    ledger_module()
    service, _, _, _, _, execution = completed_environment(tmp_path, memory=False)
    from motte_storage.maintenance import begin_maintenance, MaintenanceConflict
    real_get = service.store.calibrations.get_execution
    real_publish = service.store.calibrations.publish_report
    observed = []
    def check(stage):
        with pytest.raises(MaintenanceConflict):
            begin_maintenance(service.store, reason="gc")
        observed.append(stage)
    def read(*args, **kwargs):
        check("reconstruction")
        return real_get(*args, **kwargs)
    def write(*args, **kwargs):
        check("publication")
        return real_publish(*args, **kwargs)
    monkeypatch.setattr(service.store.calibrations, "get_execution", read)
    monkeypatch.setattr(service.store.calibrations, "publish_report", write)
    service.publish_report(execution.execution_id)
    assert observed == ["reconstruction", "publication"]


@pytest.mark.parametrize("index", ["namespace", "child"])
def test_extra_execution_owned_invocation_cannot_hide_under_undeclared_child(tmp_path, index):
    ledger = ledger_module()
    service, scoring, _, _, _, execution = completed_environment(tmp_path)
    _, _, invocation = first_call(scoring, execution)
    extra = deepcopy(invocation)
    extra.update(id="unplanned-invocation", job_id="unlisted-child")
    if index == "child":
        extra.update(job_id=execution.child_job_ids[0], run_id="calibration:wrong-execution")
    extra["result_summary"]["response_id"] = "unplanned-response"
    extra["result_summary"]["raw_response"]["response_id"] = "unplanned-response"
    service.store.invocations._rows[extra["id"]] = extra
    report = ledger.reconstruct_calibration_report(service.store, execution.execution_id)
    assert not report.report.gate_eligible
    assert report.report.coverage["ledger"]["observed_invocations"] == 121
    assert report.report.cost["total_usd"] is None


def test_legacy_missing_allowance_never_becomes_authorized_zero(tmp_path):
    ledger_module()
    service, _, _, _, _, execution = completed_environment(tmp_path)
    from motte_eval.calibration_records import CalibrationExecution
    from motte_contracts.hashing import canonical_json
    legacy = CalibrationExecution.seal(execution.model_dump(mode="json", exclude={"allowance"}))
    rows = service.store.calibrations._rows["executions"]
    key = (execution.execution_id,)
    rows[key] = (*rows[key][:-2], legacy.content_sha256, canonical_json(legacy.model_dump(mode="json")))
    report = service.publish_report(execution.execution_id)
    assert not report.report.gate_eligible
    assert report.report.coverage["ledger"]["allowance"] is None
    assert any("missing authorization" in reason for reason in report.report.reasons)
    assert not service.store.calibrations._rows["qualifications"]


def test_self_consistent_forged_qualification_flags_fail_reconstruction(tmp_path):
    ledger = ledger_module()
    service, _, _, _, _, execution = completed_environment(tmp_path, approved_missing=False)
    reconstructed = ledger.reconstruct_calibration_report(service.store, execution.execution_id)
    assert not reconstructed.report.qualified
    from motte_eval.calibration_records import CalibrationReportRecord
    raw = reconstructed.model_dump(mode="json")
    raw["report"].update(qualified=True, gate_eligible=True, experimental=False, reasons=[])
    forged = CalibrationReportRecord.seal(raw)
    source = ledger.qualification_source(forged, recorded_at=TIME)
    service.store.calibrations.publish_report(forged, source)
    assert not ledger.verify_qualification_source(service.store, source.binding)["gate_eligible"]


@pytest.mark.parametrize("change", ["refused", "failed", "missing_criterion"])
def test_changed_expected_missing_direction_remains_in_denominator(tmp_path, change):
    ledger = ledger_module()
    service, scoring, _, _, _, execution = completed_environment(tmp_path)
    plan = next(p for p in execution.plan if p["sample_id"] == "sample-03" and p["call_kind"] == "order_reverse")
    job = scoring.jobs._rows[plan["child_job_id"]]
    call = next(c for c in job["calls"] if c["call_id"] == plan["call_id"])
    inv = service.store.invocations._rows[call["invocation_id"]]
    if change == "failed":
        call["outcome"] = inv["outcome"] = "failed"
    else:
        content = {"refused": True} if change == "refused" else {"winner": "tie", "criteria": []}
        call["raw_response"]["content"] = inv["result_summary"]["raw_response"]["content"] = json.dumps(content)
    report = ledger.reconstruct_calibration_report(service.store, execution.execution_id)
    assert report.report.position_swap["pairs"] == 30
    assert report.report.position_swap["rate"] == 29 / 30
    assert report.report.missing_evidence_rate == .20
    assert not report.report.gate_eligible


def test_frozen_reservation_tampering_disables_qualification(tmp_path):
    ledger = ledger_module()
    service, scoring, _, _, _, execution = completed_environment(tmp_path)
    job, _, _ = first_call(scoring, execution)
    job["allowance"]["max_calls"] += 1
    report = ledger.reconstruct_calibration_report(service.store, execution.execution_id)
    assert not report.report.gate_eligible
    assert any("frozen child allowance" in reason for reason in report.report.reasons)


def test_undeclared_child_cannot_extend_an_authorized_execution_group(tmp_path):
    ledger = ledger_module()
    service, scoring, _, _, _, execution = completed_environment(tmp_path)
    job, _, _ = first_call(scoring, execution)
    extra = deepcopy(job)
    extra.update(job_id="rogue-child", request_key="rogue-key", status="queued", calls=[])
    scoring.jobs._rows[extra["job_id"]] = extra
    record = ledger.reconstruct_calibration_report(service.store, execution.execution_id)
    assert not record.report.gate_eligible
    assert any("child multiset" in reason for reason in record.report.reasons)


# Reuse the guarded disposable-database fixture; only synthetic task-owned data.
from tests.storage.conftest import isolated_pg_database  # noqa: F401


def test_postgres_report_publication_restart_and_verified_source(tmp_path, isolated_pg_database):  # noqa: F811
    ledger = ledger_module()
    from motte_storage.migrations import upgrade
    from motte_storage.factory import create_run_store
    upgrade(isolated_pg_database)
    store = create_run_store(storage="postgres", dsn=isolated_pg_database)
    service, _, provider, _, _, execution = completed_environment(tmp_path, store=store)
    first = service.publish_report(execution.execution_id)
    assert first.report.gate_eligible
    reopened = create_run_store(storage="postgres", dsn=isolated_pg_database)
    restart = JudgeCalibrationService(reopened, None, scoring_jobs=ScoringJobService(reopened))
    assert restart.publish_report(execution.execution_id) == first
    source = next(row for row in reopened.calibrations.iter_records() if "qualification" in row)
    assert ledger.verify_qualification_source(reopened, source["binding"])["gate_eligible"]
    assert len(provider.calls) == 120


@pytest.mark.parametrize("actual_provider", [None, "", "unrelated-provider", "scripted", "openai"])
def test_settled_provider_must_match_exact_frozen_adapter(tmp_path, actual_provider):
    ledger_module()
    service, scoring, provider, _, _, execution = completed_environment(tmp_path)
    expected = execution.provider_snapshot["adapter_id"]
    assert expected == provider.kind == "openai_compatible"
    assert len(provider.calls) == len(provider.transport.calls) == 120
    assert all(response["provider"] == expected for response in provider.calls)
    _, call, invocation = first_call(scoring, execution)
    # Consistent duplicate edits do not alter the frozen request or prove origin.
    call["raw_response"]["provider"] = actual_provider
    invocation["result_summary"]["raw_response"]["provider"] = actual_provider
    invocation["result_summary"]["provider"] = actual_provider
    report = service.publish_report(execution.execution_id)
    assert not report.report.gate_eligible
    assert not report.report.coverage["ledger"]["complete"]
    assert report.report.call_count == 120
    assert report.report.repeat_stability["samples"] == 30
    assert report.report.position_swap["pairs"] == 30
    assert not any("qualification" in row for row in service.store.calibrations.iter_records())


def test_provider_mismatch_invalidates_previously_published_qualification(tmp_path):
    ledger = ledger_module()
    service, scoring, _, _, _, execution = completed_environment(tmp_path)
    service.publish_report(execution.execution_id)
    binding = next(row["binding"] for row in service.store.calibrations.iter_records()
                   if "qualification" in row)
    assert ledger.verify_qualification_source(service.store, binding)["gate_eligible"]
    _, call, invocation = first_call(scoring, execution)
    call["raw_response"]["provider"] = "unrelated-provider"
    invocation["result_summary"]["raw_response"]["provider"] = "unrelated-provider"
    invocation["result_summary"]["provider"] = "unrelated-provider"
    assert not ledger.verify_qualification_source(service.store, binding)["gate_eligible"]


@pytest.mark.parametrize("change", ["same_length_description", "required_evidence", "missing", "invalid_digest"])
def test_current_rubric_content_must_match_pinned_identity_before_compilation(tmp_path, monkeypatch, change):
    from types import MappingProxyType
    from motte_eval import rubrics
    ledger = ledger_module()
    service, _, _, version, _, execution = completed_environment(tmp_path)
    service.publish_report(execution.execution_id)
    binding = next(row["binding"] for row in service.store.calibrations.iter_records()
                   if "qualification" in row)
    assert ledger.verify_qualification_source(service.store, binding)["gate_eligible"]
    key = (version.spec.rubric_id, version.spec.rubric_version)
    current = rubrics.get_rubric(*key)
    registered = dict(rubrics.RUBRIC_REGISTRY)
    if change == "missing":
        registered.pop(key)
    else:
        payload = current.model_dump(mode="json")
        if change == "required_evidence":
            payload["criteria"][0]["evidence_required"] = False
        else:
            old = payload["criteria"][0]["description"]
            payload["criteria"][0]["description"] = "甲" + old[1:]
            assert len(payload["criteria"][0]["description"].encode()) == len(old.encode())
        if change == "invalid_digest":
            registered[key] = current.model_copy(update={"criteria": [
                rubrics.Criterion.model_validate(value) for value in payload["criteria"]
            ]})
        else:
            payload["content_sha256"] = rubrics.rubric_content_sha256(payload)
            registered[key] = rubrics.Rubric.model_validate(payload)
            assert registered[key].content_sha256 != version.spec.rubric_sha256
    monkeypatch.setattr(rubrics, "RUBRIC_REGISTRY", MappingProxyType(registered))
    verified = ledger.verify_qualification_source(service.store, binding)
    assert not verified["gate_eligible"] and verified["experimental"]
    # Fail before any compilation/parser can consume the changed semantics.
    monkeypatch.setattr(ScoringJobService, "compile_record",
                        lambda *args, **kwargs: pytest.fail("compiled a mismatched rubric"))
    with pytest.raises(CalibrationCorrupt, match="CALIBRATION_SOURCE_CORRUPT"):
        service.publish_report(execution.execution_id)
