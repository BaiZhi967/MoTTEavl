import pytest
from motte_sandbox.tools import ToolRegistry


def test_tool_modes_fail_closed_unless_explicitly_executable():
    called = []
    r = ToolRegistry()
    def handler(a):
        called.append(a)
        return a
    for mode in ("mock", "replay", "deny", "typo"):
        r.register(mode, handler, mode=mode)
        with pytest.raises(PermissionError):
            r.call(mode, 2)
    assert called == []
    r.register("execute", handler, mode="execute")
    r.register("native", handler, mode="native")
    assert r.call("execute", 3) == 3
    assert r.call("native", 4) == 4
    with pytest.raises(PermissionError):
        r.call("missing", None)
