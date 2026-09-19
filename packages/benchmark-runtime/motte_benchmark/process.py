"""受控子进程 Job 适配器：一次 start 覆盖全部 selected case。

所有权规则（M2 需求 4.2）：
- ``prepare`` 只创建受控工作目录（沿用 M1 加固的 ``CaseWorkspace``：逐组件
  拒绝 symlink、dir_fd 打开、路径归属校验），无任何进程执行。
- ``start`` 是唯一产生执行副作用的操作；子进程以独立会话（POSIX
  ``start_new_session`` / Windows ``CREATE_NEW_PROCESS_GROUP``）启动，
  ``launch_token`` 通过环境变量与 argv 占位符传入受控 wrapper。
- 中断/清理只信号**本 Job 拥有**的进程树：会话内凭进程对象 + token，跨会话
  凭 argv 中嵌入的 token 核验（``--launch-token``），核验失败绝不信号——
  PID 被无关进程复用时不会误杀。
- stdout/stderr 有界消费：只保留尾部 ``max_output_bytes`` 字节并标记
  truncated。
- ``collect`` 经 CaseWorkspace 读 ``results.json``：拒绝 symlink 逃逸、
  超大文件与半写 JSON，不伪造记录。

进程执行使用 asyncio 子进程 API（专用事件循环线程承载），不经过 shell。
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import stat
import sys
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from motte_contracts.external_job import (
    CaseResultStatus,
    ExternalJobHandle,
    ExternalJobSpec,
    ExternalJobStatus,
    NormalizedCaseResult,
    new_launch_token,
)
from motte_sandbox.workspace import CaseWorkspace, WorkspacePolicyError

from .protocol import DEFAULT_JOB_LIMITS, BenchmarkRuntimeError

RESULTS_NAME = "results.json"
# 受 wrapper/Runner 承诺的完成标记（review R10）：恢复路径只信该标记——
# 存在部分输出而无标记一律 indeterminate，不升级为成功。
COMPLETION_MARKER = ".motte-job-complete"
TOKEN_ENV = "MOTTE_LAUNCH_TOKEN"
_WIN_CREATE_NEW_PROCESS_GROUP = 0x00000200
_READ_CHUNK = 65536


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _LoopRunner:
    """专用事件循环线程：adapter 的全部 asyncio 操作经此串行执行。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None or self._loop.is_closed():
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(
                    target=self._loop.run_forever, name="motte-job-loop", daemon=True,
                )
                self._thread.start()
            return self._loop

    def run(self, factory, timeout: float | None = 60.0) -> Any:
        future = asyncio.run_coroutine_threadsafe(factory(), self.loop())
        return future.result(timeout)

    def close(self) -> None:
        with self._lock:
            loop, self._loop = self._loop, None
            self._thread = None
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)


class ProcessJobAdapter:
    """进程型外部 Job 适配器（实现 contracts 的 ExternalJobAdapter 协议）。

    ``argv`` 是完整参数列表（不经 shell）；``{work_dir}`` / ``{job_id}`` /
    ``{run_id}`` / ``{launch_token}`` 占位符逐项替换。跨会话身份核验只认
    argv 中嵌入的 launch_token——只放环境变量的 wrapper 无法核验。
    """

    def __init__(
        self,
        *,
        argv: list[str] | None = None,
        module: str | None = None,
        extra_env: Mapping[str, str] | None = None,
        default_limits: Mapping[str, Any] | None = None,
    ) -> None:
        if (argv is None) == (module is None):
            raise ValueError("exactly one of argv or module is required")
        self._argv = [str(item) for item in argv] if argv is not None else None
        self._module = module
        self._extra_env = {str(key): str(value) for key, value in (extra_env or {}).items()}
        self.default_limits: dict[str, Any] = {**DEFAULT_JOB_LIMITS, **(default_limits or {})}
        # 测试/审计观测：每次 start 一条记录（start 计数 = 列表长度）。
        self.start_calls: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self._loop_runner = _LoopRunner()
        self._procs: dict[int, Any] = {}
        self._tokens: dict[int, str] = {}
        self._reader_tasks: list[Any] = []
        self._tails: dict[int, dict[str, Any]] = {}
        self._pid_order: list[int] = []

    # ------------------------------------------------------------------ 准备

    def prepare(self, spec: ExternalJobSpec) -> ExternalJobHandle:
        job_id = f"job-{uuid4().hex}"
        try:
            workspace = CaseWorkspace(
                Path(spec.work_root) / job_id, anchor=Path(spec.work_root),
            )
        except (WorkspacePolicyError, OSError) as error:
            raise BenchmarkRuntimeError(
                "WORKDIR_INVALID", f"cannot prepare controlled work dir: {error}",
            ) from error
        return ExternalJobHandle(
            job_id=job_id,
            run_id=spec.run_id,
            # prepare 阶段的初始身份；真正启动时由 supervisor 换发新 token。
            launch_token=new_launch_token(),
            owned_resources={"kind": "process", "pids": [], "exit_code": None},
            launch_identity={"prepared_at": _now_iso(), "host": socket.gethostname()},
            work_dir=str(workspace.root),
            created_at=_now_iso(),
            status=ExternalJobStatus.prepared,
        )

    # ------------------------------------------------------------------ 启动

    def _effective_limits(self, spec: ExternalJobSpec) -> dict[str, Any]:
        """Per-Job limits：spec.limits 覆盖 adapter 默认值。"""
        merged = dict(self.default_limits)
        for key, value in (spec.limits or {}).items():
            if key in merged:
                merged[key] = value
        return merged

    def start(
        self, spec: ExternalJobSpec, handle: ExternalJobHandle,
    ) -> ExternalJobHandle:
        if not handle.launch_token:
            raise BenchmarkRuntimeError(
                "START_TOKEN_MISSING", "start requires a persisted launch_token",
            )
        # 子进程拿到的 work_dir 一律绝对化：相对 work_root 下 cwd 即工作目录，
        # 相对 MOTTE_WORK_DIR 会在 child 里解析到错误位置（review R01 链路）。
        work_dir = os.path.abspath(handle.work_dir)
        if not os.path.isdir(work_dir):
            raise BenchmarkRuntimeError(
                "WORKDIR_INVALID", f"work dir is not prepared: {work_dir}",
            )
        replacements = {
            "work_dir": work_dir,
            "job_id": handle.job_id,
            "run_id": spec.run_id,
            "launch_token": handle.launch_token,
        }
        argv = [part.format(**replacements) for part in self._base_argv()]
        child_env = {
            **os.environ,
            **self._extra_env,
            TOKEN_ENV: handle.launch_token,
            "MOTTE_JOB_ID": handle.job_id,
            "MOTTE_RUN_ID": spec.run_id,
            "MOTTE_WORK_DIR": work_dir,
        }

        def _spawn() -> Any:
            async def go() -> Any:
                options: dict[str, Any] = {
                    "cwd": work_dir,
                    "env": child_env,
                    "stdout": asyncio.subprocess.PIPE,
                    "stderr": asyncio.subprocess.PIPE,
                }
                if os.name == "nt":
                    options["creationflags"] = _WIN_CREATE_NEW_PROCESS_GROUP
                else:
                    options["start_new_session"] = True
                try:
                    return await asyncio.create_subprocess_exec(*argv, **options)
                except (FileNotFoundError, PermissionError, OSError) as error:
                    raise BenchmarkRuntimeError(
                        "JOB_START_FAILED",
                        f"cannot launch external job process: {error}",
                    ) from error

            return go()

        proc = self._loop_runner.run(_spawn)
        pid = proc.pid
        self._procs[pid] = proc
        self._tokens[pid] = handle.launch_token
        self._pid_order.append(pid)
        self._start_tail_reader(
            proc, max_output_bytes=self._effective_limits(spec)["max_output_bytes"],
        )
        self.start_calls.append({
            "job_id": handle.job_id,
            "run_id": spec.run_id,
            "selected_case_ids": list(spec.selected_case_ids),
            "launch_token": handle.launch_token,
            "pid": pid,
            "argv": list(argv),
        })
        return handle.model_copy(update={
            "status": ExternalJobStatus.active,
            "owned_resources": {
                "kind": "process", "pids": [pid], "exit_code": None,
            },
            "launch_identity": {
                **handle.launch_identity,
                "started_at": _now_iso(),
                "pid": pid,
                "launch_token": handle.launch_token,
                "host": socket.gethostname(),
                "observed_via": "argv-token",
            },
        })

    def _base_argv(self) -> list[str]:
        return (
            self._argv
            if self._argv is not None
            else [sys.executable, "-m", str(self._module)]
        )

    def spawned_processes(self) -> list[int]:
        return list(self._pid_order)

    # ------------------------------------------------------------ 有界输出

    def _start_tail_reader(self, proc: Any, *, max_output_bytes: int) -> None:
        cap = max(int(max_output_bytes), 1)
        state: dict[str, Any] = {
            "stdout": b"", "stderr": b"",
            "stdout_truncated": False, "stderr_truncated": False,
            "cap": cap,
            "lock": threading.Lock(),
        }
        self._tails[proc.pid] = state

        def _drain(stream: Any, key: str, truncated_key: str) -> Any:
            async def go() -> None:
                total = 0
                while True:
                    chunk = await stream.read(_READ_CHUNK)
                    if not chunk:
                        return
                    total += len(chunk)
                    with state["lock"]:
                        state[key] = (state[key] + chunk)[-cap:]
                        if total > cap:
                            state[truncated_key] = True

            return go()

        self._reader_tasks.append(self._loop_runner.loop().create_task(
            _drain(proc.stdout, "stdout", "stdout_truncated"),
        ))
        self._reader_tasks.append(self._loop_runner.loop().create_task(
            _drain(proc.stderr, "stderr", "stderr_truncated"),
        ))

    def output_tail(self) -> dict[str, Any]:
        stdout = b""
        stderr = b""
        truncated_out = False
        truncated_err = False
        cap = int(self.default_limits["max_output_bytes"])
        for pid in self._pid_order:
            state = self._tails.get(pid)
            if state is None:
                continue
            cap = max(cap, int(state.get("cap", cap)))
            with state["lock"]:
                stdout = (stdout + state["stdout"])[-cap:]
                stderr = (stderr + state["stderr"])[-cap:]
                truncated_out = truncated_out or state["stdout_truncated"]
                truncated_err = truncated_err or state["stderr_truncated"]
        return {
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": truncated_out,
            "stderr_truncated": truncated_err,
        }

    # ------------------------------------------------------------------ 观察

    def _owned_proc(self, handle: ExternalJobHandle) -> Any | None:
        pids = handle.owned_resources.get("pids") or []
        pid = pids[0] if pids else None
        if pid is None:
            return None
        if self._procs.get(pid) is None:
            return None
        if self._tokens.get(pid) != handle.launch_token:
            # PID 被我们自己的另一个 Job 复用：同样不是本句柄的进程。
            return None
        return self._procs[pid]

    def _first_pid(self, handle: ExternalJobHandle) -> int | None:
        pids = handle.owned_resources.get("pids") or []
        pid = pids[0] if pids else None
        return int(pid) if isinstance(pid, int) else None

    def _verify_identity(self, pid: int, launch_token: str) -> bool:
        """跨会话身份核验：argv 中必须嵌入了本句柄的 launch_token。"""
        if pid is None or not launch_token:
            return False
        cmdline = self._pid_cmdline(pid)
        return cmdline is not None and launch_token in cmdline

    def _pid_cmdline(self, pid: int) -> str | None:
        if os.name != "posix":
            return None
        proc_cmdline = Path(f"/proc/{pid}/cmdline")
        if proc_cmdline.exists():
            try:
                return proc_cmdline.read_bytes().decode("utf-8", "replace").replace("\0", " ")
            except OSError:
                return None

        def _ps() -> Any:
            async def go() -> str | None:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "ps", "-p", str(pid), "-o", "command=",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                except OSError:
                    return None
                out, _ = await proc.communicate()
                return out.decode("utf-8", "replace") if proc.returncode == 0 else None

            return go()

        try:
            return self._loop_runner.run(_ps, timeout=10.0)
        except BenchmarkRuntimeError:
            return None

    def _results_present(self, handle: ExternalJobHandle) -> bool:
        try:
            target = Path(handle.work_dir) / RESULTS_NAME
            return target.exists() and not target.is_symlink()
        except OSError:
            return False

    def _completion_marker(self, handle: ExternalJobHandle) -> dict[str, Any] | None:
        """读取可信完成标记；缺失/损坏/symlink 一律视为无标记（R10）。"""
        try:
            target = Path(handle.work_dir) / COMPLETION_MARKER
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(target, flags)
        except OSError:
            return None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                return None
            with os.fdopen(fd, "r", encoding="utf-8") as handle_file:
                fd = -1
                data = json.loads(handle_file.read())
        except (OSError, ValueError):
            return None
        finally:
            if fd >= 0:
                os.close(fd)
        if (
            isinstance(data, dict)
            and data.get("completed") is True
            and isinstance(data.get("exit_code"), int)
        ):
            return data
        return None

    def poll(self, handle: ExternalJobHandle) -> ExternalJobHandle:
        proc = self._owned_proc(handle)
        if proc is not None:
            exit_code = proc.returncode
            if exit_code is None:
                return handle.model_copy(update={"status": ExternalJobStatus.active})
            resources = {**handle.owned_resources, "exit_code": exit_code}
            status = ExternalJobStatus.settled if exit_code == 0 else ExternalJobStatus.failed
            return handle.model_copy(update={"status": status, "owned_resources": resources})

        pid = self._first_pid(handle)
        if pid is not None and self._verify_identity(pid, handle.launch_token):
            # 进程仍在且 argv 携带本 token：只观察，不重启。
            return handle.model_copy(update={"status": ExternalJobStatus.active})
        marker = self._completion_marker(handle)
        if marker is not None:
            # 进程不可核验，但 wrapper 落了可信完成标记：按标记的退出码定级。
            exit_code = int(marker["exit_code"])
            resources = {**handle.owned_resources, "exit_code": exit_code}
            status = ExternalJobStatus.settled if exit_code == 0 else ExternalJobStatus.failed
            return handle.model_copy(update={
                "status": status,
                "owned_resources": resources,
                "launch_identity": {
                    **handle.launch_identity,
                    "observed_via": "completion-marker",
                    "exit_code_unobserved": False,
                },
            })
        self.events.append({
            "event": "identity_unverified", "job_id": handle.job_id,
            "pid": pid, "at": _now_iso(),
            "partial_outputs": self._results_present(handle),
        })
        # 无可信完成标记：部分输出不能证明成功退出，保持不确定（R10）。
        return handle.model_copy(update={"status": ExternalJobStatus.indeterminate})

    # ------------------------------------------------------------------ 中断

    def _signal_owned_group(self, proc: Any, grace_seconds: float) -> int | None:
        def _stop() -> Any:
            async def go() -> int | None:
                pid = proc.pid
                try:
                    if os.name == "posix":
                        os.killpg(os.getpgid(pid), signal.SIGTERM)
                    else:
                        proc.terminate()
                except (AttributeError, ProcessLookupError, PermissionError, OSError):
                    pass
                try:
                    return await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
                except (TimeoutError, asyncio.TimeoutError):
                    try:
                        if os.name == "posix":
                            os.killpg(os.getpgid(pid), signal.SIGKILL)
                        else:
                            proc.kill()
                    except (AttributeError, ProcessLookupError, PermissionError, OSError):
                        pass
                    return await proc.wait()

            return go()

        return self._loop_runner.run(_stop, timeout=grace_seconds + 30.0)

    def interrupt(self, handle: ExternalJobHandle) -> ExternalJobHandle:
        grace = float(handle.owned_resources.get(
            "interrupt_grace_seconds", self.default_limits["interrupt_grace_seconds"],
        ))
        proc = self._owned_proc(handle)
        if proc is not None and proc.returncode is None:
            exit_code = self._signal_owned_group(proc, grace)
            resources = {**handle.owned_resources, "exit_code": exit_code}
            return handle.model_copy(update={
                "status": ExternalJobStatus.cancelled,
                "owned_resources": resources,
                "launch_identity": {**handle.launch_identity, "interrupted_at": _now_iso()},
            })
        pid = self._first_pid(handle)
        if pid is not None and self._verify_identity(pid, handle.launch_token):
            # 跨会话但 token 可核验：信号该进程组（同用户权限内）。
            try:
                if os.name == "posix":
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                else:
                    os.kill(pid, signal.SIGTERM)
                deadline = threading.Event()
                deadline.wait(min(grace, 1.0))
                if self._verify_identity(pid, handle.launch_token):
                    try:
                        if os.name == "posix":
                            os.killpg(os.getpgid(pid), signal.SIGKILL)
                        else:
                            os.kill(pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
            except (AttributeError, ProcessLookupError, PermissionError, OSError):
                pass
            return handle.model_copy(update={
                "status": ExternalJobStatus.cancelled,
                "launch_identity": {**handle.launch_identity, "interrupted_at": _now_iso()},
            })
        # 无法证明进程属于本 Job：绝不信号，保留原状态并记录。
        self.events.append({
            "event": "identity_unverified", "job_id": handle.job_id,
            "pid": pid, "at": _now_iso(), "during": "interrupt",
        })
        return handle

    # ------------------------------------------------------------------ 采集

    def collect(
        self,
        handle: ExternalJobHandle,
        cursor: dict[str, Any],
        *,
        max_result_bytes: int | None = None,
    ) -> tuple[list[NormalizedCaseResult], dict[str, Any]]:
        cursor = dict(cursor or {})
        consumed = int(cursor.get("records_consumed", 0))
        limit = int(max_result_bytes or self.default_limits["max_result_bytes"])
        target = Path(handle.work_dir) / RESULTS_NAME
        if not target.exists():
            return ([], cursor)
        try:
            workspace = CaseWorkspace(
                Path(handle.work_dir), anchor=Path(handle.work_dir).parent,
            )
            raw = workspace.read_text(RESULTS_NAME, max_bytes=limit)
        except WorkspacePolicyError as error:
            raise BenchmarkRuntimeError(
                "JOB_OUTPUT_INVALID",
                f"results file rejected by workspace policy: {error.code}: {error}",
            ) from error
        try:
            data = json.loads(raw)
        except ValueError as error:
            raise BenchmarkRuntimeError(
                "JOB_OUTPUT_INVALID", f"results file is not complete JSON: {error}",
            ) from error
        records = data.get("records") if isinstance(data, dict) else None
        if not isinstance(records, list):
            raise BenchmarkRuntimeError(
                "JOB_OUTPUT_INVALID", "results file must contain a records list",
            )
        normalized: list[NormalizedCaseResult] = []
        for item in records:
            if not isinstance(item, dict):
                raise BenchmarkRuntimeError(
                    "JOB_OUTPUT_INVALID", "each record must be an object",
                )
            case_id = item.get("case_id")
            if not isinstance(case_id, str) or not case_id:
                raise BenchmarkRuntimeError(
                    "JOB_OUTPUT_INVALID", "each record requires a nonempty case_id",
                )
            status_value = item.get("status", "succeeded")
            try:
                status = CaseResultStatus(str(status_value))
            except ValueError as error:
                raise BenchmarkRuntimeError(
                    "JOB_OUTPUT_INVALID", f"unknown record status: {status_value!r}",
                ) from error
            normalized.append(NormalizedCaseResult(
                stable_case_key=str(item.get("stable_case_key") or case_id),
                source_case_id=case_id,
                output_ref=(
                    item.get("output_ref")
                    if isinstance(item.get("output_ref"), str) else None
                ),
                output=item.get("output"),
                error=(
                    item.get("error")
                    if isinstance(item.get("error"), dict) else None
                ),
                native_score_refs=[
                    str(ref) for ref in (item.get("native_score_refs") or [])
                    if isinstance(ref, str)
                ],
                status=status,
                error_category=(
                    item.get("error_category")
                    if isinstance(item.get("error_category"), str) else None
                ),
                usage=item.get("usage") if isinstance(item.get("usage"), dict) else {},
                evidence_coverage=(
                    item.get("evidence_coverage")
                    if isinstance(item.get("evidence_coverage"), dict) else {}
                ),
            ))
        new_items = normalized[consumed:]
        cursor["records_consumed"] = len(normalized)
        cursor["source_artifact"] = f"{handle.work_dir}/{RESULTS_NAME}"
        return (new_items, cursor)

    # ------------------------------------------------------------------ 清理

    def cleanup(self, handle: ExternalJobHandle) -> dict[str, Any]:
        report: dict[str, Any] = {
            "job_id": handle.job_id,
            "work_dir": handle.work_dir,
            "interrupted": False,
            "removed": False,
            "leftovers": [],
        }
        proc = self._owned_proc(handle)
        pid = self._first_pid(handle)
        if proc is not None and proc.returncode is None:
            self._signal_owned_group(
                proc, float(self.default_limits["interrupt_grace_seconds"]),
            )
            report["interrupted"] = True
        elif pid is not None and self._verify_identity(pid, handle.launch_token):
            self.interrupt(handle)
            report["interrupted"] = True
        else:
            report["leftovers"].append({
                "kind": "identity_unverified", "pids": [pid] if pid else [],
            })
        if pid is not None:
            self._procs.pop(pid, None)
            self._tokens.pop(pid, None)
        try:
            leftovers = sorted(
                item.name for item in Path(handle.work_dir).iterdir()
            ) if os.path.isdir(handle.work_dir) else []
        except OSError:
            leftovers = ["<unreadable>"]
        if leftovers:
            # 不删除未知状态的工作目录（回退约束）；只列出残留。
            report["leftovers"].append({"kind": "work_dir_files", "names": leftovers})
        return report

    def close(self) -> None:
        self._loop_runner.close()
