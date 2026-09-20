"""R4-03/04: immutable prepared semantics and first-writer PostgreSQL races.

PG cases use MOTTE_R4_PG_DSN and an owned temporary schema, never the default DB.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import json
import os
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest

from motte_sdk.benchmark_catalog import (
    BenchmarkCatalog,
    dataset_to_payload,
    prepare_external_dataset,
)
from motte_storage.benchmark_datasets import (
    MemoryBenchmarkDatasets,
    RevisionConflictError,
    SQLiteBenchmarkDatasets,
)


def _dataset(default_split="val"):
    row = {"id": "logic-1", "subject": "logic", "question": "1+1=?",
           "A": "1", "B": "2", "C": "3", "D": "4", "answer": "B"}
    return prepare_external_dataset(
        files={"data.jsonl": json.dumps(row).encode()},
        dataset_revision="revision-shared", default_split=default_split,
    )


def test_preparation_options_survive_payload_roundtrip():
    from motte_sdk.benchmark_catalog import dataset_from_payload

    payload = dataset_to_payload(_dataset("dev"))
    assert payload.get("preparation") == {"default_split": "dev"}
    assert dataset_to_payload(dataset_from_payload(payload)) == payload


@pytest.fixture()
def pg_dataset_store():
    dsn = os.environ.get("MOTTE_R4_PG_DSN")
    if not dsn:
        pytest.skip("set MOTTE_R4_PG_DSN for isolated PostgreSQL concurrency coverage")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    from motte_storage.pg_audit_store import PgBenchmarkDatasets

    schema = "r4_datasets_" + uuid4().hex
    with psycopg.connect(dsn) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        connection.execute(sql.SQL(
            "CREATE TABLE {}.benchmark_datasets ("
            "position BIGINT GENERATED ALWAYS AS IDENTITY, benchmark_id TEXT NOT NULL, "
            "dataset_revision TEXT NOT NULL, payload JSONB NOT NULL, "
            "created_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (benchmark_id, dataset_revision))"
        ).format(sql.Identifier(schema)))
    isolated = make_conninfo(dsn, options=f"-c search_path={schema} -c statement_timeout=15000")
    try:
        yield PgBenchmarkDatasets(isolated)
    finally:
        with psycopg.connect(dsn) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def dataset_store(request, tmp_path):
    if request.param == "postgres":
        return request.getfixturevalue("pg_dataset_store")
    if request.param == "sqlite":
        return SQLiteBenchmarkDatasets(str(tmp_path / "datasets.db"))
    return MemoryBenchmarkDatasets()


def test_same_source_with_different_normalized_split_conflicts(dataset_store):
    first = dataset_to_payload(_dataset("val"))
    second = dataset_to_payload(_dataset("dev"))
    assert first["files"] == second["files"]
    assert dataset_store.put_immutable(first)["status"] == "created"
    with pytest.raises(RevisionConflictError):
        dataset_store.put_immutable(second)
    assert dataset_store.get("ceval", "revision-shared")["dataset_splits"] == ["val"]


@pytest.mark.parametrize("field,value", [
    ("rows", [{"case_id": "logic-1", "question": "changed normalization"}]),
    ("benchmark_version", "2"),
    ("preparation", {"default_split": "dev"}),
])
def test_prepared_semantics_are_part_of_revision_identity(dataset_store, field, value):
    payload = dataset_to_payload(_dataset())
    dataset_store.put_immutable(payload)
    with pytest.raises(RevisionConflictError):
        dataset_store.put_immutable({**payload, field: value})


def test_catalog_uses_canonical_record_on_identical_prepare(dataset_store):
    dataset = _dataset()
    # The order of file metadata is incidental, unlike normalized row order.
    dataset = replace(dataset, files=(dataset.files[0], replace(dataset.files[0], logical_name="z")))
    first = BenchmarkCatalog(dataset_store)
    first.register("ceval")
    first.update_dataset("ceval", dataset)
    first.update_dataset("ceval", replace(dataset, files=tuple(reversed(dataset.files))))
    restarted = BenchmarkCatalog(dataset_store)
    restarted.register("ceval")
    assert [item.logical_name for item in first.dataset("ceval").files] == ["data.jsonl", "z"]
    assert first.dataset("ceval") == restarted.dataset("ceval")


def test_catalog_detaches_mutable_input_from_canonical_record(dataset_store):
    dataset = _dataset()
    catalog = BenchmarkCatalog(dataset_store)
    catalog.register("ceval")
    catalog.update_dataset("ceval", dataset)
    dataset.rows[0]["question"] = "mutated after publication"
    assert catalog.dataset("ceval").rows[0]["question"] == "1+1=?"


def test_legacy_unknown_preparation_roundtrip_is_identical(dataset_store):
    payload = dataset_to_payload(_dataset())
    payload.pop("preparation")
    canonical = dataset_store.put_immutable(payload)["record"]
    catalog = BenchmarkCatalog(dataset_store)
    catalog.register("ceval")
    restored = catalog.dataset("ceval")
    assert restored.preparation == {}
    catalog.update_dataset("ceval", restored)
    assert dataset_store.get("ceval", "revision-shared") == canonical
    # Unknown preparation must not silently become the new known default.
    with pytest.raises(RevisionConflictError):
        dataset_store.put_immutable(dataset_to_payload(_dataset("val")))


def test_api_repeated_prepare_rejects_semantic_change_across_instances(tmp_path):
    from fastapi.testclient import TestClient
    from apps.api.app.main import create_app
    from motte_storage.resource_store import InMemoryResourceStore
    from motte_storage.run_store import SQLiteRunStore

    db = str(tmp_path / "api.db")
    store = SQLiteRunStore(db)
    body = {"dataset_revision": "revision-shared", "files": {"data.jsonl": json.dumps({
        "id": "logic-1", "subject": "logic", "question": "Q?", "A": "1", "B": "2",
        "C": "3", "D": "4", "answer": "B",
    })}}
    for index in range(2):
        app = create_app(store=SQLiteRunStore(db), resource_store=InMemoryResourceStore())
        with TestClient(app) as client:
            assert client.post("/api/v1/benchmarks/external/ceval/prepare", json={
                **body, "default_split": "val",
            }).status_code == 200
            conflict = client.post("/api/v1/benchmarks/external/ceval/prepare", json={
                **body, "default_split": "dev",
            })
            assert conflict.status_code == 422, (index, conflict.json())
            assert conflict.json()["error"]["code"] == "DATASET_REVISION_REUSED"
    assert store.benchmark_datasets.get("ceval", "revision-shared")["dataset_splits"] == ["val"]


@pytest.mark.parametrize("different", [False, True])
def test_postgres_first_concurrent_insert_is_created_or_identical_or_conflict(
    pg_dataset_store, monkeypatch, different,
):
    import motte_storage.pg_audit_store as pg

    original_connect = pg._connect
    start = Barrier(2)
    empty_reads = Barrier(2)

    @contextmanager
    def racing_connect(dsn):
        with original_connect(dsn) as connection:
            @contextmanager
            def cursor_context():
                with connection.cursor() as cursor:
                    first = True
                    query = ""

                    def execute(statement, parameters):
                        nonlocal first, query
                        query = statement
                        if first:
                            first = False
                            start.wait(timeout=10)
                        return cursor.execute(statement, parameters)

                    def fetchone():
                        row = cursor.fetchone()
                        # Force both old SELECT FOR UPDATE implementations to see
                        # absence before either can INSERT; real SQL/connections.
                        if row is None and "FOR UPDATE" in query:
                            empty_reads.wait(timeout=10)
                        return row

                    yield SimpleNamespace(execute=execute, fetchone=fetchone)
            yield SimpleNamespace(cursor=cursor_context)

    monkeypatch.setattr(pg, "_connect", racing_connect)
    payload = dataset_to_payload(_dataset())
    other = dataset_to_payload(_dataset("dev")) if different else deepcopy(payload)

    def submit(record):
        try:
            return pg_dataset_store.put_immutable(record)
        except RevisionConflictError:
            return {"status": "conflict"}

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit, record) for record in (payload, other)]
        results = [future.result(timeout=25) for future in futures]
    assert sorted(item["status"] for item in results) == (
        ["conflict", "created"] if different else ["created", "identical"]
    )
    if not different:
        assert results[0]["record"] == results[1]["record"]
