import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from motte_sdk.publication import publication_audit
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


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_dataset_and_scenario_publish_atomically(tmp_path, backend):
    store = InMemoryResourceStore() if backend == "memory" else SQLiteResourceStore(
        tmp_path / "atomic-resources.db"
    )
    existing_scenario = {"name": "bundle", "version": "1", "cases": ["old"]}
    store.scenarios.put(existing_scenario)
    dataset = {"name": "bundle", "version": "1", "cases": ["new"]}
    scenario = {"name": "bundle", "version": "1", "cases": ["new"]}

    with pytest.raises(ResourceConflictError, match="different content"):
        store.publish_dataset_scenario(dataset, scenario)
    assert store.datasets.get("bundle", "1") is None
    assert store.scenarios.get("bundle", "1") == existing_scenario

    other_dataset = {"name": "other", "version": "1", "cases": ["new"]}
    other_scenario = {"name": "other", "version": "1", "cases": ["new"]}
    assert store.publish_dataset_scenario(other_dataset, other_scenario) == (
        other_dataset, other_scenario
    )
    assert store.publish_dataset_scenario(other_dataset, other_scenario) == (
        other_dataset, other_scenario
    )


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_pair_publish_is_concurrent_and_never_mixes_records(tmp_path, backend):
    store = InMemoryResourceStore() if backend == "memory" else SQLiteResourceStore(
        tmp_path / "concurrent-pairs.db"
    )
    same_pair = (
        {"name": "same-pair", "version": "1", "value": 1},
        {"name": "same-pair", "version": "1", "value": 1},
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(store.publish_dataset_scenario, *same_pair) for _ in range(2)]
        assert [future.result(timeout=10) for future in futures] == [same_pair, same_pair]

    pairs = [
        (
            {"name": "racing-pair", "version": "1", "value": value},
            {"name": "racing-pair", "version": "1", "value": value},
        )
        for value in (1, 2)
    ]

    def publish(pair):
        try:
            return store.publish_dataset_scenario(*pair)
        except ResourceConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish, pair) for pair in pairs]
        outcomes = [future.result(timeout=10) for future in futures]
    assert sum(outcome is None for outcome in outcomes) == 1
    winner = next(outcome for outcome in outcomes if outcome is not None)
    assert store.datasets.get("racing-pair", "1") == winner[0]
    assert store.scenarios.get("racing-pair", "1") == winner[1]


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_pair_publish_commits_append_only_publication_audit(tmp_path, backend):
    store = InMemoryResourceStore() if backend == "memory" else SQLiteResourceStore(
        tmp_path / "publication-audit.db"
    )
    fingerprint = "sha256:" + "1" * 64
    dataset = {
        "name": "audited-pair", "version": "1", "value": 1,
        "dataset_fingerprint": fingerprint,
    }
    scenario = {
        "name": "audited-pair", "version": "1", "value": 1,
        "dataset": "audited-pair@1",
    }
    publication = publication_audit(
        dataset, scenario, {"fixture": "append-only"},
        actor="test-operator", entrypoint="cli", published_at="2026-09-19T00:00:00Z",
    )

    assert store.publish_dataset_scenario(
        dataset, scenario, publication=publication
    ) == (dataset, scenario)
    assert store.publications.get(publication["id"]) == publication
    assert store.publish_dataset_scenario(
        dataset, scenario, publication=publication
    ) == (dataset, scenario)
    with pytest.raises(ResourceConflictError, match="different content"):
        store.publish_dataset_scenario(
            dataset, scenario,
            publication={**publication, "published_at": "2026-09-20T00:00:00Z"},
        )
    assert store.publications.get(publication["id"]) == publication


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_publication_repository_rejects_noncanonical_receipt_time_and_fields(tmp_path, backend):
    store = InMemoryResourceStore() if backend == "memory" else SQLiteResourceStore(
        tmp_path / "strict-publication.db"
    )
    dataset = {
        "name": "strict-audit", "version": "1",
        "dataset_fingerprint": "sha256:" + "1" * 64,
    }
    scenario = {"name": "strict-audit", "version": "1"}
    audit = publication_audit(
        dataset, scenario, {"fixture": "strict"},
        actor="pytest", entrypoint="storage-test", published_at="2026-09-19T00:00:00Z",
    )
    invalid_records = [
        {**audit, "unexpected": True},
        {**audit, "published_at": "2026-09-19T00:00:00+00:00"},
        {**audit, "receipt": {"nested": {"artifact_paths": {"data": "/tmp/private"}}}},
        {**audit, "receipt_sha256": "f" * 64},
        {**audit, "id": "publication-" + "0" * 64},
    ]
    for invalid in invalid_records:
        with pytest.raises(ValueError):
            store.publications.put(invalid)
    assert store.publications.list() == []


def test_publication_audit_must_match_published_bundle():
    store = InMemoryResourceStore()
    fingerprint = "sha256:" + "1" * 64
    dataset = {
        "name": "audited-pair", "version": "1",
        "dataset_fingerprint": fingerprint,
    }
    scenario = {
        "name": "audited-pair", "version": "1", "dataset": "audited-pair@1",
    }
    publication = publication_audit(
        {**dataset, "name": "other"}, scenario, {"fixture": "wrong-reference"},
        actor="test-operator", entrypoint="cli", published_at="2026-09-19T00:00:00Z",
    )
    with pytest.raises(ValueError, match="references"):
        store.publish_dataset_scenario(dataset, scenario, publication=publication)
    assert store.datasets.get("audited-pair", "1") is None
    assert store.scenarios.get("audited-pair", "1") is None
    assert store.publications.list() == []


def test_sqlite_pair_publish_rolls_back_when_scenario_insert_fails(tmp_path):
    path = tmp_path / "rollback-resources.db"
    store = SQLiteResourceStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_scenario_insert
            BEFORE INSERT ON scenario_versions
            WHEN NEW.name = 'rollback-pair'
            BEGIN
                SELECT RAISE(ABORT, 'forced scenario failure');
            END
            """
        )
    dataset = {"name": "rollback-pair", "version": "1", "cases": ["new"]}
    scenario = {"name": "rollback-pair", "version": "1", "cases": ["new"]}

    with pytest.raises(sqlite3.IntegrityError, match="forced scenario failure"):
        store.publish_dataset_scenario(dataset, scenario)
    assert store.datasets.get("rollback-pair", "1") is None
    assert store.scenarios.get("rollback-pair", "1") is None


def test_sqlite_bundle_rolls_back_when_publication_insert_fails(tmp_path):
    path = tmp_path / "rollback-publication.db"
    store = SQLiteResourceStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_publication_insert
            BEFORE INSERT ON resource_publications
            BEGIN
                SELECT RAISE(ABORT, 'forced publication failure');
            END
            """
        )
    fingerprint = "sha256:" + "1" * 64
    dataset = {
        "name": "rollback-audit", "version": "1",
        "dataset_fingerprint": fingerprint,
    }
    scenario = {
        "name": "rollback-audit", "version": "1", "dataset": "rollback-audit@1",
    }
    publication = publication_audit(
        dataset, scenario, {"fixture": "rollback"},
        actor="test-operator", entrypoint="cli", published_at="2026-09-19T00:00:00Z",
    )
    with pytest.raises(sqlite3.IntegrityError, match="forced publication failure"):
        store.publish_dataset_scenario(dataset, scenario, publication=publication)
    assert store.datasets.get("rollback-audit", "1") is None
    assert store.scenarios.get("rollback-audit", "1") is None
    assert store.publications.get("rollback-audit-1") is None


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_mutable_create_only_put_and_delete_use_generation_cas(tmp_path, backend):
    store = InMemoryResourceStore() if backend == "memory" else SQLiteResourceStore(
        tmp_path / "mutable-cas.db"
    )
    first = {"name": "provider", "kind": "openai_compatible", "base_url": "https://one.test"}

    def create(value):
        try:
            return store.providers.put(
                {**first, "base_url": value}, expected_generation=0
            )
        except ResourceConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(create, ("https://one.test", "https://two.test")))
    assert sum(item is None for item in outcomes) == 1
    current = store.providers.get("provider")
    assert current in [item for item in outcomes if item is not None]

    updated = {**current, "base_url": "https://three.test", "generation": 2}
    store.providers.put(updated, expected_generation=1)
    with pytest.raises(ResourceConflictError, match="generation changed"):
        store.providers.delete("provider", expected_generation=1)
    assert store.providers.get("provider")["base_url"] == "https://three.test"


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_resource_store_rejects_client_tombstone_sentinel(tmp_path, backend):
    store = InMemoryResourceStore() if backend == "memory" else SQLiteResourceStore(
        tmp_path / "reserved-tombstone.db"
    )
    with pytest.raises(ValueError, match="reserved"):
        store.scenarios.put({
            "name": "hidden", "version": "1", "cases": [], "_deleted": True,
        })
    assert store.scenarios.get("hidden", "1") is None


def test_mutable_generation_rejects_reuse_and_preserves_delete_recreate_incarnation():
    store = InMemoryResourceStore()
    first = store.providers.put(
        {"name": "provider", "kind": "openai_compatible", "base_url": "https://one.test"},
        expected_generation=0,
    )
    with pytest.raises(ResourceConflictError, match="monotonically"):
        store.providers.put({**first, "base_url": "https://two.test", "generation": 99}, expected_generation=1)
    assert store.providers.delete("provider", expected_generation=1) is True
    recreated = store.providers.put(
        {"name": "provider", "kind": "openai_compatible", "base_url": "https://three.test"},
        expected_generation=0,
    )
    assert recreated["generation"] == 2
    with pytest.raises(ResourceConflictError, match="generation changed"):
        store.providers.put(
            {**first, "base_url": "https://stale.test", "generation": 2},
            expected_generation=1,
        )
