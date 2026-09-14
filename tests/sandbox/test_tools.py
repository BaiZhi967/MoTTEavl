import pytest
from motte_sandbox.tools import ToolRegistry


def test_tool_modes():
    r = ToolRegistry()
    r.register("x", lambda a: a, mode="mock")
    assert r.call("x", 2) == 2
    with pytest.raises(PermissionError):
        r.call("missing", None)
