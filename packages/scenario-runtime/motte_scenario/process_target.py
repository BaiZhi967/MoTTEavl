"""受控子进程 Target（M5-T03b / M5-G06）。

可能阻塞且产生副作用的同步 Target 必须通过受控进程执行：线程超时返回不
代表后台已经停止。本模块复用 M4 的 SupervisedProcess（有界双管道、原生
interrupt → 宽限期 → 进程树终止、residual pid 上报），不复制进程终止实现。

Target 协议：子进程读 JSON Lines，每行一个请求，回复一行 JSON。
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Any, Deque

from motte_harness.supervisor import SupervisedLimits, SupervisedProcess


class ProcessTargetError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ProcessTarget:
    """把受控子进程当成一个可 send / interrupt / close 的业务 Target。"""

    def __init__(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        limits: SupervisedLimits | None = None,
        ready_timeout: float = 30.0,
    ) -> None:
        self._argv = list(argv)
        self._ready_timeout = ready_timeout
        self._lines: Deque[dict[str, Any]] = deque()
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._stderr_tail: Deque[str] = deque(maxlen=50)
        self._process = SupervisedProcess(
            self._argv, cwd=cwd, env=env, limits=limits,
            on_stdout=self._on_stdout, on_stderr=self._on_stderr,
            name="scenario-target", interactive_stdin=True,
        )
        self._outcome: Any = None
        self._began = False
        self._closed = False
        self._interrupts: list[dict[str, Any]] = []

    # ------------------------------------------------------------- 回调

    def _on_stdout(self, line: str) -> None:
        text = line.strip()
        if not text:
            return
        try:
            payload = json.loads(text)
        except ValueError:
            payload = {"raw": text}
        with self._condition:
            self._lines.append(payload)
            self._condition.notify_all()

    def _on_stderr(self, line: str) -> None:
        with self._lock:
            self._stderr_tail.append(line)

    # ------------------------------------------------------------- TargetPort

    def begin(self) -> dict[str, Any]:
        if self._began:
            raise ProcessTargetError("TARGET_ALREADY_BEGUN", "target session already begun")
        self._process.start()
        self._began = True
        deadline = time.monotonic() + self._ready_timeout
        while True:
            with self._condition:
                for index, payload in enumerate(self._lines):
                    if payload.get("ready"):
                        del self._lines[index]
                        return {"pid": self._process.pid, **payload}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.interrupt("ready_timeout")
                    raise ProcessTargetError(
                        "TARGET_NOT_READY",
                        "target did not report readiness: " + "".join(self._stderr_tail)[-500:],
                    )
                self._condition.wait(timeout=min(0.05, remaining))

    def send(self, message: str, *, deadline: float | None = None) -> dict[str, Any]:
        if not self._began or self._closed:
            raise ProcessTargetError("TARGET_NOT_ACTIVE", "target session is not accepting sends")
        payload = json.dumps({"message": message}, ensure_ascii=False)
        try:
            self._process.send_line(payload)
        except Exception as error:  # noqa: BLE001 - 写入失败即目标不可用
            raise ProcessTargetError(
                "TARGET_WRITE_FAILED", f"target rejected the request: {type(error).__name__}: {error}"
            ) from error
        response = self._await_line(deadline)
        if response is None:
            stopped = self.interrupt("send_deadline")
            return {
                "output": None,
                "termination_reason": "per_call_timeout",
                "timeout": True,
                "stopped": bool(stopped.get("confirmed")),
                "interrupt": stopped,
            }
        return {
            "output": response.get("output"),
            "termination_reason": response.get("termination_reason", "final_answer"),
            "detail": response.get("detail"),
            "timeout": response.get("timeout") is True,
        }

    def _await_line(self, deadline: float | None) -> dict[str, Any] | None:
        while True:
            with self._condition:
                if self._lines:
                    return self._lines.popleft()
                if self._process.pid is None:
                    return None
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._condition.wait(timeout=0.05 if remaining is None else min(0.05, remaining))

    def observe(self) -> dict[str, Any]:
        return {
            "pid": self._process.pid,
            "pending_lines": len(self._lines),
            "stderr_tail": list(self._stderr_tail)[-5:],
            "interrupts": list(self._interrupts),
        }

    def interrupt(self, reason: str) -> dict[str, Any]:
        """原生 interrupt → 宽限期 → 进程树终止；返回**实际停止确认**。"""
        if self._process.pid is None:
            return {"confirmed": True, "reason": reason, "residual_pids": []}
        self._process.interrupt(reason)
        outcome = self._process.wait()
        self._outcome = outcome
        residual = list(getattr(outcome, "residual_pids", []) or [])
        confirmed = not residual and getattr(outcome, "status", None) in (
            "interrupted", "exited", "timeout",
        )
        evidence = {
            "reason": reason,
            "confirmed": confirmed,
            "status": getattr(outcome, "status", None),
            "exit_code": getattr(outcome, "exit_code", None),
            "residual_pids": residual,
            "duration_ms": getattr(outcome, "duration_ms", None),
        }
        self._interrupts.append(evidence)
        return evidence

    def close(self) -> dict[str, Any]:
        if self._closed:
            return {"state": "closed", "interrupts": list(self._interrupts)}
        self._closed = True
        if self._outcome is None:
            self.interrupt("session_close")
        residual = list(getattr(self._outcome, "residual_pids", []) or [])
        if residual:
            raise ProcessTargetError(
                "TARGET_CLOSE_UNCONFIRMED",
                f"target left residual processes after close: {residual}",
            )
        return {
            "state": "closed",
            "status": getattr(self._outcome, "status", None),
            "interrupts": list(self._interrupts),
        }
