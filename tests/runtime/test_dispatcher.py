import pytest

from motte_sdk.dispatcher import RunDispatcher
from motte_sdk.service import RunService
from motte_storage import RunConflictError
from motte_storage.run_store import InMemoryRunStore


REPLAY_MANIFEST = {
    "provider": {"kind": "replay", "fixture": {"case-1": {"output": 1, "expected": 1}}},
    "execution": {
        "backend_id": "replay",
        "backend_version": "1",
        "capabilities": {"interactive": False, "safe_to_repeat": True},
    },
}


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
