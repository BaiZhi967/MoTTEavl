import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from motte_agent.pi import PiAgentRuntime, PiBridgeError


BRIDGE = Path(__file__).resolve().parents[2] / "bridges" / "pi" / "bridge.mjs"


def _fake_runtime(tmp_path, source: str, *, timeout: float = 2.0) -> PiAgentRuntime:
    bridge = tmp_path / "fake_pi_bridge.py"
    bridge.write_text(textwrap.dedent(source), encoding="utf-8")
    return PiAgentRuntime(
        bridge_path=bridge,
        node_binary=sys.executable,
        timeout_seconds=timeout,
    )


def _port_is_open(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.1)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _stop_fixture_process(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass


def _hung_tree_runtime(tmp_path, *, timeout: float) -> tuple[PiAgentRuntime, Path]:
    marker = tmp_path / "grandchild-ready"
    grandchild = (
        "import os,pathlib,socket,time;"
        "server=socket.socket();"
        "server.bind(('127.0.0.1',0));"
        "server.listen();"
        f"pathlib.Path({str(marker)!r}).write_text("
        "f'{server.getsockname()[1]},{os.getpid()}',encoding='utf-8');"
        "time.sleep(60)"
    )
    runtime = _fake_runtime(
        tmp_path,
        f"""
        import subprocess
        import sys
        import time

        subprocess.Popen([sys.executable, "-c", {grandchild!r}])
        time.sleep(60)
        """,
        timeout=timeout,
    )
    return runtime, marker


def _assert_grandchild_stopped(marker: Path) -> None:
    assert marker.exists(), "grandchild did not start before bridge cleanup"
    port_text, pid_text = marker.read_text(encoding="utf-8").split(",")
    port = int(port_text)
    pid = int(pid_text)
    try:
        deadline = time.monotonic() + 5
        while _port_is_open(port) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _port_is_open(port), "bridge grandchild survived process-tree cleanup"
    finally:
        if _port_is_open(port):
            _stop_fixture_process(pid)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for the Pi bridge")
def test_packaged_bridge_reports_protocol_only_capability():
    runtime = PiAgentRuntime()

    probe = runtime.probe()

    assert probe["protocol"] == "v1"
    assert probe["execution_ready"] is False
    with pytest.raises(PiBridgeError) as raised:
        runtime.run("must not be echoed")
    assert raised.value.code == "PI_BACKEND_UNAVAILABLE"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for the Pi bridge")
def test_packaged_bridge_sanitizes_invalid_input():
    completed = subprocess.run(
        [shutil.which("node"), str(BRIDGE)],
        input='not-json SECRET\n{"type":"unknown SECRET"}\n',
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )

    events = [json.loads(line) for line in completed.stdout.splitlines()]
    assert events == [
        {
            "type": "error",
            "id": None,
            "error": {
                "code": "INVALID_REQUEST",
                "message": "invalid protocol message",
            },
        },
        {
            "type": "error",
            "id": None,
            "error": {
                "code": "UNSUPPORTED_MESSAGE",
                "message": "unsupported protocol message",
            },
        },
    ]
    assert "SECRET" not in completed.stdout
    assert completed.stderr == ""


def test_successful_response_is_strictly_collected(tmp_path):
    runtime = _fake_runtime(
        tmp_path,
        """
        import json
        import sys

        requests = [json.loads(line) for line in sys.stdin]
        assert requests == [
            {"type": "probe"},
            {"type": "prompt", "id": "prompt-1", "text": "hello"},
        ]
        events = [
            {
                "type": "version",
                "version": "test-1.0",
                "protocol": "v1",
                "execution_ready": True,
            },
            {"type": "started", "id": "prompt-1"},
            {"type": "output", "id": "prompt-1", "text": "answer: HELLO"},
            {
                "type": "finished",
                "id": "prompt-1",
                "status": "completed",
                "result": {"answer": "answer: HELLO"},
            },
        ]
        for event in events:
            print(json.dumps(event), flush=True)
        """,
    )

    result = runtime.run("hello")

    assert result["status"] == "completed"
    assert result["answer"] == "answer: HELLO"
    assert result["outputs"] == ["answer: HELLO"]
    assert result["events"][-1]["result"] == {"answer": "answer: HELLO"}


def test_v1_peer_without_extension_fields_remains_compatible(tmp_path):
    runtime = _fake_runtime(
        tmp_path,
        """
        import json
        import sys

        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.read()
        events = [
            {"type": "version", "version": "legacy-1.0", "protocol": "v1"},
            {"type": "started", "id": "prompt-1"},
            {"type": "output", "id": "prompt-1", "text": "left" + chr(0x2028) + "right"},
            {"type": "finished", "id": "prompt-1", "status": "completed"},
        ]
        for event in events:
            print(json.dumps(event, ensure_ascii=False), flush=True)
        """,
    )

    result = runtime.run("hello")
    assert result["answer"] == "left\u2028right"
    assert result["result"] == {}
    assert runtime.execution_ready is True


def test_output_limit_stops_noisy_bridge(tmp_path):
    runtime = _fake_runtime(
        tmp_path,
        """
        import sys
        import time

        sys.stdin.read()
        sys.stdout.write("x" * 1100000)
        sys.stdout.flush()
        time.sleep(30)
        """,
        timeout=5,
    )

    with pytest.raises(PiBridgeError) as raised:
        runtime.run("hello")
    assert raised.value.code == "PI_BRIDGE_OUTPUT_LIMIT"


@pytest.mark.parametrize(
    "event",
    [
        pytest.param(
            {
                "type": "version",
                "version": "test-1.0",
                "protocol": "v2",
                "execution_ready": True,
            },
            id="protocol-version",
        ),
        pytest.param(
            {
                "type": "version",
                "version": "test-1.0",
                "protocol": "v1",
                "execution_ready": "yes",
            },
            id="execution-ready-type",
        ),
        pytest.param(
            {"type": "started", "id": "different-request"},
            id="response-id",
        ),
    ],
)
def test_mismatched_protocol_or_id_fails_closed(tmp_path, event):
    prefix = []
    if event["type"] != "version":
        prefix.append(
            {
                "type": "version",
                "version": "test-1.0",
                "protocol": "v1",
                "execution_ready": True,
            },
        )
    runtime = _fake_runtime(
        tmp_path,
        f"""
        import json
        import sys

        sys.stdin.read()
        for event in {prefix + [event]!r}:
            print(json.dumps(event), flush=True)
        """,
    )

    with pytest.raises(PiBridgeError) as raised:
        runtime.run("hello")
    assert raised.value.code == "PI_PROTOCOL_INVALID"


def test_malformed_or_noisy_output_is_sanitized(tmp_path):
    runtime = _fake_runtime(
        tmp_path,
        """
        import sys

        sys.stdin.read()
        print("diagnostic noise containing TOP_SECRET", flush=True)
        print("stderr TOP_SECRET", file=sys.stderr, flush=True)
        """,
    )

    with pytest.raises(PiBridgeError) as raised:
        runtime.run("hello")

    assert raised.value.code == "PI_PROTOCOL_INVALID"
    assert "TOP_SECRET" not in str(raised.value)
    assert "diagnostic noise" not in str(raised.value)


def test_invalid_result_shape_fails_closed(tmp_path):
    runtime = _fake_runtime(
        tmp_path,
        """
        import json
        import sys

        sys.stdin.read()
        events = [
            {
                "type": "version",
                "version": "test-1.0",
                "protocol": "v1",
                "execution_ready": True,
            },
            {"type": "started", "id": "prompt-1"},
            {
                "type": "finished",
                "id": "prompt-1",
                "status": "completed",
                "result": None,
            },
        ]
        for event in events:
            print(json.dumps(event), flush=True)
        """,
    )

    with pytest.raises(PiBridgeError) as raised:
        runtime.run("hello")
    assert raised.value.code == "PI_PROTOCOL_INVALID"


def test_extra_event_fields_fail_closed(tmp_path):
    runtime = _fake_runtime(
        tmp_path,
        """
        import json
        import sys

        sys.stdin.read()
        events = [
            {
                "type": "version",
                "version": "test-1.0",
                "protocol": "v1",
                "execution_ready": True,
            },
            {"type": "started", "id": "prompt-1", "debug": "TOP_SECRET"},
            {
                "type": "finished",
                "id": "prompt-1",
                "status": "completed",
                "result": {},
            },
        ]
        for event in events:
            print(json.dumps(event), flush=True)
        """,
    )

    with pytest.raises(PiBridgeError) as raised:
        runtime.run("hello")
    assert raised.value.code == "PI_PROTOCOL_INVALID"
    assert "TOP_SECRET" not in str(raised.value)


def test_structured_bridge_error_preserves_code_and_sanitizes_message(tmp_path):
    runtime = _fake_runtime(
        tmp_path,
        """
        import json
        import sys

        sys.stdin.read()
        events = [
            {
                "type": "version",
                "version": "test-1.0",
                "protocol": "v1",
                "execution_ready": True,
            },
            {
                "type": "error",
                "id": "prompt-1",
                "error": {
                    "code": "PI_BACKEND_UNAVAILABLE",
                    "message": "backend unavailable",
                },
            },
        ]
        for event in events:
            print(json.dumps(event), flush=True)
        """,
    )

    with pytest.raises(PiBridgeError) as raised:
        runtime.run("hello")

    assert raised.value.code == "PI_BACKEND_UNAVAILABLE"
    assert str(raised.value) == "pi bridge reported an execution error"
    assert "backend unavailable" not in str(raised.value)


def test_timeout_terminates_the_full_process_tree(tmp_path):
    runtime, marker = _hung_tree_runtime(tmp_path, timeout=1.0)

    started = time.monotonic()
    with pytest.raises(PiBridgeError) as raised:
        runtime.run("hello")
    elapsed = time.monotonic() - started

    assert raised.value.code == "PI_BRIDGE_TIMEOUT"
    assert elapsed < 5
    _assert_grandchild_stopped(marker)


def test_cancellation_terminates_the_full_process_tree(tmp_path, monkeypatch):
    runtime, marker = _hung_tree_runtime(tmp_path, timeout=10.0)
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
        runtime.run("hello")

    _assert_grandchild_stopped(marker)
