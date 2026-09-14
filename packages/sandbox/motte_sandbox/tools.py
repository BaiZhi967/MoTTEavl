class ToolRegistry:
    def __init__(self):
        self._tools = {}

    def register(self, name, handler, mode="real"):
        self._tools[name] = (handler, mode)

    def call(self, name, arg):
        if name not in self._tools:
            raise PermissionError(f"tool denied: {name}")
        handler, mode = self._tools[name]
        if mode == "deny":
            raise PermissionError(f"tool denied: {name}")
        return handler(arg)
