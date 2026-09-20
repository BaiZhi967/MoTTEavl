"""受控子进程运行器：超时杀进程组，采集 stdout/stderr 与退出码。

M4-T05 起提供 ``SupervisedProcess``：双管道并行增量消费、单行/总量/
空闲/总时长有界、显式 env/cwd、原生 interrupt → 宽限期 → 进程树终止、
退出后残留子进程检测与清理。``ProcessRunner`` 保留既有兼容语义。
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable


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
        """Make script entrypoints executable across POSIX and Windows hosts.

        .cmd/.bat 入口不再隐式包一层 cmd.exe（M4 起）：仓库内没有该形态的
        调用方，未经评审的 shell 包装是注入面；需要时应显式评审后新增。
        """
        if os.name != "nt" or not command:
            return list(command)
        executable = str(command[0]).lower()
        if executable.endswith(".py"):
            return [sys.executable, *command]
        return list(command)

    @staticmethod
    def _kill_group(pid: int, *, process: Any = None) -> None:
        if process is not None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        kill_tree(pid)


# ---------------------------------------------------------------- M4-T05 监督器

@dataclass(frozen=True)
class SupervisedLimits:
    """双管道有界监督的限制（M4-G09/A05/A06/A08）。"""

    max_line_bytes: int = 1_000_000
    max_total_bytes: int = 8_000_000
    idle_timeout: float = 30.0
    total_timeout: float = 300.0
    interrupt_grace: float = 5.0
    residual_grace: float = 3.0


class SupervisedProcessError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ProcessOutcome:
    status: str  # exited | interrupted | timeout | line_limit | byte_limit | invalid_utf8
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    truncated: bool = False
    residual_pids: list[int] = field(default_factory=list)
    duration_ms: int = 0
    detail: str | None = None


def minimal_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """最小受控环境：只保留运行所必需的主机变量 + 显式附加项。

    env allowlist 是受控输入通道，不是沙箱——隔离语义由调用方（bridge
    沙箱/容器）强制；本函数只保证任务拿不到未声明的宿主变量。
    """
    base: dict[str, str] = {"PATH": os.environ.get("PATH", "")}
    if os.name == "nt":
        for key in ("SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "TEMP", "TMP"):
            value = os.environ.get(key)
            if value is not None:
                base[key] = value
    else:
        for key in ("LANG", "LC_ALL", "HOME"):
            value = os.environ.get(key)
            if value is not None:
                base[key] = value
    if extra:
        base.update({str(k): str(v) for k, v in extra.items()})
    return base


def descendants_of(pid: int) -> list[int]:
    """枚举进程树后代（psutil；失败返回空，绝不误伤无关进程）。"""
    if pid <= 0:
        return []
    try:
        import psutil

        try:
            parent = psutil.Process(int(pid))
        except psutil.Error:
            return []
        found: list[int] = []
        for child in parent.children(recursive=True):
            if child.pid != os.getpid():
                found.append(child.pid)
        return found
    except Exception:  # noqa: BLE001 - 枚举失败按无后代处理
        return []


def kill_tree(pid: int) -> None:
    """强制终止进程树（psutil 枚举后代逐个终止；枚举失败时按 PID 终止）。"""
    try:
        import psutil

        victims: list = []
        if psutil.pid_exists(int(pid)):
            try:
                parent = psutil.Process(int(pid))
                victims.append(parent)
                victims.extend(parent.children(recursive=True))
            except psutil.Error:
                pass
        for victim in victims:
            try:
                victim.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(victims, timeout=2)
        return
    except Exception:  # noqa: BLE001 - psutil 不可用时按单进程终止
        pass
    try:
        os.kill(pid, signal.SIGKILL)
    except (OSError, PermissionError):
        pass


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil

        return psutil.pid_exists(int(pid))
    except Exception:  # noqa: BLE001
        if os.name == "nt":
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def process_identity(pid: int) -> dict[str, Any]:
    """进程创建身份（PID 复用防护）：创建时间 + 命令行。"""
    try:
        import psutil

        info = psutil.Process(int(pid)).as_dict(attrs=["create_time", "cmdline"])
        return {
            "create_time": info.get("create_time"),
            "cmdline": info.get("cmdline") or [],
        }
    except Exception:  # noqa: BLE001
        return {"create_time": None, "cmdline": []}


def identity_matches(record: dict[str, Any], pid: int) -> bool:
    """PID 复用判定：命令行一致才视为同一进程（时间戳缺失时保守拒绝）。"""
    current = process_identity(pid)
    recorded_cmd = [str(part) for part in record.get("cmdline") or []]
    if not recorded_cmd:
        return False
    return current.get("cmdline") == recorded_cmd
