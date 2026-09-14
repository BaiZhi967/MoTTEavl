from motte_storage.repositories import InMemoryRepository

def test_repository_put_get_and_list():
    repo = InMemoryRepository()
    repo.put("r1", {"id": "r1", "status": "queued"})
    assert repo.get("r1")["status"] == "queued"
    assert repo.list() == [{"id": "r1", "status": "queued"}]

def test_repository_get_returns_copy():
    repo = InMemoryRepository(); repo.put("x", {"id":"x", "nested":{"a":1}})
    value = repo.get("x"); value["nested"]["a"] = 9
    assert repo.get("x")["nested"]["a"] == 1
