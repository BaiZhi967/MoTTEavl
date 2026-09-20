import shutil

import pytest

from motte_agent.pi import PiAgentRuntime, PiBridgeError
from motte_agent.protocol import decode_message, encode_message


def test_protocol_roundtrip():
    assert decode_message(encode_message({"type": "init"}))["type"] == "init"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for the pi bridge")
def test_default_pi_bridge_probe_reports_real_sdk_readiness():
    # M4-T03：真实 SDK 已随 bridges/pi 安装——probe 如实上报就绪与版本，
    # 执行仍必须走 PiBridgeSession（无 echo 路径）。
    runtime = PiAgentRuntime()
    probe = runtime.probe()
    assert probe["transport_available"] is True
    assert probe["protocol"] == "v2"
    assert probe["execution_ready"] is True
    assert probe["sdk_version"] == "0.73.1"

    with pytest.raises(PiBridgeError) as caught:
        runtime.run("hello")
    assert caught.value.code == "PI_SESSION_REQUIRED"


def test_missing_bridge_reports_unavailable(tmp_path):
    runtime = PiAgentRuntime(bridge_path=tmp_path / "missing.mjs")
    assert runtime.available() is False
    with pytest.raises(PiBridgeError, match="unavailable"):
        runtime.probe()
