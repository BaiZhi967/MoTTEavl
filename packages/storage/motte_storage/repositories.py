from copy import deepcopy
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
