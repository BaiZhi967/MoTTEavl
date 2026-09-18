import pytest

from motte_storage import RunConflictError
from motte_storage.run_store import InMemoryRunStore
from motte_sdk.dispatcher import RunDispatcher
from motte_sdk.service import RunService


REPLAY_MANIFEST = {
    "provider": {"kind": "replay", "fixture": {"case-1": {"output": 1, "expected": 1}}},
    "execution": {
        "backend_id": "replay",
        "backend_version": "1",
        "capabilities": {"interactive": False, "safe_to_repeat": True},
    },
}


def test_legacy_replay_projection_is_ephemeral_and_derives_selection():
    service = RunService(InMemoryRunStore())
    run = service.create_run("replay@1", REPLAY_MANIFEST, case_ids=["case-1"])
    legacy = {**run, "manifest": {"provider": REPLAY_MANIFEST["provider"]}, "case_ids": []}
    service.store.runs.update(legacy, expected_revision=run["revision"], expected_status="queued")
    before_claim = service.store.runs.get(run["id"])

    dispatcher = RunDispatcher(service)
    claimed = dispatcher.claim(run["id"])

    assert claimed is not None
    assert service.store.runs.get(run["id"])["manifest"] == before_claim["manifest"]
    completed = dispatcher.execute_claimed(claimed)
    assert completed["status"] == "completed"
    assert completed["case_ids"] == ["case-1"]


def test_dispatcher_is_the_claim_then_execute_authority():
    service = RunService(InMemoryRunStore())
    queued = service.create_run("replay@1", REPLAY_MANIFEST, case_ids=["case-1"])
    dispatcher = RunDispatcher(service)

    with pytest.raises(ValueError, match="atomically claimed"):
        dispatcher.execute_claimed(service.store.runs.get(queued["id"]))

    completed = dispatcher.dispatch(queued["id"])
    assert completed["status"] == "completed"
    assert completed["scores"] == [{"case_id": "case-1", "passed": True}]
    with pytest.raises(RunConflictError, match="not queued"):
        dispatcher.dispatch(queued["id"])
