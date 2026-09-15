"""PostgreSQL 端到端集成测试：空库 → alembic migration → API create → worker execute → 查询 Run/Trace/Score。

仅在设置 MOTTE_PG_DSN 时运行（CI 的 postgres service 会设置；本地默认跳过）。
"""
import os

import pytest

psycopg = pytest.importorskip("psycopg")

from motte_storage.migrations import current, downgrade, upgrade  # noqa: E402
from motte_storage.postgres import create_postgres_run_store  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("MOTTE_PG_DSN"), reason="set MOTTE_PG_DSN to run PostgreSQL integration tests"
)

REPLAY_MANIFEST = {
    "provider": {
        "kind": "replay",
        "fixture": {
            "case-1": {"output": {"n": 1}, "expected": {"n": 1}},
            "case-2": {"output": {"n": 2}, "expected": {"n": 9}},
        },
    }
}


@pytest.fixture(scope="module")
def dsn() -> str:
    from motte_storage.postgres import normalize_dsn

    return normalize_dsn(os.environ["MOTTE_PG_DSN"])


@pytest.fixture(scope="module")
def migrated_dsn(dsn):
    """空库起底：幂等 DROP 全部已知表后 alembic upgrade 到 head。"""
    import importlib.util
    from pathlib import Path

    version_file = Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0001_initial.py"
    spec = importlib.util.spec_from_file_location("v0001", version_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            for statement in module.DOWN_STATEMENTS:
                cursor.execute(statement)
            cursor.execute("DROP TABLE IF EXISTS schema_migrations")
            cursor.execute("DROP TABLE IF EXISTS alembic_version")
        connection.commit()

    assert current(dsn) is None
    assert upgrade(dsn) == "0001_initial"
    assert upgrade(dsn) == "0001_initial"  # 幂等
    return dsn


def test_migration_rollback_and_reapply(migrated_dsn):
    assert downgrade(migrated_dsn) is None
    assert current(migrated_dsn) is None
    assert upgrade(migrated_dsn) == "0001_initial"


def test_full_run_lifecycle_on_postgres(migrated_dsn):
    from apps.worker.motte_worker.runtime import WorkerLoop
    from fastapi.testclient import TestClient
    from apps.api.app.main import create_app
    from motte_sdk.service import RunService

    store = create_postgres_run_store(migrated_dsn)
    client = TestClient(create_app(store))
    created = client.post(
        "/api/v1/runs",
        json={"scenario_version": "replay@1", "manifest": REPLAY_MANIFEST, "case_ids": ["case-1", "case-2"]},
    )
    assert created.status_code == 202
    run_id = created.json()["id"]
    assert created.json()["status"] == "queued"

    result = WorkerLoop(RunService(create_postgres_run_store(migrated_dsn))).claim_and_execute()
    assert result["id"] == run_id
    assert result["status"] == "completed"
    assert result["scores"] == [
        {"case_id": "case-1", "passed": True},
        {"case_id": "case-2", "passed": False},
    ]

    fresh = RunService(create_postgres_run_store(migrated_dsn))
    persisted = fresh.get_run(run_id)
    assert persisted["status"] == "completed"
    assert [row["case_id"] for row in fresh.store.case_runs.list_for_run(run_id)] == ["case-1", "case-2"]
    events = fresh.events(run_id)
    assert [event["type"] for event in events] == [
        "queued",
        "preparing",
        "running",
        "model_response",
        "model_response",
        "collecting",
        "scoring",
        "score",
        "score",
        "completed",
    ]
    assert [event["seq"] for event in fresh.events_after(run_id, 8)] == [9, 10]

    rescored = client.post(f"/api/v1/runs/{run_id}/rescore").json()
    assert rescored["rescored"] is True
    retried = client.post(f"/api/v1/runs/{run_id}/retry")
    assert retried.status_code == 409  # completed 不可 retry


def test_worker_recovery_on_postgres(migrated_dsn):
    from apps.worker.motte_worker.runtime import WorkerLoop
    from motte_sdk.service import RunService

    service = RunService(create_postgres_run_store(migrated_dsn))
    run = service.create_run("replay@1", REPLAY_MANIFEST, case_ids=["case-1", "case-2"])
    crashed = service.store.runs.get(run["id"])
    crashed["status"] = "running"
    service.store.runs.save(crashed)
    service.store.case_runs.upsert(
        {"run_id": run["id"], "case_id": "case-1", "result": {"n": 1}, "expected": {"n": 1}}
    )

    worker = WorkerLoop(RunService(create_postgres_run_store(migrated_dsn)))
    assert worker.recover_interrupted() == [run["id"]]
    result = worker.claim_and_execute()
    assert result["status"] == "completed"
    rows = RunService(create_postgres_run_store(migrated_dsn)).store.case_runs.list_for_run(run["id"])
    assert [row["case_id"] for row in rows] == ["case-1", "case-2"]


def test_claim_is_exclusive_on_postgres(migrated_dsn):
    store = create_postgres_run_store(migrated_dsn)
    store.runs.save({"id": "run-exclusive", "status": "queued", "manifest": {}, "case_ids": []})
    first = store.runs.claim_next_queued()
    assert first["id"] == "run-exclusive"
    assert first["status"] == "preparing"
    assert store.runs.claim_next_queued() is None
    store.runs.save({**first, "status": "cancelled"})
