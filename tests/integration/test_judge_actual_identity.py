"""Actual identity is a Gate prerequisite even when execution is report-only.

30-sample software-labelled fixtures, no human/model acceptance claim.
"""
import json

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from apps.worker.motte_worker.reporting import WorkerReporter
from apps.worker.motte_worker.runtime import WorkerLoop
from motte_sdk.calibration_ledger import qualification_source, verify_qualification_source, reconstruct_calibration_report
from motte_sdk.service import RunService
from tests.sdk.test_calibration_gate_binding import save_attempts
from tests.sdk.test_calibration_ledger import completed_environment
from tests.sdk.test_pairwise_quality import quality_store  # noqa: F401
from tests.evaluators.test_pairwise_quality_gate import lite, policy


@pytest.mark.parametrize("phase", ["calibration", "subject"])
@pytest.mark.parametrize("identity", ["matching", "mismatch", "missing", "allowed-alias", "changed-alias", "changed-alias-map"])
def test_actual_identity_binds_both_public_gates_after_restart(quality_store, tmp_path, phase, identity):  # noqa: F811
    store, reopen = quality_store
    aliases = {"wire-alias": "scripted-model"}
    config = {"identity_policy": "report_only", "identity_aliases": aliases, "identity_alias_version": "aliases@1"}
    service, scoring, provider, _, request, execution = completed_environment(
        tmp_path, store=store, execute=False, identity_config=config,
    )
    expected = identity in {"matching", "allowed-alias"}
    wire_phase = ["calibration"]
    original = provider.transport.post_json_detailed

    def substitute(path, body):
        outcome = original(path, body)
        if phase == wire_phase[0]:
            if identity == "missing":
                outcome.response_body.pop("model")
            elif identity == "mismatch":
                outcome.response_body["model"] = "unqualified-model"
            elif identity in {"allowed-alias", "changed-alias", "changed-alias-map"}:
                outcome.response_body["model"] = "wire-alias"
                if identity == "changed-alias":
                    # Same target, different actual version: frozen identity must
                    # not accept a silently replaced map behind the provider.
                    provider.identity_alias_version = "aliases@2"
                elif identity == "changed-alias-map":
                    provider.identity_aliases = {"wire-alias": "different-model"}
        return outcome

    provider.transport.post_json_detailed = substitute
    worker = WorkerLoop(RunService(store), scoring_jobs=scoring, reporter=WorkerReporter(enabled=False))
    for _ in execution.child_job_ids:
        assert worker.claim_and_execute()["status"] == "completed"
    record = service.publish_report(execution.execution_id)
    assert record.report.qualified is (expected if phase == "calibration" else True)
    source = qualification_source(record, recorded_at=record.recorded_at)
    if source is None:
        assert phase == "calibration" and not expected
        rebuilt = reconstruct_calibration_report(reopen(), execution.execution_id)
        assert not rebuilt.report.gate_eligible and rebuilt.content_sha256 == record.content_sha256
    else:
        assert verify_qualification_source(reopen(), source.binding)["gate_eligible"]
    wire_phase[0] = "subject"
    provider.identity_alias_version = "aliases@1"
    provider.identity_aliases = aliases
    run_id, pair = save_attempts(store)
    client = TestClient(create_app(store, resource_store=service.resources, judge_provider_factory=scoring.provider_factory))
    auth = request.authorisation.model_dump(mode="json")
    subject = {
        "run_id": run_id, "mode": "pairwise", "pairwise_refs": [pair], "spec": request.spec_request,
        "request_key": "actual-model-subject", "price_table_version": "price-1",
        "authorisation": {key: value for key, value in auth.items() if key not in {"purpose", "authorised_at"}},
    }
    if record.report.qualified:
        subject["qualification_id"] = source.binding.qualification_id
    response = client.post("/api/v1/judges", json=subject)
    assert response.status_code == 202, response.text
    provider.transport.responses.append(json.dumps({"winner": "tie", "criteria": [
        {"criterion_id": key, "preference": "tie", "reason": "software-only", "evidence": ["event:1", "event:2"]}
        for key in execution.spec.criteria
    ]}))
    assert worker.claim_and_execute()["status"] == "completed"
    # Existing policy still allows all responses to execute: qualification is
    # independently stricter than a successful report-only provider call.
    assert all(call["policy_passed"] is True for call in provider.calls)
    pass_id = response.json()["reserved_pass_id"]
    first_invocation = store.invocations.list_for_job(execution.child_job_ids[0])[0]
    persisted = first_invocation["result_summary"]
    assert "reported_model" in persisted and "identity_evidence" in persisted["raw_response"]
    before = len(provider.calls)
    for reopened in (store, reopen()):
        http = TestClient(create_app(reopened, resource_store=service.resources, judge_provider_factory=None))
        result = http.post("/api/v1/gates", json={"run_id": run_id, "scoring_pass_id": pass_id, "policy": lite()})
        assert result.status_code == 200, result.text
        assert result.json()["passed"] is expected
        assert http.post("/api/v1/gate-policies", json=policy()).status_code == 201
        result = http.post("/api/v1/gates/versioned", json={"run_id": run_id, "scoring_pass_id": pass_id, "policy_id": "explicit", "policy_version": "1"})
        assert result.status_code == 200, result.text
        assert result.json()["decision"] == ("pass" if expected else "insufficient_evidence")
    assert len(provider.calls) == before
    # Current registry changes do not replace historical frozen alias evidence.
    connection = service.resources.providers.get("judge-conn")
    service.resources.providers.put({**connection, "identity_aliases": {"wire-alias": "different-model"}, "identity_alias_version": "aliases@99"})
    if source is None:
        assert phase == "calibration" and not expected
        rebuilt = reconstruct_calibration_report(reopen(), execution.execution_id)
        assert not rebuilt.report.gate_eligible and rebuilt.content_sha256 == record.content_sha256
    else:
        assert verify_qualification_source(reopen(), source.binding)["gate_eligible"]
