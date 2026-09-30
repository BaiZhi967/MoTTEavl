"""Synthetic software-only calibration/subject fixtures; no paid or real human acceptance."""
from copy import deepcopy
import json
from uuid import uuid4

import pytest

from motte_contracts.evaluation import observation_evidence_hash
from motte_eval.calibration import ManualRevisionRequest, build_manual_revision
from motte_sdk.comparisons import ComparisonService
from motte_sdk.scoring_jobs import JudgeInputError, ScoringJobService, build_judge_submission
from tests.sdk.test_calibration_ledger import completed_environment
from tests.sdk.test_m6_comparison_service import make_run, append_pass, score_row, gate_policy_payload


def subject_module():
    import motte_sdk.scoring_jobs as module
    assert hasattr(module, "SubjectPairReference"), "saved subject-pair references are missing"
    assert hasattr(module, "resolve_saved_pairwise_inputs"), "saved pair resolver is missing"
    return module


@pytest.fixture(scope="module")
def calibrated(tmp_path_factory):
    env = completed_environment(tmp_path_factory.mktemp("binding"))
    service, scoring, provider, _, _, execution = env
    service.publish_report(execution.execution_id)
    qualification = next(row for row in service.store.calibrations.iter_records() if "qualification" in row)
    return env, qualification["binding"]


def save_attempts(store, *, run_id=None, case_id="case-1", count=2):
    run_id = run_id or "subject-" + uuid4().hex
    make_run(store, run_id, case_ids=[case_id])
    attempts = []
    for index in range(1, count + 1):
        row = store.attempts.begin({"id": f"{run_id}-attempt-{index}", "run_id": run_id,
                                    "case_id": case_id, "attempt_no": index})
        row = store.attempts.transition(row["id"], expected_revision=row["revision"],
                                        expected_status="prepared", status="dispatching")
        observation = {
            "observation_id": "obs-" + row["id"], "run_id": run_id, "case_id": case_id,
            "attempt_id": row["id"], "final_output": f"Software subject candidate {index}",
            "termination": {"reason": "final_answer"},
            "event_refs": [{"kind": "event", "run_id": run_id, "locator": str(index)}],
            "artifact_refs": [], "coverage": {"complete": True},
        }
        observation["evidence_hash"] = observation_evidence_hash(observation)
        store.attempts.complete(row["id"], expected_revision=row["revision"],
                                changes={"result": {"frozen_observation": observation}})
        attempts.append(row["id"])
    return run_id, {"case_id": case_id, "candidate_a_attempt_id": attempts[0],
                    "candidate_b_attempt_id": attempts[1], "challenger_attempt_id": attempts[0]}


def subject_request(calibrated, *, qualification=True, run_id=None, refs=None, **changes):
    subject_module()
    env, binding = calibrated
    service, scoring, provider, _, request, _ = env
    if run_id is None:
        run_id, ref = save_attempts(service.store)
        refs = [ref]
    args = dict(store=service.store, resources=service.resources,
                request_key="subject-" + uuid4().hex, run_id=run_id, mode="pairwise",
                spec_request=deepcopy(request.spec_request), authorisation=request.authorisation,
                price_table_version="price-1", pairwise_refs=refs,
                qualification_id=binding["qualification_id"] if qualification else None)
    args.update(changes)
    return build_judge_submission(**args)


def subject_pass(calibrated, *, qualification=True):
    env, binding = calibrated
    service, scoring, provider, version, _, _ = env
    request = subject_request(calibrated, qualification=qualification)
    before = len(provider.calls)
    job = scoring.submit(request)
    provider.transport.responses.append(json.dumps({"winner": "tie", "criteria": [
        {"criterion_id": key, "preference": "tie", "reason": "software fixture",
         "evidence": ["event:1", "event:2"]} for key in version.spec.criteria
    ]}))
    from apps.worker.motte_worker.runtime import WorkerLoop
    from motte_sdk.service import RunService
    result = WorkerLoop(RunService(service.store), scoring_jobs=scoring).claim_and_execute()
    assert result["status"] == "completed"
    assert len(provider.calls) == before + 1
    record = scoring.jobs.get(job["job_id"])
    return request, record, service.store.scoring_passes.get(record["reserved_pass_id"])


def assert_quality_unavailable(store, run_id, pass_id):
    comparisons = ComparisonService(store)
    assert comparisons.candidate_summary(run_id, scoring_pass_id=pass_id)["metric_values"]["accuracy"] is None
    lite = comparisons.evaluate_gate(run_id, scoring_pass_id=pass_id, policy={"metric": "accuracy", "threshold": 0})
    assert lite["passed"] is False
    assert any(rule["id"] == "pairwise_quality_unavailable" and not rule["passed"] for rule in lite["rules"])
    comparisons.publish_gate_policy(gate_policy_payload())
    full = comparisons.evaluate_gate_versioned(run_id=run_id, scoring_pass_id=pass_id,
                                               policy_id="pol-m6", policy_version="1")
    assert full["decision"] == "insufficient_evidence" and full["exit_code"] == 5
    assert any(row["rule_id"] == "pairwise_quality_unavailable" for row in full["rule_results"])
    return lite, full


def test_exact_binding_passes_qualification_but_not_quality(calibrated):
    env, binding = calibrated
    service, _, provider, _, _, _ = env
    request, job, record = subject_pass(calibrated)
    assert job["qualification_binding"] == record["qualification_binding"] == binding
    assert request.qualification_binding.model_dump(mode="json") == binding
    before = len(provider.calls)
    result = ComparisonService(service.store)._judge_gate_qualification(record["id"])
    assert result["required"] and result["gate_eligible"]
    assert result["binding"] == binding
    lite, full = assert_quality_unavailable(service.store, record["run_id"], record["id"])
    assert next(rule for rule in lite["rules"] if rule["id"] == "judge_qualification")["passed"]
    assert full["judge_qualification"]["candidate"]["binding"] == binding
    assert service.store.gate_store.get_result(full["gate_result_id"])["judge_qualification"] == full["judge_qualification"]
    assert len(provider.calls) == before


@pytest.mark.parametrize("field", ["calibration_id", "calibration_version", "calibration_content_sha256",
    "judge_spec_sha256", "rubric_id", "rubric_version", "rubric_sha256", "provider_snapshot_sha256",
    "model", "policy_sha256", "report_id", "report_sha256", "qualification_id"])
def test_each_identity_drift_fails_closed(calibrated, monkeypatch, field):
    service = calibrated[0][0]
    _, job, record = subject_pass(calibrated)
    changed = deepcopy(record)
    changed["qualification_binding"][field] = "sha256:" + "f" * 64 if field.endswith("sha256") else "different"
    # Even agreeing Job/Pass copies cannot replace authoritative qualification.
    monkeypatch.setitem(service.store.scoring_passes._passes, record["id"], changed)
    monkeypatch.setitem(calibrated[0][1].jobs._rows[job["job_id"]], "qualification_binding", changed["qualification_binding"])
    assert not ComparisonService(service.store)._judge_gate_qualification(record["id"])["gate_eligible"]


def test_old_pass_never_inherits_new_qualification(calibrated):
    service = calibrated[0][0]
    _, job, record = subject_pass(calibrated, qualification=False)
    assert not job.get("qualification_binding") and not record.get("qualification_binding")
    assert not ComparisonService(service.store)._judge_gate_qualification(record["id"])["gate_eligible"]


def test_manual_revision_keeps_source_qualification(calibrated, monkeypatch):
    service = calibrated[0][0]
    _, _, source = subject_pass(calibrated)
    revision_id = "manual-" + uuid4().hex
    revision = build_manual_revision(source, ManualRevisionRequest(
        run_id=source["run_id"], source_pass_id=source["id"], actor="software-only", reason="fixture",
        expected_current_pass_id=source["id"], changes=[{
            "case_id": row["case_id"], "metric_id": row["metric_id"],
            "passed": True, "value": 1.0,
        } for row in source["scores"]]), revision_id=revision_id, created_at=source["created_at"], current_pass_id=source["id"])
    # Copied client-looking fields do not override manual source traversal.
    revision["pass"]["judge"] = {"mode": "single", "qualified": True}
    service.store.scoring_passes.append(revision["pass"], revision["scores"])
    comparisons = ComparisonService(service.store)
    result = comparisons._judge_gate_qualification(revision_id)
    assert result["gate_eligible"] and result["judge_pass_id"] == source["id"]
    assert_quality_unavailable(service.store, source["run_id"], revision_id)
    assert comparisons.report_snapshot(source["run_id"], scoring_pass_id=revision_id).metric_values["cost.per_success_usd"] is None
    summary = comparisons.candidate_summary(source["run_id"], scoring_pass_id=revision_id)
    assert summary["cost"]["known"] is True
    assert summary["cost"]["total_usd"] == 1.25
    assert summary["cost"]["currency"] == "USD"
    assert summary["cost"]["per_success_usd"] is None
    from fastapi.testclient import TestClient
    from apps.api.app.main import create_app
    response = TestClient(create_app(service.store)).post("/api/v1/gates", json={
        "run_id": source["run_id"], "scoring_pass_id": revision_id,
        "policy": {"metric": "accuracy", "threshold": 0},
    })
    assert response.status_code == 200, response.text
    public_cost = response.json()["coverage_summary"]["cost"]
    assert public_cost == summary["cost"]
    new_id = "new-" + uuid4().hex
    append_pass(service.store, source["run_id"], new_id, [score_row("case-1", passed=True)])
    run = service.store.runs.get(source["run_id"])
    service.store.runs.update({**run, "current_scoring_pass_id": new_id}, expected_revision=run["revision"])
    assert service.store.runs.get(source["run_id"])["current_scoring_pass_id"] == new_id
    assert comparisons._judge_gate_qualification(revision_id) == result


@pytest.mark.parametrize("mode", ["single", "pairwise"])
@pytest.mark.parametrize("manual", [False, True])
def test_terminal_pairwise_cost_projection_has_no_success_denominator(mode, manual):
    """Synthetic frozen summaries test projection, not Judge qualification."""
    from motte_storage.run_store import InMemoryRunStore
    store = InMemoryRunStore()
    make_run(store, "terminal", case_ids=["case-1"], manifest={
        "benchmark_provenance": {"suite": "terminal-bench-harbor"},
        "task_manifest": {"trials": [{"trial_id": "trial-1"}]},
    })
    record = {
        "id": "terminal-source", "run_id": "terminal", "source": "judge",
        "source_run_revision": 1, "scorer_id": "software-judge", "scorer_version": "1",
        "created_at": "2026-09-30T00:00:00Z", "judge": {"mode": mode},
        "summary": {"aggregate": {
            "valid_trial_pass_rate": 1.0, "valid_trial_coverage": 1.0,
            "selected_trials": 1, "observed_trials": 1, "valid_trials": 1,
            "valid_pass_trials": 1, "invalid_trials": 0,
            "cost": {"known_trials": 1, "unknown_cost": False, "known_cost_usd": 1.25},
        }},
    }
    store.scoring_passes.append(record, [])
    if manual:
        record = {**deepcopy(record), "id": "terminal-manual", "source": "manual_revision",
                  "previous_pass_id": "terminal-source", "judge": {"mode": "single"},
                  "manual_revision": {"source_pass_id": "terminal-source"}}
        store.scoring_passes.append(record, [])
    summary = ComparisonService(store).candidate_summary("terminal", scoring_pass_id=record["id"])
    assert summary["cost"]["known"] and summary["cost"]["total_usd"] == 1.25
    assert summary["cost"]["currency"] == "USD"
    assert summary["cost"]["per_success_usd"] == (None if mode == "pairwise" else 1.25)
    assert summary["metric_values"]["valid_trial_pass_rate"] == (None if mode == "pairwise" else 1.0)


@pytest.mark.parametrize("change", ["cycle", "missing", "cross_run", "source_mismatch", "shadow", "spoofed_job"])
def test_broken_or_spoofed_lineage_fails_closed(calibrated, monkeypatch, change):
    service = calibrated[0][0]
    _, _, source = subject_pass(calibrated)
    record = deepcopy(source)
    record["id"] = "untrusted-" + uuid4().hex
    if change in {"shadow", "spoofed_job"}:
        record["source"] = "shadow" if change == "shadow" else "judge"
    else:
        record.update(source="manual_revision", previous_pass_id=source["id"],
                      manual_revision={"source_pass_id": source["id"]})
        if change == "cycle":
            record["manual_revision"]["source_pass_id"] = record["previous_pass_id"] = record["id"]
        elif change == "missing":
            record["manual_revision"]["source_pass_id"] = record["previous_pass_id"] = "missing"
        elif change == "cross_run":
            record["run_id"] = "other-run"
        else:
            record["previous_pass_id"] = "another-source"
    service.store.scoring_passes.append(record, [])
    assert not ComparisonService(service.store)._judge_gate_qualification(record["id"])["gate_eligible"]


@pytest.mark.parametrize("change", ["same_attempt", "cross_run", "cross_case", "open", "missing_observation", "hash", "observation_attempt", "caller_content", "payload_id"])
def test_pairwise_subject_refs_are_saved_same_task_only(calibrated, monkeypatch, change):
    module = subject_module()
    store = calibrated[0][0].store
    run_id, ref = save_attempts(store)
    if change == "same_attempt":
        ref["candidate_b_attempt_id"] = ref["candidate_a_attempt_id"]
    elif change == "caller_content":
        ref["content"] = "client invention"
    else:
        row = deepcopy(store.attempts._rows[ref["candidate_b_attempt_id"]])
        if change == "cross_run":
            row["run_id"] = "foreign"
        elif change == "cross_case":
            row["case_id"] = "other-case"
        elif change == "open":
            row["status"] = "dispatching"
        elif change == "missing_observation":
            row["result"] = {}
            store.case_runs.upsert({"run_id": run_id, "case_id": "case-1", "result": {
                "observation": store.attempts.get(ref["candidate_a_attempt_id"])["result"]["frozen_observation"]}})
        elif change == "payload_id":
            row["id"] = "wrong-attempt-payload"
        elif change == "hash":
            row["result"]["frozen_observation"]["final_output"] = "tampered"
        else:
            observation = row["result"]["frozen_observation"]
            observation["attempt_id"] = ref["candidate_a_attempt_id"]
            observation["evidence_hash"] = observation_evidence_hash(observation)
        monkeypatch.setitem(store.attempts._rows, ref["candidate_b_attempt_id"], row)
    with pytest.raises((ValueError, JudgeInputError)):
        module.resolve_saved_pairwise_inputs(store, run_id, [ref])


def test_saved_pair_input_is_stable_and_does_not_read_current_case(calibrated):
    module = subject_module()
    store = calibrated[0][0].store
    run_id, ref = save_attempts(store)
    first = module.resolve_saved_pairwise_inputs(store, run_id, [ref])[0]
    assert first.candidate_a.candidate.candidate_id != first.candidate_b.candidate.candidate_id
    assert first.candidate_a.content == "Software subject candidate 1"
    store.case_runs.upsert({"run_id": run_id, "case_id": "case-1", "result": {"observation": {"forged": True}}})
    assert module.resolve_saved_pairwise_inputs(store, run_id, [ref])[0] == first


def test_submission_requires_exact_budget_bearing_spec_and_no_client_binding(calibrated):
    service, scoring, provider, _, request, _ = calibrated[0]
    changed = deepcopy(request.spec_request)
    changed["budget"]["max_calls"] -= 1
    before = len(provider.calls)
    with pytest.raises(JudgeInputError):
        subject_request(calibrated, spec_request=changed)
    assert len(provider.calls) == before
    from apps.api.app.schemas import JudgeSubmitRequest
    with pytest.raises(ValueError):
        JudgeSubmitRequest.model_validate({"run_id": "x", "request_key": "x", "spec": request.spec_request,
                                          "qualification_binding": calibrated[1]})


@pytest.mark.parametrize("links", [100, 101])
def test_manual_lineage_has_bounded_source_links(calibrated, links):
    store = calibrated[0][0].store
    _, _, source = subject_pass(calibrated)
    current = source["id"]
    for _ in range(links):
        record = {"id": "manual-" + uuid4().hex, "run_id": source["run_id"],
                  "source": "manual_revision", "scorer_id": "manual-revision", "scorer_version": "1",
                  "previous_pass_id": current, "manual_revision": {"source_pass_id": current}}
        store.scoring_passes.append(record, [])
        current = record["id"]
    assert ComparisonService(store)._judge_gate_qualification(current)["gate_eligible"] is (links <= 100)


def test_public_replay_preserves_frozen_inputs_before_resource_lookup(calibrated, monkeypatch):
    from motte_storage.scoring_jobs import ScoringJobConflict
    env, binding = calibrated
    service, scoring, provider, _, _, _ = env
    run_id, ref = save_attempts(service.store)
    key = "replay-" + uuid4().hex
    request = subject_request(calibrated, run_id=run_id, refs=[ref], request_key=key)
    job = scoring.submit(request)
    before = len(provider.calls)
    monkeypatch.setattr(service.resources.models, "get", lambda *args: pytest.fail("replay looked up mutable resources"))
    monkeypatch.setattr(service.store.attempts, "get", lambda *args: pytest.fail("replay replaced frozen candidate input"))
    try:
        replay = subject_request(calibrated, run_id=run_id, refs=[ref], request_key=key)
        repeated = scoring.submit(replay)
        assert repeated["job_id"] == job["job_id"] and repeated["reused"]
        assert replay.pairwise_pairs == request.pairwise_pairs
        assert replay.qualification_binding == request.qualification_binding
        with pytest.raises(ScoringJobConflict):
            subject_request(calibrated, run_id=run_id, refs=[ref], request_key=key,
                            qualification_id="another-source")
        assert len(provider.calls) == before
    finally:
        scoring.cancel(job["job_id"], actor="software-only", reason="end synthetic replay fixture")


def test_both_selected_baseline_and_candidate_bindings_are_gate_evidence(calibrated):
    store = calibrated[0][0].store
    _, _, baseline = subject_pass(calibrated)
    _, _, candidate = subject_pass(calibrated)
    service = ComparisonService(store)
    baseline_id = "baseline-" + uuid4().hex
    service.create_baseline(baseline_id, [{"cell_key": None, "run_id": baseline["run_id"],
        "scoring_pass_id": baseline["id"]}], policy={"allowed_factors": ["model"]},
        created_by="software-only", reason="fixed source fixture")
    lite = service.evaluate_gate(candidate["run_id"], scoring_pass_id=candidate["id"],
        baseline_run_id=baseline["run_id"], policy={"metric": "accuracy", "threshold": 0, "require_comparable": True})
    assert lite["judge_qualification"]["baseline_judge_qualification"]["judge_pass_id"] == baseline["id"]
    service.publish_gate_policy(gate_policy_payload())
    full = service.evaluate_gate_versioned(run_id=candidate["run_id"], scoring_pass_id=candidate["id"],
        baseline_id=baseline_id, policy_id="pol-m6", policy_version="1")
    for role, record in [("candidate", candidate), ("baseline", baseline)]:
        assert full["judge_qualification"][role]["judge_pass_id"] == record["id"]
        assert full["judge_qualification"][role]["binding"] == calibrated[1]
    assert full["decision"] != "pass" and not lite["passed"]


@pytest.mark.parametrize("malformed", ["manual", "summary", "judge"])
def test_malformed_lineage_stays_a_gate_failure(calibrated, malformed):
    store = calibrated[0][0].store
    _, _, source = subject_pass(calibrated)
    record = deepcopy(source)
    record["id"] = "malformed-" + uuid4().hex
    record.update(source="manual_revision", previous_pass_id=source["id"],
                  manual_revision={"source_pass_id": source["id"]})
    if malformed == "manual":
        record["manual_revision"] = ["not a source mapping"]
    elif malformed == "summary":
        record["summary"] = {"manual_revision": ["not a source mapping"]}
    else:
        record.update(source="judge", judge=["not an instrument"])
    store.scoring_passes.append(record, [])
    assert ComparisonService(store).evaluate_gate(record["run_id"], scoring_pass_id=record["id"],
                                                  policy={"threshold": 0})["passed"] is False


def test_legacy_gate_serialization_omits_absent_qualification_evidence():
    from motte_contracts.gates import GateResult
    from motte_storage.run_store import InMemoryRunStore
    store = InMemoryRunStore()
    make_run(store, "old", case_ids=["c"])
    append_pass(store, "old", "old-pass", [score_row("c", passed=True)])
    service = ComparisonService(store)
    service.publish_gate_policy(gate_policy_payload())
    result = service.evaluate_gate_versioned(run_id="old", scoring_pass_id="old-pass",
                                             policy_id="pol-m6", policy_version="1")
    assert "judge_qualification" not in result
    raw = {key: value for key, value in result.items() if key != "exit_code"}
    restored = GateResult.model_validate(raw)
    assert restored.model_dump(mode="json") == raw
    assert restored.compute_gate_result_id() == raw["gate_result_id"]
    assert restored.compute_conclusion_hash() == raw["conclusion_hash"]


from tests.storage.conftest import isolated_pg_database  # noqa: F401


def test_postgres_selected_source_survives_restart_without_provider(tmp_path, isolated_pg_database):  # noqa: F811
    from motte_storage.migrations import upgrade
    from motte_storage.factory import create_run_store
    upgrade(isolated_pg_database)
    store = create_run_store(storage="postgres", dsn=isolated_pg_database)
    env = completed_environment(tmp_path, store=store)
    env[0].publish_report(env[-1].execution_id)
    binding = next(row["binding"] for row in store.calibrations.iter_records() if "qualification" in row)
    _, job, record = subject_pass((env, binding))
    reopened = create_run_store(storage="postgres", dsn=isolated_pg_database)
    assert ComparisonService(reopened)._judge_gate_qualification(record["id"])["gate_eligible"]
    _, result = assert_quality_unavailable(reopened, record["run_id"], record["id"])
    assert reopened.gate_store.get_result(result["gate_result_id"])["judge_qualification"]["candidate"]["binding"] == binding
    assert len(env[2].calls) == 121


@pytest.mark.parametrize("corruption", ["pass_index", "job_payload_id"])
def test_selected_source_rejects_record_identity_aliases(calibrated, monkeypatch, corruption):
    env, _ = calibrated
    store, jobs = env[0].store, env[1].jobs
    _, job, record = subject_pass(calibrated)
    selected = record["id"]
    if corruption == "pass_index":
        selected = "alias-" + uuid4().hex
        monkeypatch.setitem(store.scoring_passes._passes, selected, deepcopy(record))
    else:
        monkeypatch.setitem(jobs._rows[job["job_id"]], "job_id", "mismatched-job-payload")
    assert not ComparisonService(store)._judge_gate_qualification(selected)["gate_eligible"]


def test_new_subject_requires_one_pair_per_selected_case(calibrated):
    module = subject_module()
    store = calibrated[0][0].store
    run_id, first = save_attempts(store, count=3)
    second = {**first, "candidate_b_attempt_id": run_id + "-attempt-3"}
    with pytest.raises(JudgeInputError, match="one pair per selected case"):
        module.resolve_saved_pairwise_inputs(store, run_id, [first, second])
    with pytest.raises(JudgeInputError, match="one pair per selected case"):
        subject_request(calibrated, run_id=run_id, refs=[first, second])
    for duplicate in (first, {**first, "candidate_a_attempt_id": first["candidate_b_attempt_id"],
                              "candidate_b_attempt_id": first["candidate_a_attempt_id"]}):
        with pytest.raises(JudgeInputError):
            module.resolve_saved_pairwise_inputs(store, run_id, [first, duplicate])


def test_public_sdk_and_api_default_shapes_replay_same_frozen_pair(calibrated):
    from apps.api.app.schemas import JudgeSubmitRequest
    env, binding = calibrated
    service, scoring, provider, _, original, _ = env
    run_id, ref = save_attempts(service.store)
    key = "cross-surface-" + uuid4().hex
    request = subject_request(calibrated, run_id=run_id, refs=[ref], request_key=key)
    job = scoring.submit(request)
    body = JudgeSubmitRequest.model_validate({
        "request_key": key, "run_id": run_id, "mode": "pairwise", "spec": original.spec_request,
        "authorisation": original.authorisation.model_dump(exclude={"purpose", "authorised_at"}),
        "pairwise_refs": [ref], "qualification_id": binding["qualification_id"], "price_table_version": "price-1",
    })
    before = len(provider.calls)
    try:
        replay = build_judge_submission(store=service.store, resources=service.resources,
            request_key=key, run_id=run_id, mode=body.mode, spec_request=body.spec.model_dump(),
            authorisation=body.authorisation.model_dump(), pairwise_refs=body.pairwise_refs,
            qualification_id=body.qualification_id, price_table_version=body.price_table_version,
            case_ids=body.case_ids, source_pass_id=body.source_pass_id, publish_policy=body.publish_policy,
            repeats=body.repeats, presentation_orders=body.presentation_orders)
        result = scoring.submit(replay)
        assert result["reused"] and result["job_id"] == job["job_id"]
        assert len(provider.calls) == before
    finally:
        scoring.cancel(job["job_id"], actor="software-only", reason="end cross-surface fixture")
