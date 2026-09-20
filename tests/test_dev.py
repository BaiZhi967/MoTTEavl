"""Offline regression tests for the local development supervisor."""
import json
import os
from pathlib import Path
import socket
import sys
import time
from unittest.mock import Mock

import pytest

from apps import dev


def test_api_reload_scope_excludes_local_worktrees():
    """The reloading API child must never watch a sibling checkout under `.worktree/`.

    uvicorn's stat reloader seeds its watch set from every `*.py` beneath each reload
    directory and defaults to the working directory (the repository root), which made
    unrelated edits in other local worktrees restart this API.
    """
    command = dev.api_command()
    assert "--reload" in command
    given = [command[index + 1]
             for index, argument in enumerate(command) if argument == "--reload-dir"]
    assert all(not Path(value).is_absolute() for value in given), "paths resolve against ROOT"
    watched = [(dev.ROOT / value).resolve() for value in given]
    assert all(directory.is_dir() for directory in watched), "uvicorn refuses missing reload dirs"
    assert dev.ROOT / "apps" in watched, "the API's own code must stay hot-reloading"
    assert dev.ROOT / "packages" in watched
    assert dev.ROOT not in watched, "watching the repository root re-includes .worktree"
    assert not any((dev.ROOT / ".worktree").is_relative_to(directory) for directory in watched)


def arguments(command, flag):
    return [command[index + 1] for index, argument in enumerate(command) if argument == flag]


def test_reload_scope_is_overridable_per_run(monkeypatch):
    """A run may watch another whitelist, or ask for exclusions, without editing code."""
    monkeypatch.setenv(dev.RELOAD_DIRS_ENV, f"apps, scripts{os.pathsep}packages")
    monkeypatch.setenv(dev.RELOAD_EXCLUDE_ENV, "apps *.pyc")
    command = dev.api_command()
    assert arguments(command, "--reload-dir") == ["apps", "scripts", "packages"]
    # a directory becomes absolute (see test_directory_excludes_are_made_absolute);
    # a glob is passed through for uvicorn to match per path
    assert arguments(command, "--reload-exclude") == [str((dev.ROOT / "apps").resolve()), "*.pyc"]
    assert dev.RELOAD_DIRS == ("apps", "packages"), "defaults stay the API's own source"


def test_directory_excludes_are_made_absolute(monkeypatch):
    """A relative directory would exclude nothing: uvicorn matches exclude directories
    against the absolute paths the watcher reports (measured — `.worktree` leaked)."""
    monkeypatch.setenv(dev.RELOAD_EXCLUDE_ENV, ".worktree")
    assert dev.reload_watch()[1] == (str((dev.ROOT / ".worktree").resolve()),)
    monkeypatch.setenv(dev.RELOAD_EXCLUDE_ENV, str(dev.ROOT / ".worktree"))
    assert dev.reload_watch()[1] == (str((dev.ROOT / ".worktree").resolve()),)
    monkeypatch.setenv(dev.RELOAD_EXCLUDE_ENV, "missing-dir")
    assert dev.reload_watch()[1] == ("missing-dir",), "unresolvable entries stay patterns"


@pytest.mark.parametrize("value,expected", [("", ()), ("   ", ()), ("*.pyc", ("*.pyc",))])
def test_reload_exclude_defaults_to_nothing(monkeypatch, value, expected):
    monkeypatch.delenv(dev.RELOAD_DIRS_ENV, raising=False)
    monkeypatch.setenv(dev.RELOAD_EXCLUDE_ENV, value)
    assert dev.reload_watch() == (dev.RELOAD_DIRS, expected)


def test_excludes_warn_that_watchfiles_is_required(monkeypatch, capsys):
    """Without watchfiles uvicorn drops --reload-exclude, so a silent no-op is a trap."""
    monkeypatch.setattr(dev, "watchfiles_installed", lambda: False)
    dev.warn_unwatchable_excludes((".worktree",))
    stderr = capsys.readouterr().err
    assert dev.RELOAD_EXCLUDE_ENV in stderr and "watchfiles" in stderr

    dev.warn_unwatchable_excludes(())
    monkeypatch.setattr(dev, "watchfiles_installed", lambda: True)
    dev.warn_unwatchable_excludes((".worktree",))
    assert capsys.readouterr().err == ""


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
