"""M8 T03: a disposable PostgreSQL database proves process-level allocation ownership."""

from __future__ import annotations

import multiprocessing as mp
import os
import time
from queue import Empty
from typing import Any

import pytest

from motte_sdk.experiments import ExperimentError, ExperimentService
from motte_sdk.service import RunService
from motte_storage.factory import create_resource_store, create_run_store


def _create_in_process(
    dsn: str, payload: dict[str, Any], entered: Any, release: Any,
    calls: Any, outcomes: Any, *, pause: bool,
) -> None:
    store = create_run_store(storage="postgres", dsn=dsn)
    resources = create_resource_store(storage="postgres", dsn=dsn)
    service = ExperimentService(store, RunService(store), resources=resources)
    original = service._create_cell_run

    def tracked(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.put("first" if pause else "second")
        if pause:
            entered.set()
            if not release.wait(15):
                raise AssertionError("active allocation was not released")
        return original(*args, **kwargs)

    service._create_cell_run = tracked
    try:
        result = service.create(payload, request_key="m8-pg-shared-key")
        outcomes.put(("ok", [cell["run_id"] for cell in result["cells"]]))
    except BaseException as error:  # noqa: BLE001 - child error must reach test parent
        outcomes.put(("error", f"{type(error).__name__}: {error}"))


@pytest.mark.skipif(not os.environ.get("MOTTE_PG_DSN"), reason="real PostgreSQL required")
def test_two_postgres_processes_do_not_reclaim_active_cell(isolated_pg_database: str) -> None:
    from motte_sdk.direct_llm import import_builtin_dataset
    from motte_storage.migrations import upgrade

    dsn = isolated_pg_database
    upgrade(dsn)
    resources = create_resource_store(storage="postgres", dsn=dsn)
    import_builtin_dataset("direct-llm-exact-answer", resources=resources)
    resources.providers.put({
        "name": "local", "kind": "openai_compatible",
        "base_url": "https://local.test/v1", "model": "unused",
    })
    resources.models.put({
        "id": "model-a", "provider": "local", "model": "probe-1",
        "capabilities": {}, "max_output_tokens": 4096,
    })
    payload = {
        "experiment_id": "m8-pg-processes", "version": "v1",
        "task_ref": {"suite": "direct-llm", "scenario_version": "direct-llm-exact-answer@1"},
        "factors": {"model_profile": ["model-a"]}, "repeats": 1,
        "budget_policy": {"max_total_calls": 10},
        "created_by": "m8-disposable-pg-test", "reason": "process allocation race",
    }
    # pytest loads threaded libraries; fork inherited their locks and the
    # first child never reached allocation on Linux CI. Fresh spawn processes
    # exercise the actual cross-process database coordination without that
    # unrelated process-memory hazard.
    context = mp.get_context("spawn")
    entered, release = context.Event(), context.Event()
    calls, outcomes = context.Queue(), context.Queue()
    first = context.Process(target=_create_in_process,
                            args=(dsn, payload, entered, release, calls, outcomes),
                            kwargs={"pause": True})
    second = context.Process(target=_create_in_process,
                             args=(dsn, payload, entered, release, calls, outcomes),
                             kwargs={"pause": False})
    first.start()
    try:
        deadline = time.monotonic() + 20
        while not entered.wait(0.1):
            try:
                early = outcomes.get_nowait()
            except Empty:
                early = None
            if early is not None:
                raise AssertionError(f"first process failed before claim: {early}")
            if first.exitcode is not None:
                raise AssertionError(f"first process exited before claim: {first.exitcode}")
            if time.monotonic() >= deadline:
                raise AssertionError("first process never claimed its cell")
        second.start()
        # Without the allocation lock the second process can finish while the
        # first owns an unfinished claim. With it, the second waits here.
        second.join(timeout=2)
    finally:
        release.set()
        first.join(timeout=20)
        if second.pid is not None:
            second.join(timeout=20)
        for process in (first, second):
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    assert first.exitcode == second.exitcode == 0
    results = [outcomes.get(timeout=2) for _ in range(2)]
    assert all(result[0] == "ok" for result in results), results
    assert results[0][1] == results[1][1]
    observed = [calls.get(timeout=2)]
    try:
        observed.append(calls.get(timeout=0.2))
    except Empty:
        pass
    assert observed == ["first"]
    store = create_run_store(storage="postgres", dsn=dsn)
    assert len(store.runs.list()) == 1
    assert store.experiments.list_cells("m8-pg-processes", "v1")[0]["allocation_status"] == "allocated"

    service = ExperimentService(store, RunService(store), resources=resources)
    with pytest.raises(ExperimentError, match="REQUEST_KEY_CONFLICT"):
        service.create({**payload, "version": "v2"}, request_key="m8-pg-shared-key")
    assert store.experiments.get_spec("m8-pg-processes", "v2") is None
