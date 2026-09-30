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
    state = {"malformed": False, "output_cap": 512, "completion_tokens": 100, "timeout": 20}

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
            winner = "A" if "return a + b" in data["A"]["candidate_output"] else "B"
            evidence = data[winner]["evidence_index"]
            output = {"winner": winner, "criteria": [
                {"criterion_id": key, "preference": winner, "reason": "Correct Python addition",
                 "evidence": evidence} for key in CRITERIA]}
        else:
            evidence = ["event:" + ref["locator"] for ref in data["observation"]["event_refs"]]
            output = {"criteria": [
                {"criterion_id": key, "passed": True, "reason": "Correct Python addition",
                 "evidence": evidence} for key in CRITERIA]}
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
    assert "fake-key" not in json.dumps(result)
    assert "def add" not in json.dumps(result)
    if name == "check_judge_calibration":
        assert result["human_reviewed_count"] == 0
        assert result["candidate_only_count"] == 1
        assert result["gate_eligible"] is False
        assert result["qualification"] == "blocked_missing_human_review"
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
def test_judge_cap_comes_from_context_and_binds_all_budgets(wire, name, label, count):
    from motte_storage.resource_store import SQLiteResourceStore
    from motte_storage.run_store import SQLiteRunStore

    ctx, calls, state = wire
    state["output_cap"] = 1024
    getattr(module(), name)(ctx)
    assert all(row["body"]["max_tokens"] == 1024 for row in calls)
    path = ctx.root / label / "judge.db"
    assert SQLiteResourceStore(path).models.get(module().MODEL_ID)["max_output_tokens"] == 1024
    jobs = SQLiteRunStore(path).scoring_jobs.list_by_status()
    assert len(jobs) == 1
    job = jobs[0]
    assert job["provider_snapshot"]["max_output_tokens"] == 1024
    assert job["judge_spec"]["parameters"]["max_output_tokens"] == 1024
    assert job["budget"]["max_completion_tokens"] == count * 1024
    assert job["authorisation"]["max_total_tokens"] == 40_000 + count * 1024
    assert job["allowance"]["max_completion_tokens"] == count * 1024


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


@pytest.mark.parametrize("cap", [True, 0, 1025])
def test_judge_cap_cannot_exceed_harness_maximum(wire, cap):
    ctx, calls, state = wire
    state["output_cap"] = cap
    mod = module()
    assert hasattr(mod, "JudgeWitnessFailure")
    with pytest.raises(mod.JudgeWitnessFailure, match="judge_output_cap_invalid"):
        mod.check_judge_single(ctx)
    assert calls == []


def test_diagnostic_timeout_is_frozen_into_real_factory_calls(wire):
    ctx,calls,state=wire
    state['timeout']=60
    state['output_cap']=1024
    result=module().check_judge_pairwise(ctx)
    assert result['ok']
    assert all(row['timeout']==60 for row in calls)
