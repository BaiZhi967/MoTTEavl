import json
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from motte_agent.pi import PiAgentRuntime, PiBridgeError, PiBridgeSession

from bridge_process import (
    assert_grandchild_stopped,
    fake_runtime,
    fake_session_bridge,
    hung_tree_runtime,
    json_line,
    run_bridge_with_lines,
    session_for,
)


BRIDGE = Path(__file__).resolve().parents[2] / "bridges" / "pi" / "bridge.mjs"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is required for the Pi bridge")
def test_packaged_bridge_reports_protocol_only_capability():
    runtime = PiAgentRuntime()

    probe = runtime.probe()

    assert probe["protocol"] == "v2"
    # 真实 SDK 已安装：wiring 就绪；sdk_version 必须来自包元数据而非猜测。
    assert probe["execution_ready"] is True
    assert probe["sdk_version"] == "0.73.1"
    # 裸 prompt 交换已被移除：PiAgentRuntime.run 不再执行，也没有 echo 路径。
    with pytest.raises(PiBridgeError) as raised:
        runtime.run("must not be echoed")
    assert raised.value.code == "PI_SESSION_REQUIRED"
    # 原始 bridge 对裸 prompt 消息给出结构化错误，绝不回显输入。
    completed = run_bridge_with_lines(
        NODE, [json_line({"type": "prompt", "id": "p1", "text": "must not be echoed"})],
    )
    events = [json.loads(line) for line in completed.stdout.splitlines()]
    assert events[-1]["type"] == "error"
    assert events[-1]["error"]["code"] == "UNSUPPORTED_MESSAGE"
    assert "must not be echoed" not in completed.stdout


@pytest.mark.skipif(NODE is None, reason="node is required for the Pi bridge")
def test_packaged_bridge_sanitizes_invalid_input():
    completed = run_bridge_with_lines(
        NODE,
        ["not-json SECRET", json_line({"type": "unknown SECRET"})],
    )

    events = [json.loads(line) for line in completed.stdout.splitlines()]
    assert events == [
        {
            "type": "error",
            "id": None,
            "seq": 1,
            "error": {
                "code": "INVALID_REQUEST",
                "message": "invalid protocol message",
            },
        },
        {
            "type": "error",
            "id": None,
            "seq": 2,
            "error": {
                "code": "UNSUPPORTED_MESSAGE",
                "message": "unsupported protocol message",
            },
        },
    ]
    assert "SECRET" not in completed.stdout
    assert completed.stderr == ""


def test_successful_response_is_strictly_collected(tmp_path):
    bridge = fake_session_bridge(
        tmp_path,
        """
        import json
        import sys

        identity = {"run_id": "run-1", "case_id": "case-1",
                    "session_id": "session-1", "operation_id": "operation-1"}
        seq = 0
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            message = json.loads(line)
            if message["type"] == "probe":
                seq += 1
                print(json.dumps({"type": "version", "version": "test-1.0",
                                  "protocol": "v2", "execution_ready": True,
                                  "sdk_version": "0.73.1", "seq": seq}), flush=True)
            elif message["type"] == "init":
                seq += 1
                print(json.dumps({"type": "ready", "seq": seq, **identity,
                                  "sdk_version": "0.73.1"}), flush=True)
            elif message["type"] == "run":
                seq += 1
                print(json.dumps({"type": "session_event", "seq": seq, **identity,
                                  "agent_event": {"type": "agent_start"}}), flush=True)
                seq += 1
                print(json.dumps({"type": "output", "seq": seq, **identity,
                                  "text": "answer: HELLO"}), flush=True)
                seq += 1
                print(json.dumps({"type": "finished", "seq": seq, **identity,
                                  "id": message["id"], "status": "completed",
                                  "result": {"final_output": "answer: HELLO",
                                             "steps": 1, "tool_calls": 0,
                                             "usage": {"reported": False}}}), flush=True)
        """,
    )
    session = session_for(bridge, tmp_path=tmp_path)
    session.start()

    result = session.run("hello")

    assert result["status"] == "completed"
    assert result["final_output"] == "answer: HELLO"
    assert result["outputs"] == ["answer: HELLO"]
    session.close()


def test_v2_peer_without_optional_fields_remains_compatible(tmp_path):
    bridge = fake_session_bridge(
        tmp_path,
        """
        import json
        import sys

        sys.stdout.reconfigure(encoding="utf-8")
        identity = {"run_id": "run-1", "case_id": "case-1",
                    "session_id": "session-1", "operation_id": "operation-1"}
        seq = 0
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            message = json.loads(line)
            if message["type"] == "probe":
                seq += 1
                print(json.dumps({"type": "version", "version": "legacy-1.0",
                                  "protocol": "v2", "seq": seq}), flush=True)
            elif message["type"] == "init":
                seq += 1
                print(json.dumps({"type": "ready", "seq": seq, **identity,
                                  "sdk_version": None}), flush=True)
            elif message["type"] == "run":
                seq += 1
                text = "left" + chr(0x2028) + "right"
                print(json.dumps({"type": "output", "seq": seq, **identity,
                                  "text": text}, ensure_ascii=False), flush=True)
                seq += 1
                print(json.dumps({"type": "finished", "seq": seq, **identity,
                                  "id": message["id"], "status": "completed",
                                  "result": {"final_output": text, "steps": 1,
                                             "tool_calls": 0, "usage": {}}}), flush=True)
        """,
    )
    session = session_for(bridge, tmp_path=tmp_path)
    session.start()
    result = session.run("hello")
    assert result["final_output"] == "left\u2028right"
    session.close()


def test_output_limit_stops_noisy_bridge(tmp_path):
    bridge = fake_session_bridge(
        tmp_path,
        """
        import sys

        sys.stdin.read()
        sys.stdout.write("x" * 1100000)
        sys.stdout.flush()
        import time
        time.sleep(30)
        """,
    )
    session = session_for(bridge, tmp_path=tmp_path, idle=5.0, total=10.0)
    with pytest.raises(PiBridgeError) as raised:
        session.start()
    assert raised.value.code in ("PI_BRIDGE_OUTPUT_LIMIT", "PI_BRIDGE_TIMEOUT")


def test_huge_single_line_fails_closed(tmp_path):
    bridge = fake_session_bridge(
        tmp_path,
        """
        import sys

        sys.stdin.read()
        sys.stdout.write("x" * 5_000_000 + "\\n")
        sys.stdout.flush()
        import time
        time.sleep(30)
        """,
    )
    session = session_for(bridge, tmp_path=tmp_path, idle=5.0, total=10.0)
    with pytest.raises(PiBridgeError) as raised:
        session.start()
    assert raised.value.code in ("PI_BRIDGE_OUTPUT_LIMIT", "PI_BRIDGE_TIMEOUT")


@pytest.mark.parametrize(
    "protocol_value",
    ["v3", None],
)
def test_mismatched_protocol_fails_closed(tmp_path, protocol_value):
    bridge = fake_session_bridge(
        tmp_path,
        """
        import json
        import sys

        while True:
            line = sys.stdin.readline()
            if not line:
                break
            payload = {"type": "version", "version": "test-1.0", "seq": 1}
            if PROTOCOL_VALUE is not None:
                payload["protocol"] = PROTOCOL_VALUE
            print(json.dumps(payload), flush=True)
        """.replace("PROTOCOL_VALUE", repr(protocol_value)),
    )
    session = session_for(bridge, tmp_path=tmp_path)
    with pytest.raises(PiBridgeError) as raised:
        session.start()
    assert raised.value.code == "PI_PROTOCOL_INVALID"


def test_identity_mismatch_and_seq_regression_fail_closed(tmp_path):
    bridge = fake_session_bridge(
        tmp_path,
        """
        import json
        import sys

        identity = {"run_id": "run-1", "case_id": "case-1",
                    "session_id": "session-1", "operation_id": "operation-1"}
        seq = 0
        mode = "identity"
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            message = json.loads(line)
            if message["type"] == "probe":
                seq += 1
                print(json.dumps({"type": "version", "version": "t", "protocol": "v2",
                                  "execution_ready": True, "seq": seq}), flush=True)
            elif message["type"] == "init":
                seq += 1
                print(json.dumps({"type": "ready", "seq": seq, **identity,
                                  "sdk_version": None}), flush=True)
            elif message["type"] == "run":
                # seq 回退：不递增（复用 ready 的 seq）→ 客户端必须拒绝
                if mode != "seq":
                    seq += 1
                wrong = dict(identity)
                if mode == "identity":
                    wrong["session_id"] = "other-session"
                print(json.dumps({"type": "output", "seq": seq, **wrong,
                                  "text": "leak?"}), flush=True)
                import time
                time.sleep(5)
        """,
    )
    for mode in ("identity", "seq"):
        target = bridge.with_name(f"bridge-{mode}.py")
        target.write_text(
            bridge.read_text(encoding="utf-8").replace('mode = "identity"', f'mode = "{mode}"'),
            encoding="utf-8",
        )
        session = session_for(target, tmp_path=tmp_path)
        session.start()
        with pytest.raises(PiBridgeError) as raised:
            session.run("hello")
        assert raised.value.code == "PI_PROTOCOL_INVALID"
        session.close()


def test_malformed_or_noisy_output_is_sanitized(tmp_path):
    bridge = fake_session_bridge(
        tmp_path,
        """
        import sys

        print("diagnostic noise containing TOP_SECRET", flush=True)
        print("stderr TOP_SECRET", file=sys.stderr, flush=True)
        import time
        time.sleep(10)
        """,
    )
    session = session_for(bridge, tmp_path=tmp_path)
    with pytest.raises(PiBridgeError) as raised:
        session.start()

    assert raised.value.code == "PI_PROTOCOL_INVALID"
    assert "TOP_SECRET" not in str(raised.value)
    assert "diagnostic noise" not in str(raised.value)


def test_invalid_result_shape_fails_closed(tmp_path):
    bridge = fake_session_bridge(
        tmp_path,
        """
        import json
        import sys

        identity = {"run_id": "run-1", "case_id": "case-1",
                    "session_id": "session-1", "operation_id": "operation-1"}
        seq = 0
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            message = json.loads(line)
            if message["type"] == "probe":
                seq += 1
                print(json.dumps({"type": "version", "version": "t", "protocol": "v2",
                                  "execution_ready": True, "seq": seq}), flush=True)
            elif message["type"] == "init":
                seq += 1
                print(json.dumps({"type": "ready", "seq": seq, **identity,
                                  "sdk_version": None}), flush=True)
            elif message["type"] == "run":
                seq += 1
                print(json.dumps({"type": "finished", "seq": seq, **identity,
                                  "id": message["id"], "status": "completed",
                                  "result": None}), flush=True)
        """,
    )
    session = session_for(bridge, tmp_path=tmp_path)
    session.start()
    with pytest.raises(PiBridgeError) as raised:
        session.run("hello")
    assert raised.value.code == "PI_PROTOCOL_INVALID"
    session.close()


def test_extra_event_fields_fail_closed(tmp_path):
    bridge = fake_session_bridge(
        tmp_path,
        """
        import json
        import sys

        identity = {"run_id": "run-1", "case_id": "case-1",
                    "session_id": "session-1", "operation_id": "operation-1"}
        seq = 0
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            message = json.loads(line)
            if message["type"] == "probe":
                seq += 1
                print(json.dumps({"type": "version", "version": "t", "protocol": "v2",
                                  "execution_ready": True, "seq": seq}), flush=True)
            elif message["type"] == "init":
                seq += 1
                print(json.dumps({"type": "ready", "seq": seq, **identity,
                                  "sdk_version": None}), flush=True)
            elif message["type"] == "run":
                seq += 1
                print(json.dumps({"type": "session_event", "seq": seq, **identity,
                                  "agent_event": {"type": "agent_start"},
                                  "debug": "TOP_SECRET"}), flush=True)
                import time
                time.sleep(5)
        """,
    )
    session = session_for(bridge, tmp_path=tmp_path)
    session.start()
    with pytest.raises(PiBridgeError) as raised:
        session.run("hello")
    assert raised.value.code == "PI_PROTOCOL_INVALID"
    assert "TOP_SECRET" not in str(raised.value)
    session.close()


def test_structured_bridge_error_preserves_code_and_sanitizes_message(tmp_path):
    bridge = fake_session_bridge(
        tmp_path,
        """
        import json
        import sys

        identity = {"run_id": "run-1", "case_id": "case-1",
                    "session_id": "session-1", "operation_id": "operation-1"}
        seq = 0
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            message = json.loads(line)
            if message["type"] == "probe":
                seq += 1
                print(json.dumps({"type": "version", "version": "t", "protocol": "v2",
                                  "execution_ready": True, "seq": seq}), flush=True)
            elif message["type"] == "init":
                seq += 1
                print(json.dumps({"type": "ready", "seq": seq, **identity,
                                  "sdk_version": None}), flush=True)
            elif message["type"] == "run":
                seq += 1
                print(json.dumps({"type": "error", "id": message["id"], "seq": seq,
                                  "error": {"code": "PI_BACKEND_UNAVAILABLE",
                                            "message": "backend unavailable"}}), flush=True)
        """,
    )
    session = session_for(bridge, tmp_path=tmp_path)
    session.start()
    with pytest.raises(PiBridgeError) as raised:
        session.run("hello")

    assert raised.value.code == "PI_BACKEND_UNAVAILABLE"
    assert str(raised.value) == "pi bridge reported an execution error"
    assert "backend unavailable" not in str(raised.value)
    session.close()


def test_timeout_terminates_the_full_process_tree(tmp_path):
    runtime, marker = hung_tree_runtime(tmp_path, timeout=1.0)

    started = time.monotonic()
    with pytest.raises(PiBridgeError) as raised:
        runtime.probe()
    elapsed = time.monotonic() - started

    assert raised.value.code == "PI_BRIDGE_TIMEOUT"
    assert elapsed < 10
    assert_grandchild_stopped(marker)


def test_cancellation_terminates_the_full_process_tree(tmp_path, monkeypatch):
    runtime, marker = hung_tree_runtime(tmp_path, timeout=10.0)
    original_wait = subprocess.Popen.wait
    bridge_path = str(runtime.bridge_path)
    interrupted = False

    def interrupt_bridge(process, *args, **kwargs):
        nonlocal interrupted
        command = [str(part) for part in process.args]
        if bridge_path in command and not interrupted:
            interrupted = True
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            raise KeyboardInterrupt
        return original_wait(process, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "wait", interrupt_bridge)

    with pytest.raises(KeyboardInterrupt):
        runtime.probe()

    assert_grandchild_stopped(marker)
