from motte_storage.resource_store import InMemoryResourceStore, SQLiteResourceStore


def _exercise(store):
    store.models.put({"id": "m-1", "provider": "p", "capabilities": {"text": True}})
    assert store.models.get("m-1")["provider"] == "p"
    assert len(store.models.list()) == 1

    # 同键 put 是幂等更新
    store.models.put({"id": "m-1", "provider": "p2", "capabilities": {}})
    assert store.models.get("m-1")["provider"] == "p2"
    assert len(store.models.list()) == 1

    store.scenarios.put({"name": "json_extract", "version": "3", "cases": []})
    assert store.scenarios.get("json_extract", "3")["version"] == "3"
    assert store.scenarios.get("json_extract", "4") is None

    assert store.scenarios.delete("json_extract", "3") is True
    assert store.scenarios.delete("json_extract", "3") is False
    assert store.scenarios.list() == []


def test_in_memory_resource_store_semantics():
    _exercise(InMemoryResourceStore())


def test_sqlite_resource_store_survives_reopen(tmp_path):
    path = tmp_path / "resources.db"
    first = SQLiteResourceStore(path)
    _exercise(first)
    reopened = SQLiteResourceStore(path)
    assert reopened.models.get("m-1")["provider"] == "p2"
    assert reopened.scenarios.list() == []


def test_resource_stores_are_independent(tmp_path):
    store = SQLiteResourceStore(tmp_path / "resources.db")
    store.providers.put({"name": "local-vllm", "kind": "openai_compatible", "base_url": "http://x"})
    store.price_tables.put({"model_id": "m-1", "version": "v1", "input_per_million": 1})
    assert store.providers.list()[0]["name"] == "local-vllm"
    assert store.price_tables.get("m-1", "v1")["input_per_million"] == 1
    assert store.models.list() == []
