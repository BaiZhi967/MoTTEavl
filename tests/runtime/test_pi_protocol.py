import shutil

import pytest

from motte_agent.pi import PiAgentRuntime, PiBridgeError
from motte_agent.protocol import decode_message, encode_message


def test_protocol_roundtrip():
    assert decode_message(encode_message({"type": "init"}))["type"] == "init"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for the pi bridge")
def test_default_pi_bridge_is_probeable_but_not_execution_ready():
    runtime = PiAgentRuntime()
    probe = runtime.probe()
    assert probe["transport_available"] is True
    assert probe["available"] is False
    assert probe["execution_ready"] is False
    assert probe["protocol"] == "v1"

    with pytest.raises(PiBridgeError) as caught:
        runtime.run("hello")
    assert caught.value.code == "PI_BACKEND_UNAVAILABLE"


def test_missing_bridge_reports_unavailable(tmp_path):
    runtime = PiAgentRuntime(bridge_path=tmp_path / "missing.mjs")
    assert runtime.available() is False
    with pytest.raises(Exception, match="unavailable"):
        runtime.run("x")
