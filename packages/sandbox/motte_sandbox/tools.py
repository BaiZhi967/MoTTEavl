"""Explicit tool mode registry.

Only execute and native are executable. Mock/replay/deny and unknown modes
fail closed at call time, so a typo cannot silently run a real handler.
"""


class ToolRegistry:
    _EXECUTABLE_MODES = frozenset({"execute", "native"})

    def __init__(self):
        self._tools = {}

    def register(self, name, handler, mode="deny"):
        if not isinstance(name, str) or not name:
            raise ValueError("tool name must be non-empty")
        if not callable(handler):
            raise TypeError("tool handler must be callable")
        self._tools[name] = (handler, mode)

    def call(self, name, arg):
        if name not in self._tools:
            raise PermissionError(f"tool denied: {name}")
        handler, mode = self._tools[name]
        if mode not in self._EXECUTABLE_MODES:
            raise PermissionError(f"tool mode is not executable: {name}: {mode!r}")
        return handler(arg)
