"""M4-T05：有界双管道监督器。

``SupervisedProcess`` 消费受控 argv/env/cwd：stdout/stderr 由后台线程并行
增量读取，单行/累计字节/空闲/总时长都有界；无效 UTF-8 与预算截断分开
报告；interrupt 先原生信号再宽限期后杀进程树；父进程退出后仍在写的
后代被检测并在宽限后清理（残留如实上报，不冒充成功清理）。
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .process import descendants_of, kill_tree, pid_alive


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


class SupervisedProcess:
    """有界双管道监督的受控子进程。"""

    def __init__(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        limits: SupervisedLimits | None = None,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
        name: str = "supervised",
    ) -> None:
        if not argv or not isinstance(argv[0], str):
            raise ValueError("argv must be a non-empty list of strings")
        self.argv = [str(part) for part in argv]
        self._executable = self.argv[0]
        self._arguments = self.argv[1:]
        self.cwd = cwd
        self.env = env
        self.limits = limits or SupervisedLimits()
        self.name = name
        self._on_stdout = on_stdout
        self._on_stderr = on_stderr
        self._process: subprocess.Popen[Any] | None = None
        self._stop_reason: str | None = None
        self._utf8_invalid = False
        self._stdout = bytearray()
        self._stderr = bytearray()
        self._lock: Any = None
        self._started_at: float | None = None
        self._last_progress = -1
        self._idle_since: float | None = None

    # ------------------------------------------------------------ 生命周期

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    def _spawn_locked(self) -> None:
        """spawn 受控进程（无 shell、无字符串拼接的命令构造）。"""
        options: dict[str, Any] = {
            "cwd": self.cwd,
            "env": self.env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "stdin": subprocess.DEVNULL,
        }
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        self._lock = threading.Lock()
        self._started_at = time.monotonic()
        self._idle_since = self._started_at
        try:
            self._process = subprocess.Popen(
                [self._executable, *self._arguments], **options,
            )
        except OSError as error:
            raise SupervisedProcessError(
                "PROCESS_START_FAILED", f"failed to spawn {self._executable}: {error}",
            ) from error
        self._start_readers()

    def start(self) -> None:
        if self._process is not None:
            raise SupervisedProcessError("SUPERVISOR_STATE", "process already started")
        self._spawn_locked()

    def _start_readers(self) -> None:
        assert self._process is not None
        assert self._process.stdout is not None and self._process.stderr is not None
        limits = self.limits

        def read_stream(stream: Any, sink: bytearray, callback: Any) -> None:  # noqa: ANN001
            while True:
                try:
                    chunk = stream.readline(limits.max_line_bytes + 1)
                except (OSError, ValueError):
                    return
                if not chunk:
                    return
                try:
                    chunk.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    with self._lock:
                        self._utf8_invalid = True
                with self._lock:
                    if self._stop_reason:
                        return
                    total = len(self._stdout) + len(self._stderr) + len(chunk)
                    if len(chunk) > limits.max_line_bytes:
                        self._stop_reason = "line_limit"
                        self._signal_native_interrupt()
                        return
                    if total > limits.max_total_bytes:
                        self._stop_reason = "byte_limit"
                        self._signal_native_interrupt()
                        return
                    sink.extend(chunk)
                line = chunk.decode("utf-8", errors="replace")
                if callback is not None:
                    try:
                        callback(line)
                    except Exception:  # noqa: BLE001 - 慢消费者不阻塞采集
                        pass

        for stream, sink, callback in (
            (self._process.stdout, self._stdout, self._on_stdout),
            (self._process.stderr, self._stderr, self._on_stderr),
        ):
            thread = threading.Thread(
                target=read_stream, args=(stream, sink, callback), daemon=True,
                name=f"{self.name}-reader",
            )
            thread.start()

    def _signal_native_interrupt(self) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        if os.name == "nt":
            try:
                os.kill(process.pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            except (OSError, AttributeError):
                process.terminate()
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGINT)
            except (OSError, PermissionError):
                process.terminate()

    def interrupt(self, reason: str = "operator") -> None:
        """原生 interrupt → 宽限期 → 进程树强制终止（M4-A08）。"""
        if self._stop_reason is None:
            self._stop_reason = f"interrupted:{reason}"
        self._signal_native_interrupt()
        process = self._process
        if process is None:
            return
        try:
            process.wait(timeout=self.limits.interrupt_grace)
            return
        except subprocess.TimeoutExpired:
            pass
        kill_tree(process.pid)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass

    def wait(self) -> ProcessOutcome:
        if self._process is None:
            raise SupervisedProcessError("SUPERVISOR_STATE", "process not started")
        started = self._started_at or time.monotonic()
        deadline = started + self.limits.total_timeout
        while True:
            if self._stop_reason:
                self._await_exit_bounded()
                break
            try:
                self._process.wait(timeout=0.1)
                self._await_exit_bounded()
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() > deadline:
                    self._stop_reason = "timeout"
                    kill_tree(self._process.pid)
                    try:
                        self._process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
                    break
                with self._lock:
                    current = len(self._stdout) + len(self._stderr)
                now = time.monotonic()
                if current != self._last_progress:
                    self._last_progress = current
                    self._idle_since = now
                elif self._idle_since is not None and now - self._idle_since > self.limits.idle_timeout:
                    self._stop_reason = "idle_timeout"
                    kill_tree(self._process.pid)
                    try:
                        self._process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
                    break
        duration_ms = int((time.monotonic() - started) * 1000)
        residual = self._collect_residual()
        status = self._outcome_status()
        return ProcessOutcome(
            status=status,
            exit_code=self._process.returncode if self._process else None,
            stdout=self._stdout.decode("utf-8", errors="replace"),
            stderr=self._stderr.decode("utf-8", errors="replace"),
            stdout_bytes=len(self._stdout),
            stderr_bytes=len(self._stderr),
            truncated=self._stop_reason in ("byte_limit", "line_limit", "idle_timeout", "timeout"),
            residual_pids=residual,
            duration_ms=duration_ms,
            detail=self._stop_reason,
        )

    def _await_exit_bounded(self) -> None:
        assert self._process is not None
        try:
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            kill_tree(self._process.pid)
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    def _outcome_status(self) -> str:
        if self._utf8_invalid:
            return "invalid_utf8"
        reason = self._stop_reason or ""
        if reason.startswith("byte_limit"):
            return "byte_limit"
        if reason.startswith("line_limit"):
            return "line_limit"
        if reason.startswith("interrupted"):
            return "interrupted"
        if reason in ("idle_timeout", "timeout"):
            return "timeout"
        return "exited"

    def _collect_residual(self) -> list[int]:
        """父进程退出后仍在写的后代：检测、等待宽限、再强制清理（A08/A09）。"""
        assert self._process is not None
        pid = self._process.pid
        deadline = time.monotonic() + self.limits.residual_grace
        residual = descendants_of(pid)
        while residual and time.monotonic() < deadline:
            time.sleep(0.1)
            residual = [p for p in residual if pid_alive(p)]
        if residual:
            kill_tree(pid)
            for child in residual:
                kill_tree(child)
            residual = [p for p in residual if pid_alive(p)]
        return residual
