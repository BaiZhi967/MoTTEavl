import shutil

import pytest

from motte_agent.pi import PiAgentRuntime
from motte_agent.protocol import decode_message, encode_message


def test_protocol_roundtrip():
    assert decode_message(encode_message({"type": "init"}))["type"] == "init"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for the pi bridge")
def test_pi_bridge_probe_and_prompt_roundtrip():
    runtime = PiAgentRuntime()
    probe = runtime.probe()
    assert probe["available"] is True
    assert probe["protocol"] == "v1"

    result = runtime.run("hello")
    assert result["status"] == "completed"
    assert result["answer"] == "answer: HELLO"
    types = [event["type"] for event in result["events"]]
    assert types[0] == "version"
    assert "started" in types and "finished" in types


def test_missing_bridge_reports_unavailable(tmp_path):
    runtime = PiAgentRuntime(bridge_path=tmp_path / "missing.mjs")
    assert runtime.available() is False
    with pytest.raises(Exception, match="unavailable"):
        runtime.run("x")
