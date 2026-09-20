"""M4-T05：有界双管道监督器。

``SupervisedProcess`` 消费受控 argv/env/cwd：stdout/stderr 由后台线程并行
增量读取，单行/累计字节/空闲/总时长都有界；无效 UTF-8 与预算截断分开
报告；interrupt 先原生信号再宽限期后杀进程树；``cancel_check`` 让宿主
（RunService 取消）在运行期间随时按原因打断。

后代所有权是 **OS 级持久边界**（M4 review R05）：POSIX 上受控进程是
session leader（整个树共享其 pgid，父进程退出后 ``killpg`` 仍可达）；
Windows 上受控进程进入 KILL_ON_JOB_CLOSE 的 Job Object（父进程退出后
TerminateJobObject 仍可清理整树）。psutil 枚举只用于**上报**残留 PID
（附创建身份，PID 复用绝不误杀）。成功返回前有界排空 reader 线程，
慢消费者不再吞掉尾部证据（M4 review R12）。
"""
from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .process import (
    descendants_of,
    identity_matches,
    kill_tree,
    pid_alive,
    process_identity,
)


@dataclass(frozen=True)
class SupervisedLimits:
    """双管道有界监督的限制（M4-G09/A05/A06/A08）。"""

    max_line_bytes: int = 1_000_000
    max_total_bytes: int = 8_000_000
    idle_timeout: float = 30.0
    total_timeout: float = 300.0
    interrupt_grace: float = 5.0
    residual_grace: float = 3.0
    drain_timeout: float = 5.0


class SupervisedProcessError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# Windows Job Object（KILL_ON_JOB_CLOSE）：受控进程树的持久所有权边界。
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


def _create_kill_on_close_job() -> int | None:
    """创建 KILL_ON_JOB_CLOSE 的 Job Object；失败返回 None（降级枚举）。"""
    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.windll.kernel32  # noqa: SLJ001
    except AttributeError:
        return None

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info), ctypes.sizeof(info),
    ):
        kernel32.CloseHandle(job)
        return None
    return int(job)


def _assign_process_to_job(job: int, pid: int) -> bool:
    try:
        kernel32 = ctypes.windll.kernel32  # noqa: SLJ001
    except AttributeError:
        return False
    process_handle = kernel32.OpenProcess(
        _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, int(pid),
    )
    if not process_handle:
        return False
    try:
        return bool(kernel32.AssignProcessToJobObject(job, process_handle))
    finally:
        kernel32.CloseHandle(process_handle)


def _terminate_job(job: int) -> None:
    try:
        kernel32 = ctypes.windll.kernel32  # noqa: SLJ001
    except AttributeError:
        return
    kernel32.TerminateJobObject(job, 1)


def _close_job(job: int) -> None:
    try:
        kernel32 = ctypes.windll.kernel32  # noqa: SLJ001
    except AttributeError:
        return
    kernel32.CloseHandle(job)


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
        cancel_check: Callable[[], str | None] | None = None,
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
        self._cancel_check = cancel_check
        self._process: subprocess.Popen[Any] | None = None
        self._readers: list[threading.Thread] = []
        self._stop_reason: str | None = None
        self._utf8_invalid = False
        self._stdout = bytearray()
        self._stderr = bytearray()
        self._lock: Any = None
        self._started_at: float | None = None
        self._last_progress = -1
        self._idle_since: float | None = None
        # 后代所有权登记：父进程存活期间持续快照（pid -> 创建身份），
        # 供 residual 上报与 PID 复用核对（M4 review R05）。
        self._descendants: dict[int, dict[str, Any]] = {}
        self._last_descendant_scan = -1.0
        # OS 级持久边界：Windows Job Object（KILL_ON_JOB_CLOSE）。
        self._job: int | None = None

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
        if os.name == "nt":
            # Job Object 是 Windows 上跨父进程退出的持久所有权边界；
            # 创建/指派失败降级为 psutil 枚举清理（POSIX 走 session pgid）。
            job = _create_kill_on_close_job()
            if job is not None and _assign_process_to_job(job, self._process.pid):
                self._job = job
            elif job is not None:
                _close_job(job)
        self._scan_descendants()
        self._start_readers()

    def start(self) -> None:
        if self._process is not None:
            raise SupervisedProcessError("SUPERVISOR_STATE", "process already started")
        self._spawn_locked()

    def _scan_descendants(self) -> None:
        """父进程存活期间登记后代身份；父进程已退出时保留既有登记。"""
        assert self._process is not None
        if self._process.poll() is not None:
            return
        now = time.monotonic()
        if now - self._last_descendant_scan < 0.1:
            return
        self._last_descendant_scan = now
        for pid in descendants_of(self._process.pid):
            if pid not in self._descendants:
                self._descendants[pid] = process_identity(pid)

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
            self._readers.append(thread)
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
            if self._cancel_check is not None:
                try:
                    cancel_reason = self._cancel_check()
                except Exception:  # noqa: BLE001 - 探测失败按未取消处理
                    cancel_reason = None
                if cancel_reason:
                    self.interrupt(str(cancel_reason))
                    continue
            try:
                self._process.wait(timeout=0.1)
                self._await_exit_bounded()
                break
            except subprocess.TimeoutExpired:
                self._scan_descendants()
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
        drain_note = self._drain_readers()
        residual = self._collect_residual()
        status = self._outcome_status()
        detail = self._stop_reason
        truncated = self._stop_reason in ("byte_limit", "line_limit", "idle_timeout", "timeout")
        if drain_note is not None:
            detail = f"{detail};{drain_note}" if detail else drain_note
            truncated = True
        self._release_ownership()
        return ProcessOutcome(
            status=status,
            exit_code=self._process.returncode if self._process else None,
            stdout=self._stdout.decode("utf-8", errors="replace"),
            stderr=self._stderr.decode("utf-8", errors="replace"),
            stdout_bytes=len(self._stdout),
            stderr_bytes=len(self._stderr),
            truncated=truncated,
            residual_pids=residual,
            duration_ms=duration_ms,
            detail=detail,
        )

    def _drain_readers(self) -> str | None:
        """有界排空 reader 线程：管道 EOF 后线程应立即退出（M4 review R12）。

        慢消费者在 ``drain_timeout`` 内未排完 → 明确降完整度
        （``reader_drain_timeout``），不冒充已采集全部输出。
        """
        drain_deadline = time.monotonic() + self.limits.drain_timeout
        for reader in self._readers:
            remaining = max(0.0, drain_deadline - time.monotonic())
            reader.join(timeout=remaining)
        stuck = [reader.name for reader in self._readers if reader.is_alive()]
        return f"reader_drain_timeout:{','.join(stuck)}" if stuck else None

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
        """父进程退出后仍在写的后代：检测、等待宽限、再强制清理（A08/A09）。

        清理先走 **OS 级所有权边界**（父进程退出后仍可达）：
        - POSIX：受控进程是 session leader，``killpg(pgid)`` 覆盖整个树；
        - Windows：Job Object ``TerminateJobObject`` 覆盖整个树。

        候选 = 存活期登记的后代 ∪（父进程仍存活时的）当前枚举。上报前逐个
        核对创建身份：身份不符（PID 复用）绝不杀，只如实上报（R05）。
        """
        assert self._process is not None
        pid = self._process.pid
        candidates = dict(self._descendants)
        if self._process.poll() is None:
            kill_tree(pid)
        # 持久边界清理：无论父进程是否存活，整个树都会被终止。
        if os.name != "nt":
            try:
                os.killpg(pid, signal.SIGKILL)  # type: ignore[attr-defined]
            except (OSError, PermissionError):
                pass
        if self._job is not None:
            _terminate_job(self._job)
        if self._process.poll() is None:
            for child in descendants_of(pid):
                if child not in candidates:
                    candidates[child] = process_identity(child)
        deadline = time.monotonic() + self.limits.residual_grace
        while candidates and time.monotonic() < deadline:
            candidates = {
                child: identity
                for child, identity in candidates.items()
                if pid_alive(child)
            }
            if not candidates:
                break
            time.sleep(0.1)
        residual: list[int] = []
        for child, identity in candidates.items():
            if pid_alive(child) and identity_matches(identity, child):
                kill_tree(child)
                residual.append(child)
        return [child for child in residual if pid_alive(child)]

    def _release_ownership(self) -> None:
        """释放所有权边界句柄（Windows Job 关闭即 KILL_ON_JOB_CLOSE 兜底）。"""
        if self._job is not None:
            _close_job(self._job)
            self._job = None
