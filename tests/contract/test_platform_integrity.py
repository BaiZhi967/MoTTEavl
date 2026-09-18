"""Core v2 contracts and explicit legacy projection boundaries."""
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from motte_contracts.errors import ErrorEnvelope, ExecutionError
from motte_contracts.events import TraceEvent
from motte_contracts.evidence import Score, ScoreSet, ScoringPass
from motte_contracts.model import (IdentityEvidence, IdentityPolicy, IdentityResult,
                                   IdentityVerdict, ModelProfile)
from motte_contracts.report import RunReport
from motte_contracts.run import (AttemptStatus, CaseAttempt, CaseRun,
                                 EvaluationDescriptor, ExecutionSpec, ResolvedManifest,
                                 Run, RunCommand, RunCommandStatus, RunStatus)
from motte_contracts.scenario import ScenarioSpec

NOW = datetime(2026, 9, 18, 12, tzinfo=UTC)


def _roundtrip(record):
    assert type(record).model_validate_json(record.model_dump_json()) == record


def _score(pass_id="pass-1"):
    return Score(case_id="case-1", passed=True, outcome="correct",
                 scorer="exact", scorer_version="answer-v1", scoring_pass_id=pass_id)


def test_run_v2_roundtrip_and_real_case_shape():
    run = Run(
        id="run-9e560b89139c470a98a876b659818722", scenario_version="gsm8k-test@1",
        status=RunStatus.completed, revision=7, manifest={"provider": {"kind": "replay"}},
        requested_manifest={"model": "profile-1"}, case_ids=["case-1"],
        parent_run_id="run-1", current_scoring_pass_id="pass-1", created_at=NOW,
        updated_at=NOW, finished_at=NOW,
        cases=[CaseRun(run_id="run-9e560b89139c470a98a876b659818722",
                       case_id="case-1", result={"content": "42"}, expected="42")],
        scores=[_score()],
    )
    _roundtrip(run)
    assert run.status == "completed"
    assert Run.model_validate({"id": "run-1", "scenario_version": "replay@1",
                               "model": "fixture-model"}).model == "fixture-model"
    assert run.cases[0].status is None
    assert run.schema_version == 2


def test_legacy_run_can_be_explicitly_projected_without_fake_revision():
    legacy = Run.model_validate({
        "schema_version": 1, "revision": 0, "id": "run-1",
        "scenario_version": "replay@1", "status": "failed", "case_ids": ["case-1"],
        "manifest": {}, "cases": [{"run_id": "run-1", "case_id": "case-1", "result": None}],
        "scores": [{"case_id": "case-1", "passed": False}],
        "error": {"type": "ProviderCallError", "class": "server", "message": "offline"},
    })
    assert legacy.revision == 0 and legacy.schema_version == 1
    assert legacy.error is not None and legacy.error.error_class == "server"
    _roundtrip(legacy)


@pytest.mark.parametrize("update", [
    {"scenario_id": "old-shape"}, {"status": "unknown"},
    {"revision": -1}, {"revision": True}, {"case_ids": ["a", "a"]},
    {"revision": 0},
])
def test_run_rejects_unknown_fields_invalid_status_and_revision(update):
    with pytest.raises(ValidationError):
        Run.model_validate({"id": "run-1", "scenario_version": "replay@1", **update})


def test_frozen_run_and_mutable_defaults_are_isolated():
    first = Run(id="run-1", scenario_version="replay@1")
    second = Run(id="run-2", scenario_version="replay@1")
    with pytest.raises(ValidationError):
        first.status = RunStatus.completed
    first.manifest["model"] = "first"
    first.case_ids.append("case-1")
    assert second.manifest == {} and second.case_ids == []
    left = ScenarioSpec(id="s", version="1", mode="direct", dataset="d", model="m")
    right = ScenarioSpec(id="s", version="1", mode="direct", dataset="d", model="m")
    left.skills.append("custom")
    assert right.skills == []


def test_case_attempt_models_uncertain_dispatch_and_rejects_invalid_state():
    attempt = CaseAttempt(
        id="attempt-1", run_id="run-1", case_id="case-1", revision=3, attempt_no=1,
        status=AttemptStatus.indeterminate, execution_token="token-1",
        idempotency_key="request-1", dispatched_at=NOW,
        error=ExecutionError(code="RESULT_UNCERTAIN", message="request may have been sent"),
    )
    _roundtrip(attempt)
    assert RunStatus.needs_review.value == "needs_review"
    with pytest.raises(ValidationError):
        CaseAttempt(id="a", run_id="r", case_id="c", status="retried")
    with pytest.raises(ValidationError):
        CaseAttempt(id="a", run_id="r", case_id="c", attempt_no=0)
    stored = CaseAttempt.model_validate({"id": "a", "run_id": "r", "case_id": "c",
                                         "revision": 1, "attempt_no": 1, "status": "prepared"})
    assert stored.status is AttemptStatus.prepared


def test_execution_and_evaluation_manifest_v2_roundtrip():
    descriptor = EvaluationDescriptor(
        benchmark_id="gsm8k", benchmark_version="1",
        adapter_id="gsm8k-official-jsonl", adapter_version="1",
        scorer_id="final-decimal", scorer_version="gsm8k-final-decimal-v1",
    )
    manifest = ResolvedManifest(
        execution=ExecutionSpec(
            backend_id="direct-llm", backend_version="1", config_hash="sha256:abcd",
            capabilities={"interactive": False, "safe_to_repeat": False},
        ),
        evaluation=descriptor,
        provider={"kind": "openai_compatible", "model": "target"},
        resource_snapshots={"model": {"id": "target", "profile_hash": "abc"}},
        model="target", cases={"case-1": {"prompt": "What is 6*7?"}},
        benchmark_provenance={"suite": "gsm8k"},
    )
    _roundtrip(manifest)
    assert manifest.schema_version == 2
    with pytest.raises(ValidationError):
        ResolvedManifest.model_validate({"model": {"id": "old"}, "dataset": {"id": "d"}})
    with pytest.raises(ValidationError):
        ResolvedManifest.model_validate({"schema_version": 1, "execution": manifest.execution.model_dump()})
    with pytest.raises(ValidationError):
        ExecutionSpec(backend_id="direct-llm", backend_version="1",
                      capabilities={"interactive": "false"})
    with pytest.raises(ValidationError):
        ResolvedManifest.model_validate({**manifest.model_dump(), "untracked": "value"})


def test_event_envelope_roundtrip_and_legacy_flat_event_rejected():
    event = TraceEvent(run_id="run-1", seq=3, type="model_response",
                       payload={"case_id": "case-1", "result": {"content": "42"}},
                       recorded_at=NOW)
    _roundtrip(event)
    assert event.schema_version == 2 and event.span_id is None
    with pytest.raises(ValidationError):
        TraceEvent.model_validate({"run_id": "run-1", "seq": 3,
                                   "type": "running", "status": "running"})
    with pytest.raises(ValidationError):
        TraceEvent(run_id="run-1", seq=0, type="running")


def test_scoring_passes_are_separate_immutable_snapshots():
    initial = ScoringPass(id="pass-1", run_id="run-1", scorer_id="exact",
                          scorer_version="answer-v1", created_at=NOW, scores=[_score()])
    rescored = ScoringPass(id="pass-2", run_id="run-1", scorer_id="exact",
                           scorer_version="answer-v2", previous_pass_id=initial.id,
                           scores=[_score("pass-2")])
    _roundtrip(initial)
    _roundtrip(rescored)
    assert initial.id != rescored.id and initial.scores[0].scoring_pass_id == "pass-1"
    _roundtrip(ScoreSet(run_id="run-1", scoring_pass_id="pass-1", scores=[_score()]))
    _roundtrip(Score(evaluator="accuracy", value=1.0))
    with pytest.raises(ValidationError):
        ScoreSet(run_id="run-1", scoring_pass_id="pass-1", scores=[_score(), _score()])
    with pytest.raises(ValidationError):
        ScoreSet(run_id="run-1", scoring_pass_id="pass-2", scores=[_score()])
    with pytest.raises(ValidationError):
        ScoreSet(run_id="run-1", scoring_pass_id="pass-1", scores=[Score(passed=True)])
    with pytest.raises(ValidationError):
        ScoreSet(run_id="run-1", scoring_pass_id="pass-1", scores=[Score(case_id="")])


def test_error_envelope_preserves_legacy_class_without_accepting_unknown_keys():
    envelope = ErrorEnvelope.model_validate({"error": {
        "code": "PROVIDER_FAILED", "class": "network", "message": "offline",
    }})
    _roundtrip(envelope)
    assert envelope.error.error_class == "network"
    assert envelope.model_dump()["error"]["error_class"] == "network"
    with pytest.raises(ValidationError):
        ExecutionError(message="missing code and type")
    with pytest.raises(ValidationError):
        ErrorEnvelope.model_validate({"error": {"code": "FAIL"}, "unexpected": True})


def test_run_command_queued_is_not_acknowledged():
    command = RunCommand(id="cmd-1", run_id="run-1", content="continue", created_at=NOW)
    _roundtrip(command)
    assert command.status is RunCommandStatus.queued and command.revision == 1
    assert command.delivered_at is None and command.acknowledged_at is None
    with pytest.raises(ValidationError):
        RunCommand(id="cmd-1", run_id="run-1", status="accepted")


def test_report_binds_v2_scores_to_pass_and_legacy_report_remains_projectable():
    report = RunReport(
        run_id="run-1", scenario_version="gsm8k-test@1", status=RunStatus.completed,
        generated_at=NOW, scoring_pass_id="pass-1",
        summary={"cases": 1, "scored": 1, "passed": 1, "failed": 0,
                 "pass_rate": 1.0, "selected": 1, "accuracy": 1.0},
        cost={"total": 0.0, "price_table_versions": ["v1"]},
        scores=[_score()], cases=[{"case_id": "case-1", "result": {"content": "42"}}],
        benchmark={"suite": "gsm8k"}, usage={"prompt_tokens": 3},
    )
    _roundtrip(report)
    assert report.cost.total == 0.0
    with pytest.raises(ValidationError):
        RunReport.model_validate({**report.model_dump(mode="json"), "scoring_pass_id": None})
    legacy = RunReport.model_validate({**report.model_dump(mode="json"),
                                       "schema_version": 1, "scoring_pass_id": None,
                                       "scores": [{"case_id": "case-1", "passed": True}]})
    assert legacy.schema_version == 1
    _roundtrip(legacy)


def test_model_identity_does_not_infer_reported_model_from_request():
    unreported = IdentityResult(requested_model="gateway/org/model",
                                identity_policy=IdentityPolicy.require_reported,
                                identity_policy_result=IdentityVerdict.unreported,
                                policy_passed=False)
    _roundtrip(unreported)
    assert unreported.reported_model is None and unreported.resolved_model_identity is None
    assert unreported.identity_evidence.source is None
    alias = IdentityResult(
        requested_model="gateway/org/model", reported_model="model-2026-09-18",
        resolved_model_identity="org/model", identity_policy=IdentityPolicy.require_match,
        identity_policy_result=IdentityVerdict.alias_match, policy_passed=True,
        identity_evidence=IdentityEvidence(
            source="response.model", reported_value="model-2026-09-18",
            path="$.model", alias_map_version="model-profile-4",
            matched_alias="model-2026-09-18", policy=IdentityPolicy.require_match),
    )
    _roundtrip(alias)
    profile = ModelProfile(id="model", provider="gateway", capabilities={},
                           identity_policy=IdentityPolicy.require_match,
                           identity_aliases={"model-2026-09-18": "org/model"},
                           identity_alias_version="model-profile-4")
    _roundtrip(profile)
    assert profile.identity_aliases["model-2026-09-18"] == "org/model"
    with pytest.raises(ValidationError):
        IdentityResult(requested_model="x", reported_model="x",
                       identity_policy_result=IdentityVerdict.unreported)
    with pytest.raises(ValidationError):
        IdentityResult(requested_model="x", identity_policy_result=IdentityVerdict.alias_match)
    with pytest.raises(ValidationError):
        IdentityResult(requested_model="x", identity_policy=IdentityPolicy.report_only,
                       identity_evidence={"source": None, "policy": "require_match"})
    with pytest.raises(ValidationError):
        ModelProfile(id="model", provider="gateway", capabilities={},
                     identity_aliases={"alias": "canonical"})
