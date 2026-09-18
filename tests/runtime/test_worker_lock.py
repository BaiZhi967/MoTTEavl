from multiprocessing import get_context

import pytest

from apps.worker.motte_worker.coordination import WorkerAlreadyRunning, worker_execution_lock


def _hold_lock(path, ready, release):
    with worker_execution_lock(path):
        ready.set()
        release.wait(10)


def test_sqlite_execution_lock_is_exclusive_between_processes(tmp_path):
    path = tmp_path / "runs.db"
    context = get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(target=_hold_lock, args=(str(path), ready, release))
    process.start()
    try:
        assert ready.wait(10), "child did not acquire worker lock"
        with pytest.raises(WorkerAlreadyRunning):
            with worker_execution_lock(path):
                pass
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.kill()
    assert process.exitcode == 0


def test_worker_loop_binds_lock_to_injected_postgres_store(monkeypatch):
    from apps.worker.motte_worker.runtime import WorkerLoop
    from motte_sdk.service import RunService
    from motte_storage.postgres import _PgRuns
    from motte_storage.run_store import RunStore

    service = RunService(
        RunStore(
            runs=_PgRuns("postgresql://injected.example/motte"),
            case_runs=object(), events=object(), scores=object(),
        )
    )
    loop = WorkerLoop(service)
    guard = loop._execution_guard()
    assert guard._backend == "postgres"
    assert guard._postgres_dsn == "postgresql://injected.example/motte"


def test_worker_entrypoint_uses_cached_store_lock_identity(tmp_path, monkeypatch):
    from apps.worker.motte_worker.__main__ import main
    from apps.worker.motte_worker.tasks import configure_service, get_service

    path_a = tmp_path / "store-a.db"
    path_b = tmp_path / "store-b.db"
    configure_service(path_a)
    service = get_service()
    service.create_run(
        "replay@1",
        {"provider": {"kind": "replay", "fixture": {"case-1": {"output": 1}}}},
        case_ids=["case-1"],
    )
    monkeypatch.setenv("MOTTE_DB_PATH", str(path_b))
    with worker_execution_lock(path_a):
        assert main(["--once", "--quiet"]) == 75
    configure_service(None)


def test_worker_entrypoint_refuses_second_executor(tmp_path, capsys):
    from apps.worker.motte_worker.__main__ import main

    path = tmp_path / "runs.db"
    with worker_execution_lock(path):
        assert main(["--db", str(path), "--once"]) == 75
    assert "worker_start_refused" in capsys.readouterr().err

    # The lock is released when its owner exits.
    assert main(["--db", str(path), "--once", "--quiet"]) == 0
