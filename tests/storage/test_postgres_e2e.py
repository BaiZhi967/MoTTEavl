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

    versions = Path(__file__).resolve().parents[2] / "migrations" / "versions"
    modules = []
    for version in ("0002_platform_integrity", "0001_initial"):
        version_file = versions / f"{version}.py"
        spec = importlib.util.spec_from_file_location(version, version_file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules.append(module)

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            for module in modules:
                for statement in module.DOWN_STATEMENTS:
                    # 0002's revision column does not exist on an already-downgraded 0001 database.
                    if "ALTER TABLE runs DROP COLUMN" in statement:
                        cursor.execute("SELECT to_regclass('public.runs')")
                        if cursor.fetchone()[0] is None:
                            continue
                    cursor.execute(statement)
            cursor.execute("DROP TABLE IF EXISTS schema_migrations")
            cursor.execute("DROP TABLE IF EXISTS alembic_version")
        connection.commit()

    assert current(dsn) is None
    assert upgrade(dsn) == "0002_platform_integrity"
    assert upgrade(dsn) == "0002_platform_integrity"  # 幂等
    return dsn


def test_migration_rollback_and_reapply(migrated_dsn):
    assert downgrade(migrated_dsn) == "0001_initial"
    assert current(migrated_dsn) == "0001_initial"
    with psycopg.connect(migrated_dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO runs(id, payload) VALUES "
                "('run-legacy-pg', '{\"id\":\"run-legacy-pg\",\"status\":\"cancelled\"}'::jsonb)"
            )
            cursor.execute(
                "INSERT INTO scores(run_id, case_id, ordinal, payload) VALUES "
                "('run-legacy-pg', 'case-legacy', 1, '{\"case_id\":\"case-legacy\",\"passed\":true}'::jsonb)"
            )
    assert upgrade(migrated_dsn) == "0002_platform_integrity"
    store = create_postgres_run_store(migrated_dsn)
    legacy = store.runs.get("run-legacy-pg")
    assert legacy["revision"] == 0 and legacy["schema_version"] == 1
    assert store.scores.list_for_run(legacy["id"]) == [{"case_id": "case-legacy", "passed": True}]
    assert store.runs.update(legacy, expected_revision=0)["revision"] == 1


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
        "scoring_pass_created",
        "completed",
    ]
    assert [event["seq"] for event in fresh.events_after(run_id, 8)] == [9, 10, 11]

    rescored_response = client.post(f"/api/v1/runs/{run_id}/rescore")
    assert rescored_response.status_code == 200
    rescored = rescored_response.json()
    assert rescored["status"] == "completed"
    assert len(fresh.store.scoring_passes.list_for_run(run_id)) == 2
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


def test_integrity_repositories_on_postgres(migrated_dsn):
    from motte_storage.integrity import RunConflictError

    store = create_postgres_run_store(migrated_dsn)
    run = store.runs.create({"id": "run-integrity-pg", "status": "queued"},
                            event={"type": "queued"})
    assert run["revision"] == 1
    with pytest.raises(RunConflictError):
        store.runs.create({"id": run["id"], "status": "queued"})
    claimed = store.runs.claim(run["id"])
    assert claimed["revision"] == 2
    assert [row["type"] for row in store.events.list_for_run(run["id"])] == ["queued", "preparing"]
    with pytest.raises(RunConflictError):
        store.runs.transition(run["id"], expected_revision=1,
                              expected_status="queued", status="running")
    attempt = store.attempts.begin({"run_id": run["id"], "case_id": "case-1"})
    dispatched = store.attempts.transition(attempt["id"], expected_revision=1,
                                           expected_status="prepared", status="dispatching")
    completed = store.attempts.complete(
        dispatched["id"], expected_revision=2, case_run={
            "run_id": run["id"], "case_id": "case-1", "result": "answer"
        }, event={"type": "model_response", "case_id": "case-1"},
    )
    assert completed["status"] == "succeeded"
    scoring_pass = store.scoring_passes.append(
        {"run_id": run["id"], "scorer_version": "v1"},
        [{"case_id": "case-1", "passed": True}],
        expected_run_revision=2, expected_run_status="preparing",
    )
    assert store.scoring_passes.current(run["id"]) == scoring_pass
    assert store.score_sets.list_for_pass(scoring_pass["id"]) == [{"case_id": "case-1", "passed": True}]
    command = store.commands.create({"run_id": run["id"], "content": "continue"})
    delivered = store.commands.transition(command["id"], expected_revision=1,
                                          expected_status="queued", status="delivered")
    assert store.commands.get(command["id"]) == delivered
    fresh = create_postgres_run_store(migrated_dsn)
    assert fresh.attempts.get(attempt["id"])["status"] == "succeeded"
    assert fresh.commands.list_for_run(run["id"]) == [delivered]


def test_postgres_unmanaged_versions_are_immutable(migrated_dsn):
    from motte_storage.resource_store import PostgresResourceStore, ResourceConflictError

    store = PostgresResourceStore(migrated_dsn)
    for repository, record, key in (
        (store.datasets, {"name": "custom-pg", "version": "v1", "cases": []},
         ("custom-pg", "v1")),
        (store.scenarios, {"name": "custom-pg", "version": "v1", "cases": []},
         ("custom-pg", "v1")),
        (store.price_tables, {"model_id": "m-custom-pg", "version": "v1", "price": 1},
         ("m-custom-pg", "v1")),
    ):
        assert repository.put(record) == record
        assert repository.put(record) == record
        with pytest.raises(ResourceConflictError):
            repository.put({**record, "content": "different"})
        with pytest.raises(ResourceConflictError):
            repository.delete(*key)
        assert repository.get(*key) == record
    assert store.models.put({"id": "model-pg", "provider": "p1"})["provider"] == "p1"
    assert store.models.put({"id": "model-pg", "provider": "p2"})["provider"] == "p2"
