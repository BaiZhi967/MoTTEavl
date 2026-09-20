"""R3-06/07: terminal Trial dispositions and fail-closed frozen plan ownership."""
from copy import deepcopy
from pathlib import Path

import pytest

from motte_sdk.benchmark_plugins import import_terminal_bench_trials
from motte_storage.integrity import RunConflictError
from tests.integration.test_harbor_round2_service import (
    _build_store, _create, _foreign_outcome, _inputs_for,
)


def test_queued_cancel_persists_all_trial_dispositions(tmp_path: Path) -> None:
    service, record = _build_store(tmp_path)
    inputs = _inputs_for(record, run_id="queued-cancel")
    _create(service, inputs, run_id="queued-cancel")
    view = service.cancel("queued-cancel")
    rows = service.store.trials.list_for_run("queued-cancel")
    assert view["status"] == "cancelled"
    assert len(rows) == 4
    assert {row["result"]["disposition"] for row in rows} == {"cancelled"}
    assert len(view["scores"]) == 4


@pytest.mark.parametrize("phase,disposition", [("preparing", "not_attempted"), ("running", "indeterminate")])
def test_failure_disposes_trials_without_inventing_execution_evidence(
    tmp_path: Path, phase: str, disposition: str,
) -> None:
    service, record = _build_store(tmp_path)
    inputs = _inputs_for(record, run_id="failed-run")
    _create(service, inputs, run_id="failed-run")
    service._transition("failed-run", "preparing")
    if phase == "running":
        def launch(_run):
            raise RuntimeError("fixture launch/collection failed")
        view = service.execute_external_job("failed-run", service._load("failed-run"), launch)
    else:
        view = service._fail("failed-run", RuntimeError("fixture preparation failed"))
    rows = service.store.trials.list_for_run("failed-run")
    assert view["status"] == "failed"
    assert len(rows) == 4
    assert {row["result"]["disposition"] for row in rows} == {disposition}
    assert len(view["scores"]) == 4


def test_quarantined_unknown_payload_disposes_only_missing_trials(tmp_path: Path) -> None:
    service, record = _build_store(tmp_path)
    inputs = _inputs_for(record, run_id="quarantine")
    _create(service, inputs, run_id="quarantine")
    real_id = inputs["trials"][0]["trial_id"]
    outcome = _foreign_outcome(service, inputs, trial_id=real_id)
    outcome["results"].extend(_foreign_outcome(service, inputs, trial_id="unknown")["results"])
    view = service.execute_external_job("quarantine", service._load("quarantine"), lambda _: outcome)
    assert view["status"] == "failed"
    rows = service.store.trials.list_for_run("quarantine")
    assert len(rows) == 4
    assert service.store.trials.get("unknown") is None
    real = service.store.trials.get(real_id)["result"]
    assert real["disposition"] == "succeeded"
    assert not real.get("synthesized_by")
    missing = [row for row in rows if row["trial_id"] != real_id]
    assert all(row["result"] and row["result"]["disposition"] == "indeterminate" for row in missing)
    assert len(view["scores"]) == 4


@pytest.mark.parametrize("boundary", ["create", "execute", "import"])
@pytest.mark.parametrize("mismatch", ["foreign_run", "stored_owner"])
def test_entire_plan_is_validated_before_any_mutation(
    tmp_path: Path, boundary: str, mismatch: str,
) -> None:
    service, record = _build_store(tmp_path)
    victim_inputs = _inputs_for(record, run_id="victim")
    _create(service, victim_inputs, run_id="victim")
    service.store.trials.create_plans(victim_inputs["trials"])
    victim_before = service.store.trials.list_for_run("victim")
    inputs = _inputs_for(record, run_id="incoming")
    plan = inputs["manifest"]["external_benchmark"]["runner_config"]["plan"]["trials"]
    # The first unit is valid and new: rejecting only while iterating would leak it.
    if mismatch == "foreign_run":
        plan[-1] = deepcopy(victim_inputs["trials"][0])
    else:
        plan[-1]["trial_id"] = victim_inputs["trials"][0]["trial_id"]
    bad_run = {"id": "incoming", "manifest": inputs["manifest"], "status": "queued", "case_ids": inputs["case_ids"]}
    incoming_before = None
    if boundary == "execute":
        # A legacy malformed persisted manifest must still be stopped at dispatch.
        service.store.runs.create({**bad_run, "scenario_version": inputs["scenario_version"]})
        incoming_before = service.store.runs.get("incoming")
    launches = []
    def launch(_run):
        launches.append(_run)
        return _foreign_outcome(service, inputs, trial_id=plan[-1]["trial_id"])
    with pytest.raises((ValueError, RunConflictError), match="[Tt]rial|[Pp]lan"):
        if boundary == "create":
            _create(service, inputs, run_id="incoming")
        elif boundary == "execute":
            service.execute_external_job("incoming", service._load("incoming"), launch)
        else:
            import_terminal_bench_trials(service.store, bad_run, [{
                "trial_id": plan[-1]["trial_id"],
                "result": {"trial_id": plan[-1]["trial_id"], "disposition": "succeeded"},
            }])
    assert launches == []
    assert service.store.trials.list_for_run("victim") == victim_before
    assert service.store.trials.list_for_run("incoming") == []
    assert service.store.runs.get("incoming") == incoming_before


def test_profile_stale_leaves_no_pending_trial(tmp_path: Path) -> None:
    service, record = _build_store(tmp_path)
    inputs = _inputs_for(record, run_id="stale")
    _create(service, inputs, run_id="stale")
    view = service.mark_profile_stale("stale", "PROFILE_CHANGED")
    assert view["status"] == "profile_stale"
    assert len(view["scores"]) == 4
    assert {row["result"]["disposition"] for row in service.store.trials.list_for_run("stale")} == {"not_attempted"}
