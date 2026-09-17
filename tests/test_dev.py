"""Offline regression tests for the local development supervisor."""
import json
import os
import socket
import sys
import time
from unittest.mock import Mock

import pytest

from apps import dev


@pytest.mark.parametrize("status,identity,body,ready", [
    (200, "launch", {"status": "ok"}, True),
    (503, "launch", {"status": "ok"}, False),
    (302, "launch", {"status": "ok"}, False),
    (200, "other", {"status": "ok"}, False),
    (200, None, {"status": "ok"}, False),
    (200, "launch", {"status": "starting"}, False),
    (200, "launch", "invalid json", False),
])
def test_health_probe(monkeypatch, status, identity, body, ready):
    response = Mock(status=status, headers={"X-Motte-Dev": identity})
    response.read.return_value = json.dumps(body).encode() if isinstance(body, dict) else body.encode()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    connection = Mock()
    connection.getresponse.return_value = response
    factory = Mock(return_value=connection)
    monkeypatch.setattr(dev, "HTTPConnection", factory)
    assert dev.probe_health("launch", 0.25)[0] is ready
    factory.assert_called_once_with("127.0.0.1", 8000, timeout=0.25)
    connection.request.assert_called_once_with("GET", "/health")
    connection.close.assert_called_once()


def test_health_connection_failure(monkeypatch):
    connection = Mock()
    connection.request.side_effect = ConnectionRefusedError("not listening")
    monkeypatch.setattr(dev, "HTTPConnection", Mock(return_value=connection))
    assert dev.probe_health("launch", 0.1) == (False, "not listening")
    connection.close.assert_called_once()


def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(dev.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(dev.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))


def test_wait_retries_until_ready(monkeypatch):
    clock(monkeypatch)
    probe = Mock(side_effect=[(False, "refused"), (True, "ok")])
    monkeypatch.setattr(dev, "probe_health", probe)
    child = Mock()
    dev.wait_ready(child, "launch", timeout=1)
    assert probe.call_count == 2
    assert child.check.call_count == 4


def test_wait_timeout_has_last_diagnostic(monkeypatch):
    clock(monkeypatch)
    monkeypatch.setattr(dev, "probe_health", Mock(return_value=(False, "different server")))
    with pytest.raises(RuntimeError, match="API not ready after 1s.*different server"):
        dev.wait_ready(Mock(), "launch", timeout=1)


def test_child_exit_checked_before_and_after_health(monkeypatch):
    for fail_at in (1, 2):
        child = Mock()
        child.check.side_effect = [None] * (fail_at - 1) + [RuntimeError("API exited")]
        probe = Mock(return_value=(True, "ok"))
        monkeypatch.setattr(dev, "probe_health", probe)
        with pytest.raises(RuntimeError, match="API exited"):
            dev.wait_ready(child, "launch")
        assert probe.call_count == fail_at - 1


def test_busy_port_is_not_reused():
    with socket.socket() as listener:
        listener.bind((dev.HOST, 0))
        listener.listen()
        with pytest.raises(RuntimeError, match="no existing server will be reused"):
            dev.check_port(listener.getsockname()[1])


@pytest.mark.parametrize("failure", ["interrupt", "API", "Web", "ready", "spawn", "port"])
def test_supervisor_order_failure_and_cleanup(monkeypatch, failure):
    events = []

    class FakeChild:
        def __init__(self, name, command, env):
            events.append(f"start {name}")
            self.name = name
            assert env["MOTTE_DEV_TOKEN"]
            if name == "Web":
                assert "ready" in events
                assert "--strictPort" in command
                if failure == "spawn":
                    raise OSError("spawn failed")

        def check(self):
            if "start Web" in events:
                if failure == self.name:
                    raise RuntimeError(f"{self.name} exited")
                if self.name == "Web":
                    raise KeyboardInterrupt

        def stop(self):
            events.append(f"stop {self.name}")

    def ready(*args):
        if failure == "ready":
            raise RuntimeError("API timeout")
        events.append("ready")

    monkeypatch.setattr(dev, "Child", FakeChild)
    monkeypatch.setattr(dev, "wait_ready", ready)
    monkeypatch.setattr(dev.shutil, "which", lambda _: "pnpm")
    monkeypatch.setattr(dev, "check_port", Mock(side_effect=RuntimeError("busy") if failure == "port" else None))
    assert dev.run() == (130 if failure == "interrupt" else 1)
    if failure == "port":
        assert events == []
    elif failure in ("ready", "spawn"):
        assert events[-1] == "stop API"
        assert "stop Web" not in events
    else:
        assert events == ["start API", "ready", "start Web", "stop Web", "stop API"]


@pytest.mark.parametrize("parent_exits", [False, True])
def test_cleanup_owned_descendants(tmp_path, parent_exits):
    """Real subprocess tree on the host OS; no provider, database, or external I/O."""
    marker = tmp_path / "ready"
    # Grandchild holds an OS resource so cleanup can be observed without PID reuse issues.
    grandchild = (
        "import socket,time,pathlib; s=socket.socket(); s.bind(('127.0.0.1',0)); "
        f"s.listen(); pathlib.Path({str(marker)!r}).write_text(str(s.getsockname()[1])); "
        "time.sleep(60)"
    )
    parent = (
        f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{grandchild!r}]); "
        + ("sys.exit(7)" if parent_exits else "time.sleep(60)")
    )
    child = dev.Child("fixture", [sys.executable, "-c", parent], dict(os.environ))
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), "grandchild did not start"
        port = int(marker.read_text())
        if parent_exits:
            child.process.wait(timeout=5)
            with pytest.raises(RuntimeError, match="exit code 7"):
                child.check()
    finally:
        child.stop()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex((dev.HOST, port)) != 0:
                break
        time.sleep(0.02)
    else:
        pytest.fail("owned grandchild still listening after cleanup")
    assert child.process.poll() is not None
