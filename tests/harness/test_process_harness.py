"""ProcessRunner 与 Claude/Codex harness 的真实子进程测试（fake binary）。"""
import asyncio
import json
import os
import sys

import psutil
import pytest

from motte_harness.claude import ClaudeHarness
from motte_harness.codex import CodexHarness
from motte_harness.process import ProcessRunner


def make_fake_binary(tmp_path, name, *, version_line="fake 1.2.3", body='echo \'{"type":"assistant","text":"hi"}\''):
    suffix = ".py" if os.name == "nt" else ""
    script = tmp_path / f"{name}{suffix}"
    if os.name == "nt":
        # Keep the fake executable portable on Windows where /bin/sh and
        # executable-bit semantics are unavailable.
        output_lines = []
        for line in body.splitlines():
            if line.startswith("echo "):
                value = line[5:]
                expression = value if value[:1] in {"'", '"'} else repr(value)
                output_lines.append(f"print({expression})")
            else:
                output_lines.append(line)
        script.write_text(
            "import sys\n"
            f"if len(sys.argv) > 1 and sys.argv[1] == '--version': print({version_line!r})\n"
            "else:\n"
            + "\n".join(f"    {line}" for line in output_lines)
            + "\n",
            encoding="utf-8",
        )
    else:
        script.write_text(
            "#!/bin/sh\n"
            f'if [ "$1" = "--version" ]; then echo "{version_line}"; else\n'
            f"{body}\n"
            "fi\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
    return str(script)


def test_process_runner_captures_exit_code_and_output(tmp_path):
    async def scenario():
        if os.name == "nt":
            command = [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"]
        else:
            command = ["/bin/sh", "-c", "echo out; echo err >&2; exit 3"]
        return await ProcessRunner(timeout=5).run(command)

    result = asyncio.run(scenario())
    assert result["status"] == "exited"
    assert result["exit_code"] == 3
    assert result["stdout"] == "out\n"
    assert result["stderr"] == "err\n"


def test_process_runner_timeout_kills_process_group():
    async def scenario():
        if os.name == "nt":
            command = [sys.executable, "-c", "import time; time.sleep(30); print('late')"]
        else:
            command = ["/bin/sh", "-c", "sleep 30; echo late"]
        return await ProcessRunner(timeout=0.2).run(command)

    result = asyncio.run(scenario())
    assert result["status"] == "timeout"
    assert result["exit_code"] != 0
    assert "late" not in result["stdout"]


def test_process_runner_falls_back_when_event_loop_lacks_subprocess(monkeypatch):
    async def unsupported(*args, **kwargs):
        raise NotImplementedError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unsupported)
    result = asyncio.run(
        ProcessRunner(timeout=5).run([sys.executable, "-c", "print('fallback-ok')"])
    )
    assert result["status"] == "exited"
    assert result["exit_code"] == 0
    assert result["stdout"] == "fallback-ok\n"


def test_process_runner_cancellation_kills_child(tmp_path):
    pid_file = tmp_path / "child.pid"

    async def scenario():
        command = [
            sys.executable, "-c",
            "import os,pathlib,time; "
            f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
            "time.sleep(30)",
        ]
        task = asyncio.create_task(ProcessRunner(timeout=60).run(command))
        for _ in range(100):
            if pid_file.exists():
                break
            await asyncio.sleep(0.02)
        assert pid_file.exists()
        pid = int(pid_file.read_text(encoding="utf-8"))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return pid

    pid = asyncio.run(scenario())
    assert not psutil.pid_exists(pid)


def test_claude_probe_and_run_with_fake_binary(tmp_path):
    binary = make_fake_binary(
        tmp_path,
        "fake-claude",
        body='echo \'{"type":"assistant","text":"hi"}\'\necho not-json',
    )

    async def scenario():
        harness = ClaudeHarness(binary=binary, timeout=5)
        probe = await harness.probe()
        assert probe == {"name": "claude-cli", "version": "1.2.3", "available": True}
        result = await harness.run("say hi")
        return result

    result = asyncio.run(scenario())
    assert result["harness"] == "claude-cli"
    # M4-T06 起 run() 走原生 parser：非 result schema 不假成功
    assert result["parser"] == "claude-json-v1"
    assert result["events"]["status"] == "insufficient"
    assert result["events"]["reason"] == "malformed_json"


def test_codex_run_records_transport_and_events(tmp_path):
    binary = make_fake_binary(tmp_path, "fake-codex", body='echo \'{"type":"item","item":"done"}\'')

    async def scenario():
        return await CodexHarness(binary=binary, timeout=5).run("do it")

    result = asyncio.run(scenario())
    assert result["harness"] == "codex-cli"
    assert result["transport"] == "cli-exec-jsonl"
    # 无 msg 包装/无终态：insufficient，不冒充事件成功
    assert result["events"]["status"] == "insufficient"
    assert json.dumps(result)


def test_probe_reports_missing_binary_as_unavailable():
    async def scenario():
        return await ClaudeHarness(binary="definitely-missing-claude-xyz").probe()

    probe = asyncio.run(scenario())
    assert probe["available"] is False
    assert probe["version"] is None
