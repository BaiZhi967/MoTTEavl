"""受控子进程运行器：超时杀进程组，采集 stdout/stderr 与退出码。"""
from __future__ import annotations

import asyncio
import os
import signal
import time
from typing import Any


class ProcessRunner:
    def __init__(self, *, timeout: float = 30.0, cwd: str | None = None) -> None:
        self.timeout = timeout
        self.cwd = cwd

    async def run(self, command: list[str], timeout: float | None = None) -> dict[str, Any]:
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        status = "exited"
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout or self.timeout)
        except TimeoutError:
            self._kill_group(process.pid)
            await process.wait()
            stdout, stderr = await process.communicate()
            status = "timeout"
        return {
            "status": status,
            "exit_code": process.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "command": list(command),
        }

    @staticmethod
    def _kill_group(pid: int) -> None:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
