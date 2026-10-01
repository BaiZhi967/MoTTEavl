"""Judge witnesses use real durable services with a hermetic HTTP boundary."""
from __future__ import annotations

import importlib
import json
import socket
from types import SimpleNamespace
from urllib.request import OpenerDirector

import pytest

from tests.provider.test_openai_compatible import FakeResponse

CRITERIA = ("task_completion", "constraint_adherence", "evidence_grounding")


@pytest.fixture
def wire(tmp_path, monkeypatch):
    def escape(*args, **kwargs):
        pytest.fail("offline judge test attempted an unguarded network connection")

    monkeypatch.setattr(socket.socket, "connect", escape)
    monkeypatch.setattr(socket, "create_connection", escape)
    monkeypatch.setattr(OpenerDirector, "open", escape)
    monkeypatch.setattr("motte_provider.config.resolve_api_key", lambda *a, **kw: "fake-key")
    calls = []
    state = {"malformed": False, "output_cap": 512, "completion_tokens": 100, "timeout": 20,
             "position_bias": False, "reject_calls": set(), "reject_criteria": {},
             "vary_explanation": False}

    def responder(request, **kwargs):
        body = json.loads(request.data)
        calls.append({"session": request.get_header("X-opencode-session"), "body": body,"timeout":kwargs.get("timeout")})
        assert request.full_url == "https://opencode.ai/zen/go/v1/chat/completions"
        assert request.get_header("User-agent") == "MoTTEavl/0.1.0"
        assert body["model"] == "space-bunny-free" and body["max_tokens"] <= state["output_cap"]
        assert not body.get("tools")
        prompt = body["messages"][-1]["content"]
        data = json.loads(prompt.split("<<<CANDIDATE_DATA\n", 1)[1].split("\nCANDIDATE_DATA>>>", 1)[0])
        calls[-1]["data"] = data
        if "A" in data:
            winner = "A" if state["position_bias"] or "return a + b" in data["A"]["candidate_output"] else "B"
            evidence = data[winner]["evidence_index"]
            output = {"winner": winner, "criteria": [
                {"criterion_id": key, "preference": winner, "reason": "Correct Python addition",
                 "evidence": evidence} for key in CRITERIA]}
        else:
            evidence = ["event:" + ref["locator"] for ref in data["observation"]["event_refs"]]
            output = {"criteria": [
                {"criterion_id": key,
                 "passed": len(calls) not in state["reject_calls"]
                 and key not in state["reject_criteria"].get(len(calls), set()),
                 "reason": f"Synthetic judgement {len(calls)}" if state["vary_explanation"] else "Synthetic judgement",
                 "evidence": evidence} for key in CRITERIA]}
            if state["vary_explanation"] and len(calls) == 2:
                output["criteria"].reverse()
        return FakeResponse({"id": f"synthetic-response-{len(calls)}", "model": body["model"],
            "choices": [{"message": {"content": "bad json" if state["malformed"] else json.dumps(output)},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": state["completion_tokens"],
                      "total_tokens": 100 + state["completion_tokens"]}})

    monkeypatch.setattr("motte_provider.transport._safe_urlopen", responder)
    monkeypatch.setattr("motte_provider.transport._bounded_urlopen", responder)
    ctx = SimpleNamespace(root=tmp_path, provider_config=lambda: {
        "kind": "openai_compatible", "base_url": "https://opencode.ai/zen/go/v1",
        "model": "space-bunny-free", "credentials": "offline-test", "timeout": state["timeout"],
        "max_retries": 0, "max_output_tokens": state["output_cap"], "identity_policy": "require_match",
    })
    return ctx, calls, state


def module():
    assert importlib.util.find_spec("scripts.opencode_go_judge_checks") is not None
    return importlib.import_module("scripts.opencode_go_judge_checks")


@pytest.mark.parametrize(("name", "count"), [
    ("check_judge_single", 1), ("check_judge_pairwise", 2), ("check_judge_calibration", 2),
])
def test_judge_witness_is_bounded_durable_and_honest(wire, name, count):
    ctx, calls, _ = wire
    result = getattr(module(), name)(ctx)
    assert result["ok"] is True
    assert result["billed_calls"] == count == len(calls)
    assert len({row["session"] for row in calls}) == count
    assert all(row["session"] for row in calls)
    assert result["report_immutable"] is True
    assert all(row["body"]["max_tokens"] == 512 and row["timeout"] == 20 for row in calls)
    assert "fake-key" not in json.dumps(result)
    assert "def add" not in json.dumps(result)
    if name == "check_judge_calibration":
        assert result["human_reviewed_count"] == 0
        assert result["candidate_only_count"] == 1
        assert result["gate_eligible"] is False
        assert result["qualification"] == "blocked_missing_human_review"
        assert result["repeat_stable"] is True
        assert result["model_quality_passed"] is True
        assert result["qualified"] is False
        assert result["criterion_labels"] == [dict.fromkeys(CRITERIA, True)] * 2
        from motte_storage.run_store import SQLiteRunStore

        store = SQLiteRunStore(ctx.root / "judge-calibration" / "judge.db")
        execution = store.calibrations.list_executions("bounded-code-review")[0]
        child = store.scoring_jobs.get(execution.child_job_ids[0])
        for bundle in child["inputs"].values():
            for ref in bundle["field_values"]["event_refs"]:
                events = store.events.list_for_run(ref["run_id"])
                assert any(str(event["seq"]) == ref["locator"] for event in events)
    else:
        assert result["subject_source"] == "synthetic_frozen_fixture"
        if name == "check_judge_pairwise":
            assert calls[0]["data"]["A"] == calls[1]["data"]["B"]
            assert calls[0]["data"]["B"] == calls[1]["data"]["A"]
            assert calls[0]["data"]["A"] != calls[0]["data"]["B"]


def test_pairwise_rejects_position_biased_winner(wire):
    ctx, calls, state = wire
    state["position_bias"] = True
    mod = module()
    with pytest.raises(mod.JudgeWitnessFailure, match="judge_pairwise_code_preference_mismatch"):
        mod.check_judge_pairwise(ctx)
    assert len(calls) == 2


@pytest.mark.parametrize("reject_calls", [{2}, {1, 2}])
def test_candidate_calibration_honestly_reports_inconsistent_or_incorrect_grades(wire, reject_calls):
    ctx, calls, state = wire
    state["reject_calls"] = reject_calls
    result = module().check_judge_calibration(ctx)
    assert len(calls) == 2
    assert result["ok"] is True
    assert result["repeat_stable"] is (reject_calls == {1, 2})
    assert result["instability_gate_reason_present"] is (reject_calls == {2})
    assert result["model_quality_passed"] is False
    assert result["gate_eligible"] is result["qualified"] is False
    assert result["human_reviewed_count"] == 0
    assert result["report_immutable"] is result["ledger_complete"] is True
    assert result["criterion_labels"] == [dict.fromkeys(CRITERIA, index not in reject_calls)
                                           for index in (1, 2)]


def test_calibration_reports_one_changed_criterion_without_response_text(wire):
    ctx, calls, state = wire
    state["reject_criteria"] = {2: {"evidence_grounding"}}
    result = module().check_judge_calibration(ctx)
    assert result["ok"] is True
    assert result["repeat_stable"] is result["model_quality_passed"] is False
    assert result["criterion_labels"][1] == {
        "task_completion": True, "constraint_adherence": True, "evidence_grounding": False,
    }
    diagnostics = result["diagnostics"]
    assert diagnostics["calibration_call_count"] == 2
    assert diagnostics["repeat_measured"] == diagnostics["repeat_sample_count"] == 1
    assert diagnostics["repeat_stable_count"] == diagnostics["repeat_rate_is_one"] == 0
    assert diagnostics["repeat_unstable_count"] == 1
    assert diagnostics["criterion_disagreement_count"] == diagnostics["criterion_false_count"] == 1
    assert diagnostics["criterion_missing_count"] == diagnostics["calibration_invalid_call_count"] == 0
    assert diagnostics["parse_failure_count"] == diagnostics["non_scored_count"] == 0
    assert diagnostics["job_status_code"] == module().JOB_STATUS_CODES["completed"]
    assert all(type(value) is int and value >= 0 for value in diagnostics.values())
    assert not any(value in json.dumps(diagnostics) for value in ("evidence_grounding", "Synthetic", "fake-key"))
    assert len(calls) == 2


def test_calibration_repeat_ignores_rationale_and_criterion_order(wire):
    ctx, calls, state = wire
    state["vary_explanation"] = True
    result = module().check_judge_calibration(ctx)
    assert result["ok"] is result["repeat_stable"] is True
    assert len(calls) == 2


@pytest.mark.parametrize(("field", "value", "code"), [
    ("repeat_stability", {"measured": True, "samples": 1, "stable": 0, "rate": 0.0,
                          "unstable_samples": ["python-add-review"]}, "judge_calibration_repeat_metric_mismatch"),
    ("gate_eligible", True, "judge_calibration_not_honest"),
    ("qualified", True, "judge_calibration_not_honest"),
    ("human_reviewed_count", 1, "judge_calibration_not_honest"),
])
def test_calibration_rejects_report_metrics_or_gate_disagreeing_with_calls(wire, monkeypatch, field, value, code):
    ctx, calls, _ = wire
    mod = module()
    original = mod.JudgeCalibrationService.publish_report

    def tampered(service, *args, **kwargs):
        record = original(service, *args, **kwargs)
        return record.model_copy(update={"report": record.report.model_copy(update={field: value})})

    monkeypatch.setattr(mod.JudgeCalibrationService, "publish_report", tampered)
    with pytest.raises(mod.JudgeWitnessFailure, match=code) as caught:
        mod.check_judge_calibration(ctx)
    assert caught.value.diagnostics["calibration_call_count"] == 2
    assert caught.value.diagnostics["criterion_disagreement_count"] == 0
    assert len(calls) == 2


def test_calibration_rejects_unstable_report_missing_its_gate_reason(wire, monkeypatch):
    ctx, calls, state = wire
    state["reject_calls"] = {2}
    mod = module()
    original = mod.JudgeCalibrationService.publish_report

    def tampered(service, *args, **kwargs):
        record = original(service, *args, **kwargs)
        reasons = [reason for reason in record.report.reasons if not reason.startswith("repeat stability ")]
        return record.model_copy(update={"report": record.report.model_copy(update={"reasons": reasons})})

    monkeypatch.setattr(mod.JudgeCalibrationService, "publish_report", tampered)
    with pytest.raises(mod.JudgeWitnessFailure, match="judge_calibration_gate_reason_mismatch"):
        mod.check_judge_calibration(ctx)
    assert len(calls) == 2


@pytest.mark.parametrize("wrong", [False, True])
def test_complete_stable_reviewed_contract_fixture_still_requires_correct_labels(wire, wrong):
    # Reuse explicitly simulated review-provenance fixtures, never real human acceptance.
    from tests.evaluators.test_judge_calibration import (
        build_calibration_report, calibration_with, criteria_json, human_samples,
        judge_spec, matching_observed, measurement_calls, outcome_for,
    )

    spec = judge_spec(calibration_version="1")
    calibration = calibration_with(human_samples(6), judge_spec_sha256=spec.spec_sha256)
    observed = matching_observed(calibration, spec)
    if wrong:
        observed = {sample.sample_id: outcome_for(spec, criteria_json({
            key: not value for key, value in sample.expected_criteria.items()
        })) for sample in calibration.samples}
    report = build_calibration_report(calibration, observed=observed,
                                      calls=measurement_calls(calibration, observed))
    assert report.human_reviewed_count == 30 and report.coverage["covered"] is True
    assert report.repeat_stability["rate"] == report.position_swap["rate"] == 1.0
    assert report.disagreement_rate == float(wrong)
    assert report.qualified is report.gate_eligible is (not wrong)
    assert all(reason.startswith("disagreement rate") for reason in report.reasons)
    assert wire[1] == []


@pytest.mark.parametrize("name", ["check_judge_single", "check_judge_pairwise", "check_judge_calibration"])
def test_malformed_judgement_is_not_a_pass_or_retry(wire, name):
    ctx, calls, state = wire
    state["malformed"] = True
    with pytest.raises(RuntimeError, match="judge_"):
        getattr(module(), name)(ctx)
    assert 1 <= len(calls) <= module().CALL_CAPS[name]


@pytest.mark.parametrize(("name", "label", "count"), [
    ("check_judge_single", "judge-single", 1),
    ("check_judge_pairwise", "judge-pairwise", 2),
    ("check_judge_calibration", "judge-calibration", 2),
])
@pytest.mark.parametrize("output_cap", [1024, 4096])
def test_judge_cap_comes_from_context_and_binds_all_budgets(wire, name, label, count, output_cap):
    from motte_storage.resource_store import SQLiteResourceStore
    from motte_storage.run_store import SQLiteRunStore

    ctx, calls, state = wire
    state["output_cap"] = output_cap
    getattr(module(), name)(ctx)
    assert all(row["body"]["max_tokens"] == output_cap for row in calls)
    path = ctx.root / label / "judge.db"
    assert SQLiteResourceStore(path).models.get(module().MODEL_ID)["max_output_tokens"] == output_cap
    jobs = SQLiteRunStore(path).scoring_jobs.list_by_status()
    assert len(jobs) == 1
    job = jobs[0]
    assert job["provider_snapshot"]["max_output_tokens"] == output_cap
    assert job["judge_spec"]["parameters"]["max_output_tokens"] == output_cap
    assert job["budget"]["max_completion_tokens"] == count * output_cap
    assert job["authorisation"]["max_total_tokens"] == 40_000 + count * output_cap
    assert job["allowance"]["max_completion_tokens"] == count * output_cap


def test_failed_judge_preserves_only_safe_numeric_diagnostics(wire):
    ctx, calls, state = wire
    state.update(malformed=True, completion_tokens=512)
    mod = module()
    assert hasattr(mod, "JudgeWitnessFailure")
    with pytest.raises(mod.JudgeWitnessFailure) as caught:
        mod.check_judge_pairwise(ctx)
    error = caught.value
    assert error.code == error.error_class == "judge_worker_not_completed"
    assert error.diagnostics["parse_failure_count"] == 2
    assert error.diagnostics["billed_calls"] == 2
    assert error.diagnostics["completion_tokens"] == 1024
    assert error.diagnostics["calls_at_output_cap"] == 2
    assert error.diagnostics["job_status_code"] == mod.JOB_STATUS_CODES["failed"]
    assert all(type(value) is int for value in error.diagnostics.values())
    assert "bad json" not in json.dumps(error.diagnostics)
    assert "fake-key" not in str(error)
    assert len(calls) == 2


def test_witness_failure_rejects_unknown_codes_and_diagnostics(wire):
    mod = module()
    assert hasattr(mod, "JudgeWitnessFailure")
    error = mod.JudgeWitnessFailure("secret-provider-text", {
        "raw_response": "secret-response", "parse_failure_count": "secret-key",
        "billed_calls": 2, "completion_tokens": True,
    })
    assert error.code == "judge_witness_failure"
    assert error.diagnostics == {"billed_calls": 2}
    assert "secret" not in str(error)


@pytest.mark.parametrize("cap", [True, 0, 4097])
def test_judge_cap_cannot_exceed_harness_maximum(wire, cap):
    ctx, calls, state = wire
    state["output_cap"] = cap
    mod = module()
    assert hasattr(mod, "JudgeWitnessFailure")
    with pytest.raises(mod.JudgeWitnessFailure, match="judge_output_cap_invalid"):
        mod.check_judge_single(ctx)
    assert calls == []


@pytest.mark.parametrize("timeout", [60, 120])
@pytest.mark.parametrize(("name", "label"), [
    ("check_judge_single", "judge-single"),
    ("check_judge_pairwise", "judge-pairwise"),
    ("check_judge_calibration", "judge-calibration"),
])
def test_diagnostic_timeout_is_frozen_into_real_factory_calls(wire, timeout, name, label):
    from motte_storage.run_store import SQLiteRunStore

    ctx, calls, state = wire
    state["timeout"] = timeout
    result = getattr(module(), name)(ctx)
    assert result["ok"]
    assert all(row["timeout"] == timeout for row in calls)
    job = SQLiteRunStore(ctx.root / label / "judge.db").scoring_jobs.list_by_status()[0]
    assert job["provider_snapshot"]["transport"]["timeout"] == timeout


@pytest.mark.parametrize("timeout", [True, 0, 121, float("inf"), float("nan")])
def test_judge_timeout_cannot_exceed_harness_maximum(wire, timeout):
    ctx, calls, state = wire
    state["timeout"] = timeout
    mod = module()
    with pytest.raises(mod.JudgeWitnessFailure, match="judge_request_timeout_invalid"):
        mod.check_judge_single(ctx)
    assert calls == []
