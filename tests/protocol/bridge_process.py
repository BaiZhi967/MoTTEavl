"""Test helper: drive the packaged Pi bridge with fixed-argv subprocess calls.

Call sites always pass a parameter list (never a shell string).  Fake-bridge
fixtures are fully static scripts written into tmp_path: they reference their
sibling files by __file__, so no path is ever interpolated into code.
"""
from __future__ import annotations

import json
import signal
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from motte_agent.pi import PiAgentRuntime, PiBridgeSession

BRIDGE = Path(__file__).resolve().parents[2] / "bridges" / "pi" / "bridge.mjs"


def run_bridge_with_lines(node: str, payload_lines: list[str]) -> subprocess.CompletedProcess:
    """Run the packaged bridge, feeding JSONL lines on stdin (argv is fixed)."""
    stdin_payload = "\n".join(payload_lines) + "\n"
    return subprocess.run(
        [node, str(BRIDGE)],
        input=stdin_payload,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )


def json_line(message: dict) -> str:
    return json.dumps(message)


def _write_script(path: Path, source: str) -> Path:
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


def fake_runtime(tmp_path, source: str, *, timeout: float = 2.0) -> PiAgentRuntime:
    bridge = _write_script(tmp_path / "fake_pi_bridge.py", source)
    return PiAgentRuntime(
        bridge_path=bridge,
        node_binary=sys.executable,
        timeout_seconds=timeout,
    )


def fake_session_bridge(tmp_path, source: str) -> Path:
    return _write_script(tmp_path / "fake_pi_session_bridge.py", source)


def session_for(
    bridge: Path, *, tmp_path, node=sys.executable, idle=2.0, total=5.0,
) -> PiBridgeSession:
    return PiBridgeSession(
        run_id="run-1",
        case_id="case-1",
        session_id="session-1",
        operation_id="operation-1",
        workspace=str(tmp_path / "workspace"),
        model_config={"id": "scripted-1"},
        responses=[[{"type": "text", "text": "ok"}]],
        tools=["read_file", "write_file", "list_files"],
        bridge_path=bridge,
        node_binary=node,
        idle_timeout=idle,
        total_timeout=total,
    )


def port_is_open(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.1)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def stop_fixture_process(pid: int) -> None:
    try:
        import os

        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass


# 静态模板：fake bridge 启动同目录的 grandchild.py，二者都不携带插值路径。
_HUNG_PARENT_SOURCE = """
    import pathlib
    import subprocess
    import sys
    import time

    script = pathlib.Path(__file__).with_name("grandchild.py")
    child_argv = [sys.executable, str(script)]
    subprocess.Popen(child_argv)
    time.sleep(60)
    """

_GRANDCHILD_SOURCE = """
    import os
    import pathlib
    import socket
    import time

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    marker = pathlib.Path(__file__).with_name("grandchild-ready")
    marker.write_text(f"{server.getsockname()[1]},{os.getpid()}", encoding="utf-8")
    time.sleep(60)
    """


def hung_tree_runtime(tmp_path, *, timeout: float) -> tuple[PiAgentRuntime, Path]:
    """无网络故障注入：父子 fixture 都落在 tmp_path 内，静态模板启动。"""
    _write_script(tmp_path / "grandchild.py", _GRANDCHILD_SOURCE)
    marker = tmp_path / "grandchild-ready"
    return fake_runtime(tmp_path, _HUNG_PARENT_SOURCE, timeout=timeout), marker


def assert_grandchild_stopped(marker: Path) -> None:
    assert marker.exists(), "grandchild did not start before bridge cleanup"
    port_text, pid_text = marker.read_text(encoding="utf-8").split(",")
    port = int(port_text)
    pid = int(pid_text)
    try:
        deadline = time.monotonic() + 5
        while port_is_open(port) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not port_is_open(port), "bridge grandchild survived process-tree cleanup"
    finally:
        if port_is_open(port):
            stop_fixture_process(pid)
