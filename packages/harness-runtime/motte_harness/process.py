"""受控子进程运行器：超时杀进程组，采集 stdout/stderr 与退出码。"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from typing import Any


class ProcessRunner:
    def __init__(self, *, timeout: float = 30.0, cwd: str | None = None) -> None:
        self.timeout = timeout
        self.cwd = cwd

    async def run(self, command: list[str], timeout: float | None = None) -> dict[str, Any]:
        started = time.monotonic()
        command = self._normalize_command(command)
        options: dict[str, Any] = {
            "cwd": self.cwd,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if os.name == "nt":
            options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            options["start_new_session"] = True
        process = await asyncio.create_subprocess_exec(*command, **options)
        status = "exited"
        try:
            deadline = self.timeout if timeout is None else timeout
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=deadline)
        except TimeoutError:
            self._kill_group(process.pid, process=process)
            await process.wait()
            stdout, stderr = await process.communicate()
            status = "timeout"
        return {
            "status": status,
            "exit_code": process.returncode,
            "stdout": stdout.decode("utf-8", errors="replace").replace("\r\n", "\n"),
            "stderr": stderr.decode("utf-8", errors="replace").replace("\r\n", "\n"),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "command": list(command),
        }

    @staticmethod
    def _normalize_command(command: list[str]) -> list[str]:
        """Make script entrypoints executable across POSIX and Windows hosts."""
        if os.name != "nt" or not command:
            return list(command)
        executable = str(command[0]).lower()
        if executable.endswith(".py"):
            return [sys.executable, *command]
        if executable.endswith((".cmd", ".bat")):
            comspec = os.environ.get("ComSpec", "cmd.exe")
            return [comspec, "/d", "/s", "/c", subprocess.list2cmdline(command)]
        return list(command)

    @staticmethod
    def _kill_group(pid: int, *, process: Any = None) -> None:
        if os.name == "nt":
            if process is not None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                    timeout=5,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                pass
            return
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (AttributeError, ProcessLookupError, PermissionError):
            pass
