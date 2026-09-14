from copy import deepcopy
import json
import sqlite3
from pathlib import Path
from typing import Any


class InMemoryRepository:
    def __init__(self):
        self._items: dict[str, Any] = {}

    def put(self, key: str, value: Any) -> Any:
        self._items[key] = deepcopy(value)
        return deepcopy(value)

    def get(self, key: str) -> Any | None:
        value = self._items.get(key)
        return deepcopy(value)

    def list(self) -> list[Any]:
        return [deepcopy(v) for v in self._items.values()]

    def delete(self, key: str) -> None:
        self._items.pop(key, None)


class SQLiteRepository:
    """Small durable repository used for local development and replay runs."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with sqlite3.connect(self.path) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def put(self, key: str, value: Any) -> Any:
        payload = json.dumps(value, sort_keys=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute("INSERT OR REPLACE INTO records(key, value) VALUES (?, ?)", (key, payload))
        return deepcopy(value)

    def get(self, key: str) -> Any | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute("SELECT value FROM records WHERE key = ?", (key,)).fetchone()
        return deepcopy(json.loads(row[0])) if row else None

    def list(self) -> list[Any]:
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute("SELECT value FROM records ORDER BY key").fetchall()
        return [json.loads(row[0]) for row in rows]

    def delete(self, key: str) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute("DELETE FROM records WHERE key = ?", (key,))
