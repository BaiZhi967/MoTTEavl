"""Uncalibrated Judge evidence cannot qualify either public Gate entry point."""
import pytest
from motte_sdk.comparisons import ComparisonService
from tests.api.test_judge_flow import environment, run_worker, submit_body, POLICY_FOR_ONE_CASE
from tests.sdk.test_m6_comparison_service import gate_policy_payload


def judged(tmp_path):
    store, resources, provider, _factory, client = environment(tmp_path)
    client.post("/api/v1/judges", json=submit_body())
    assert run_worker(store, provider).claim_and_execute()["status"] == "completed"
    return store, client


def test_lite_gate_retains_case_metrics_but_rejects_uncalibrated_judge(tmp_path):
    store, client = judged(tmp_path)
    response = client.post("/api/v1/gates", json={
        "run_id": "run-1", "policy": {**POLICY_FOR_ONE_CASE, "judge_qualified": True},
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["coverage_summary"]["selected"] == 1
    assert result["coverage_summary"]["metric_values"]["accuracy"] == 1.0
    assert result["passed"] is False
    assert next(rule for rule in result["rules"] if rule["id"] == "judge_qualification")["passed"] is False


@pytest.mark.parametrize("diagnostic", [False, True])
def test_versioned_gate_uncalibrated_judge_never_becomes_pass(tmp_path, diagnostic):
    store, client = judged(tmp_path)
    service = ComparisonService(store)
    service.publish_gate_policy(gate_policy_payload(diagnostic=diagnostic))
    response = client.post("/api/v1/gates/versioned", json={
        "run_id": "run-1", "policy_id": "pol-m6", "policy_version": "1",
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["decision"] == "insufficient_evidence"
    assert result["exit_code"] == 5
    assert any(rule["rule_id"] == "judge_qualification" for rule in result["rule_results"])
    assert store.gate_store.get_result(result["gate_result_id"]) is not None


def test_lite_gate_baseline_ref_and_qualification_use_same_compared_pass(monkeypatch):
    from motte_storage.run_store import InMemoryRunStore
    from tests.sdk.test_m6_comparison_service import make_run, append_pass, score_row
    store = InMemoryRunStore()
    for run_id in ("base", "cand"):
        make_run(store, run_id, case_ids=["c1"])
        append_pass(store, run_id, "old-" + run_id, [score_row("c1", passed=True)])
    service = ComparisonService(store)
    original = service.compare
    def rescore(*args, **kwargs):
        result = original(*args, **kwargs)
        append_pass(store, "base", "new-base", [score_row("c1", passed=False)])
        return result
    monkeypatch.setattr(service, "compare", rescore)
    result = service.evaluate_gate("cand", baseline_run_id="base", policy={
        "metric": "accuracy", "threshold": 1, "required_coverage": 1, "require_comparable": True,
    })
    assert result["report_refs"]["baseline"]["scoring_pass_id"] == "old-base"


def test_http_lite_gate_summary_uses_returned_fixed_pass(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from apps.api.app.main import create_app
    from motte_storage.run_store import InMemoryRunStore
    from tests.sdk.test_m6_comparison_service import make_run, append_pass, score_row
    store = InMemoryRunStore()
    make_run(store, "candidate", case_ids=["c1"])
    append_pass(store, "candidate", "old", [score_row("c1", passed=True)])
    app = create_app(store=store)
    original = app.state.comparisons.evaluate_gate
    def rescore(*args, **kwargs):
        result = original(*args, **kwargs)
        append_pass(store, "candidate", "new", [score_row("c1", passed=False)])
        return result
    monkeypatch.setattr(app.state.comparisons, "evaluate_gate", rescore)
    result = TestClient(app).post("/api/v1/gates", json={
        "run_id": "candidate", "policy": {"metric": "accuracy", "threshold": 1},
    }).json()
    assert result["report_refs"]["candidate"]["scoring_pass_id"] == "old"
    assert result["coverage_summary"]["metric_values"]["accuracy"] == 1.0


def test_cross_run_manual_lineage_cannot_grant_judge_eligibility():
    from motte_storage.run_store import InMemoryRunStore
    from tests.sdk.test_m6_comparison_service import make_run, append_pass, score_row
    store = InMemoryRunStore()
    make_run(store, "foreign", case_ids=["c1"])
    make_run(store, "candidate", case_ids=["c1"])
    append_pass(store, "foreign", "foreign-pass", [score_row("c1", passed=True)])
    store.scoring_passes.append({
        "id": "cross-run-manual", "run_id": "candidate", "source": "manual_revision",
        "scorer_id": "manual-revision", "scorer_version": "1", "source_run_revision": 1,
        "created_at": "2026-09-30T00:00:00Z", "summary": {},
        "manual_revision": {"source_pass_id": "foreign-pass"},
    }, [score_row("c1", passed=True)])
    service = ComparisonService(store)
    assert service._judge_gate_qualification("cross-run-manual")["gate_eligible"] is False
    assert service.evaluate_gate("candidate", policy={"threshold": 1})["passed"] is False
