from motte_storage.repositories import SQLiteRepository


def test_sqlite_repository_survives_reopen(tmp_path):
    path = tmp_path / "runs.db"
    first = SQLiteRepository(path)
    first.put("run-1", {"id": "run-1", "status": "queued", "manifest": {"seed": 7}})
    second = SQLiteRepository(path)
    assert second.get("run-1")["manifest"]["seed"] == 7
