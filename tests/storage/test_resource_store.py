from concurrent.futures import ThreadPoolExecutor

import pytest

from motte_storage.resource_store import (
    InMemoryResourceStore,
    ResourceConflictError,
    SQLiteResourceStore,
)


def _exercise(store):
    store.models.put({"id": "m-1", "provider": "p", "capabilities": {"text": True}})
    assert store.models.get("m-1")["provider"] == "p"
    assert len(store.models.list()) == 1

    # Provider and model profiles are mutable; versioned resources are not.
    store.models.put({"id": "m-1", "provider": "p2", "capabilities": {}})
    assert store.models.get("m-1")["provider"] == "p2"
    assert len(store.models.list()) == 1

    store.scenarios.put({"name": "json_extract", "version": "3", "cases": []})
    assert store.scenarios.get("json_extract", "3")["version"] == "3"
    assert store.scenarios.get("json_extract", "4") is None

    with pytest.raises(ResourceConflictError, match="immutable"):
        store.scenarios.delete("json_extract", "3")
    assert store.scenarios.delete("json_extract", "4") is False
    assert len(store.scenarios.list()) == 1


def test_in_memory_resource_store_semantics():
    _exercise(InMemoryResourceStore())


def test_sqlite_resource_store_survives_reopen(tmp_path):
    path = tmp_path / "resources.db"
    first = SQLiteResourceStore(path)
    _exercise(first)
    reopened = SQLiteResourceStore(path)
    assert reopened.models.get("m-1")["provider"] == "p2"
    assert reopened.scenarios.list() == [{"name": "json_extract", "version": "3", "cases": []}]


def test_resource_stores_are_independent(tmp_path):
    store = SQLiteResourceStore(tmp_path / "resources.db")
    store.providers.put({"name": "local-vllm", "kind": "openai_compatible", "base_url": "http://x"})
    store.price_tables.put({"model_id": "m-1", "version": "v1", "input_per_million": 1})
    assert store.providers.list()[0]["name"] == "local-vllm"
    assert store.price_tables.get("m-1", "v1")["input_per_million"] == 1
    assert store.models.list() == []


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("repo,record,key,field", [
    ("datasets", {"name": "custom", "version": "v1", "cases": []}, ("custom", "v1"), "cases"),
    ("scenarios", {"name": "custom", "version": "v1", "cases": []}, ("custom", "v1"), "cases"),
    ("price_tables", {"model_id": "m1", "version": "v1", "input_per_million": 1},
     ("m1", "v1"), "input_per_million"),
])
def test_unmanaged_versioned_records_are_insert_only(tmp_path, backend, repo, record, key, field):
    store = InMemoryResourceStore() if backend == "memory" else SQLiteResourceStore(
        tmp_path / "immutable.db"
    )
    repository = getattr(store, repo)
    assert repository.put(record) == record
    assert repository.put(record) == record
    changed = {**record, field: ["other"] if field == "cases" else 2}
    with pytest.raises(ResourceConflictError, match="different content"):
        repository.put(changed)
    assert repository.get(*key) == record
    with pytest.raises(ResourceConflictError, match="immutable"):
        repository.delete(*key)
    assert repository.delete(key[0], "missing") is False
    second = {**record, "version": "v2", field: changed[field]}
    assert repository.put(second) == second
    assert len(repository.list()) == 2


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_version_collision_is_atomic_under_concurrency(tmp_path, backend):
    store = InMemoryResourceStore() if backend == "memory" else SQLiteResourceStore(
        tmp_path / "concurrent.db"
    )

    def write(value):
        try:
            return store.price_tables.put({"model_id": "model", "version": "1", "price": value})
        except ResourceConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(write, (1, 2)))
    assert sum(item is None for item in outcomes) == 1
    assert [row for row in outcomes if row is not None] == store.price_tables.list()
