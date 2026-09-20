"""Strict JSONL client for the Pi Agent bridge protocol v2 (M4-T03).

Two layers:

- ``PiAgentRuntime`` — probe-only transport client (CLI doctor / API probe)。
  ``run()`` 显式要求 session（PI_SESSION_REQUIRED）；没有任何 echo 路径。
- ``PiBridgeSession`` — 一次 CaseAttempt 的受控会话客户端：init → run →
  finished，事件增量读取，身份/序列/字段严格校验，行/总量/空闲/总时长
  有界，interrupt 与进程树清理复用既有硬化语义。

stdout 只承载协议；诊断走 stderr 且脱敏（错误消息不透传 bridge 文本）。
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, BinaryIO
from queue import Empty, Queue

from .protocol import decode_message, encode_message
from .runtime import AgentRuntime

DEFAULT_BRIDGE = Path(__file__).resolve().parents[3] / "bridges" / "pi" / "bridge.mjs"
PROTOCOL_VERSION = "v2"
MAX_OUTPUT_BYTES = 32_000_000
MAX_LINE_BYTES = 4_000_000


class PiBridgeError(RuntimeError):
    def __init__(self, message: str, *, code: str = "PI_BRIDGE_ERROR") -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- probe 客户端

class PiAgentRuntime(AgentRuntime):
    """Probe-oriented transport client; execution requires PiBridgeSession."""

    def __init__(
        self,
        bridge_path: str | Path | None = None,
        *,
        node_binary: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.bridge_path = Path(bridge_path) if bridge_path else DEFAULT_BRIDGE
        self._node = node_binary or shutil.which("node")
        self.timeout_seconds = timeout_seconds
        self.protocol_version: str | None = None
        self.bridge_version: str | None = None
        self.sdk_version: str | None = None
        self.execution_ready = False

    def available(self) -> bool:
        """Return whether the bridge transport can be probed, not execution readiness."""
        return self._node is not None and self.bridge_path.is_file()

    def probe(self) -> dict[str, Any]:
        exchange = _BatchExchange(
            self._node, self.bridge_path, timeout_seconds=self.timeout_seconds,
        )
        events = exchange.exchange([{"type": "probe"}], mode="probe")
        version = events[0]
        self._remember_version(version)
        return {
            "name": "pi-bridge",
            "version": self.bridge_version,
            "protocol": self.protocol_version,
            "sdk_version": self.sdk_version,
            "available": self.execution_ready,
            "transport_available": True,
            "execution_ready": self.execution_ready,
        }

    def run(self, prompt: str) -> dict[str, Any]:
        # v2 起执行必须携带 workspace/模型/预算的受控 session；
        # 旧的裸 prompt 交换已被移除，绝不允许退化为 echo。
        raise PiBridgeError(
            "pi execution requires a PiBridgeSession (workspace + model + budgets)",
            code="PI_SESSION_REQUIRED",
        )

    def _remember_version(self, version: dict[str, Any]) -> None:
        self.protocol_version = version["protocol"]
        self.bridge_version = version["version"]
        self.sdk_version = version.get("sdk_version")
        self.execution_ready = bool(version.get("execution_ready", False))


# ---------------------------------------------------------------- 会话客户端

_VERSION_KEYS = (
    {"type", "version", "protocol", "seq"},
    {"type", "version", "protocol", "seq", "execution_ready"},
    {"type", "version", "protocol", "seq", "sdk_version"},
    {"type", "version", "protocol", "seq", "execution_ready", "sdk_version"},
)
_READY_KEYS = frozenset({
    "type", "seq", "run_id", "case_id", "session_id", "operation_id", "sdk_version",
})
_SESSION_EVENT_KEYS = frozenset({
    "type", "seq", "run_id", "case_id", "session_id", "operation_id", "agent_event",
})
_OUTPUT_KEYS = frozenset({
    "type", "seq", "run_id", "case_id", "session_id", "operation_id", "text",
})
_TOOL_CALL_KEYS = frozenset({
    "type", "seq", "run_id", "case_id", "session_id", "operation_id",
    "call_id", "name", "arguments",
})
_TOOL_RESULT_KEYS = frozenset({
    "type", "seq", "run_id", "case_id", "session_id", "operation_id",
    "call_id", "name", "is_error",
})
_FINISHED_KEYS = frozenset({
    "type", "seq", "run_id", "case_id", "session_id", "operation_id", "id", "status", "result",
})
_INTERRUPTED_KEYS = frozenset({
    "type", "seq", "run_id", "case_id", "session_id", "operation_id", "id", "status",
})
_ERROR_KEYS = frozenset({"type", "id", "seq", "error"})
_EVENT_KEYS = {
    "ready": _READY_KEYS,
    "session_event": _SESSION_EVENT_KEYS,
    "output": _OUTPUT_KEYS,
    "tool_call": _TOOL_CALL_KEYS,
    "tool_result": _TOOL_RESULT_KEYS,
    "finished": _FINISHED_KEYS,
    "interrupted": _INTERRUPTED_KEYS,
    "error": _ERROR_KEYS,
}
_KNOWN_AGENT_EVENT_TYPES = frozenset({
    "agent_start", "agent_end", "turn_start", "turn_end",
    "message_start", "message_update", "message_end",
    "budget_stop",
})


class PiBridgeSession:
    """一次受控 bridge 会话：严格身份/序列/字段校验与有界增量读取。"""

    def __init__(
        self,
        *,
        run_id: str,
        case_id: str,
        session_id: str,
        operation_id: str,
        workspace: str | Path,
        model_config: dict[str, Any] | None = None,
        responses: list[Any] | None = None,
        tools: list[str] | None = None,
        system_prompt: str = "",
        budgets: dict[str, Any] | None = None,
        bridge_path: str | Path | None = None,
        node_binary: str | None = None,
        idle_timeout: float = 60.0,
        total_timeout: float = 600.0,
    ) -> None:
        for name, value in (
            ("run_id", run_id), ("case_id", case_id),
            ("session_id", session_id), ("operation_id", operation_id),
        ):
            if not isinstance(value, str) or not value:
                raise PiBridgeError(f"{name} must be a non-empty string", code="PI_SESSION_INVALID")
        self.identity = {
            "run_id": run_id, "case_id": case_id,
            "session_id": session_id, "operation_id": operation_id,
        }
        self.workspace = str(workspace)
        self.model_config = model_config or {}
        self.responses = responses or []
        self.tools = tools or ["read_file", "write_file", "list_files"]
        self.system_prompt = system_prompt
        self.budgets = budgets or {}
        self.bridge_path = Path(bridge_path) if bridge_path else DEFAULT_BRIDGE
        self._node = node_binary or shutil.which("node")
        self.idle_timeout = idle_timeout
        self.total_timeout = total_timeout
        self._process: subprocess.Popen[Any] | None = None
        self._lines: Queue[str | None] = Queue()
        self._overflow = Event()
        self._reader_error: str | None = None
        self._total_read = 0
        self._total_lock = Lock()
        self._closed = False

    # ------------------------------------------------------------ 生命周期

    def start(self) -> dict[str, Any]:
        if self._node is None or not self.bridge_path.is_file():
            raise PiBridgeError("pi bridge transport is unavailable", code="PI_BRIDGE_UNAVAILABLE")
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self._process = subprocess.Popen(
            [self._node, str(self.bridge_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
            creationflags=creationflags,
        )
        self._start_readers()
        try:
            self._send({"type": "probe"})
            version = self._expect(_VERSION_KEYS, terminal=False)
            self._validate_version(version)
            self._send({"type": "init", **self.identity, "workspace": self.workspace,
                        "model": self.model_config, "budgets": self.budgets,
                        "config": {
                            "system_prompt": self.system_prompt,
                            "max_steps": self.budgets.get("max_steps"),
                            "tools": self.tools,
                            "responses": self.responses,
                            "tokens_per_second": self.budgets.get("tokens_per_second"),
                        }})
            ready = None
            while ready is None:
                candidate = self._next_event()
                if candidate.get("type") == "error":
                    error = candidate.get("error") or {}
                    raise PiBridgeError(
                        "pi bridge reported an execution error",
                        code=str(error.get("code") or "PI_BRIDGE_ERROR"),
                    )
                if set(candidate) != _READY_KEYS:
                    raise PiBridgeError(
                        "pi bridge event fields are invalid", code="PI_PROTOCOL_INVALID",
                    )
                ready = candidate
            for field in ("run_id", "case_id", "session_id", "operation_id"):
                if ready.get(field) != self.identity[field]:
                    raise PiBridgeError(
                        "bridge ready event identity mismatch", code="PI_PROTOCOL_INVALID",
                    )
            return ready
        except BaseException:
            self.close()
            raise

    def run(self, text: str) -> dict[str, Any]:
        if self._process is None:
            raise PiBridgeError("session not started", code="PI_SESSION_INVALID")
        if not isinstance(text, str) or not text:
            raise PiBridgeError("prompt text must be non-empty", code="PI_SESSION_INVALID")
        run_id = f"run-{self.identity['operation_id']}"
        self._send({"type": "run", "id": run_id, "text": text})
        events: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []
        outputs: list[str] = []
        interrupted = False
        while True:
            event = self._next_event()
            events.append(event)
            kind = event.get("type")
            if kind == "tool_call":
                tool_calls.append({
                    "call_id": event.get("call_id"),
                    "name": event.get("name"),
                    "arguments": event.get("arguments"),
                })
            elif kind == "output":
                outputs.append(event.get("text") or "")
            elif kind == "error":
                error = event.get("error") or {}
                raise PiBridgeError(
                    "pi bridge reported an execution error", code=str(error.get("code") or "PI_BRIDGE_ERROR"),
                )
            elif kind == "interrupted":
                interrupted = True
            elif kind == "finished":
                return self._envelope(event, events, tool_calls, outputs, interrupted)

    def interrupt(self) -> None:
        if self._process is None:
            return
        try:
            self._send({"type": "interrupt", "id": f"run-{self.identity['operation_id']}"})
        except (OSError, ValueError):
            self.close()

    def close(self) -> None:
        if self._closed or self._process is None:
            self._closed = True
            return
        self._closed = True
        process = self._process
        try:
            if process.stdin and not process.stdin.closed:
                try:
                    process.stdin.close()
                except (OSError, ValueError):
                    pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _terminate_process_tree(process)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            for reader in getattr(self, "_readers", []):
                reader.join(timeout=2)
            self._process = None

    def __enter__(self) -> PiBridgeSession:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------ 读取与校验

    def _start_readers(self) -> None:
        assert self._process is not None
        assert self._process.stdout is not None and self._process.stderr is not None

        def read_bounded(stream: BinaryIO) -> None:
            while True:
                try:
                    chunk = stream.readline(MAX_LINE_BYTES + 1)
                except (OSError, ValueError) as error:
                    self._reader_error = str(error)
                    break
                if not chunk:
                    self._lines.put(None)
                    break
                with self._total_lock:
                    self._total_read += len(chunk)
                    if self._total_read > MAX_OUTPUT_BYTES:
                        self._overflow.set()
                if self._overflow.is_set():
                    break
                if len(chunk) > MAX_LINE_BYTES:
                    self._reader_error = "line exceeds protocol limit"
                    self._overflow.set()
                    break
                try:
                    self._lines.put(chunk.decode("utf-8", errors="strict"))
                except UnicodeDecodeError:
                    self._reader_error = "invalid utf-8 in protocol stream"
                    self._overflow.set()
                    break

        def drain_stderr(stream: BinaryIO) -> None:
            try:
                while stream.read(65536):
                    pass
            except (OSError, ValueError):
                pass

        self._readers = [
            Thread(target=read_bounded, args=(self._process.stdout,), daemon=True),
            Thread(target=drain_stderr, args=(self._process.stderr,), daemon=True),
        ]
        for reader in self._readers:
            reader.start()

    def _send(self, message: dict[str, Any]) -> None:
        assert self._process is not None and self._process.stdin is not None
        try:
            self._process.stdin.write(encode_message(message).encode("utf-8"))
            self._process.stdin.flush()
        except (OSError, ValueError) as error:
            raise PiBridgeError(
                "pi bridge transport closed", code="PI_BRIDGE_EXITED",
            ) from error

    def _next_raw_line(self) -> str:
        try:
            line = self._lines.get(timeout=max(0.05, self.idle_timeout))
        except Empty as error:
            raise PiBridgeError("pi bridge timed out", code="PI_BRIDGE_TIMEOUT") from error
        if line is None:
            raise PiBridgeError(
                "pi bridge closed its protocol stream", code="PI_BRIDGE_EXITED",
            )
        return line

    def _next_event(self) -> dict[str, Any]:
        if self._overflow.is_set():
            raise PiBridgeError(
                self._reader_error or "pi bridge output exceeded the protocol limit",
                code="PI_BRIDGE_OUTPUT_LIMIT",
            )
        line = self._next_raw_line().rstrip("\r\n")
        if not line.strip():
            raise PiBridgeError("pi bridge emitted a blank protocol line", code="PI_PROTOCOL_INVALID")
        try:
            event = decode_message(line)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise PiBridgeError(
                "pi bridge emitted malformed JSONL", code="PI_PROTOCOL_INVALID",
            ) from error
        if not isinstance(event, dict):
            raise PiBridgeError("pi bridge event must be an object", code="PI_PROTOCOL_INVALID")
        return self._validate_event(event)

    def _expect(self, allowed_keys: Any, *, terminal: bool) -> dict[str, Any]:
        event = self._next_event()
        if isinstance(allowed_keys, tuple):
            # version 事件有多种合法字段组合（可选 sdk_version/execution_ready）
            if set(event) not in [set(option) for option in allowed_keys]:
                raise PiBridgeError("pi bridge event fields are invalid", code="PI_PROTOCOL_INVALID")
            return event
        if set(event) != set(allowed_keys):
            raise PiBridgeError("pi bridge event fields are invalid", code="PI_PROTOCOL_INVALID")
        return event

    def _validate_version(self, event: dict[str, Any]) -> None:
        if (
            event.get("type") != "version"
            or event.get("protocol") != PROTOCOL_VERSION
            or not isinstance(event.get("version"), str)
            or not event["version"].strip()
            or ("execution_ready" in event and type(event["execution_ready"]) is not bool)
            or ("sdk_version" in event and event["sdk_version"] is not None
                and not isinstance(event["sdk_version"], str))
        ):
            raise PiBridgeError("invalid pi bridge version event", code="PI_PROTOCOL_INVALID")

    def _validate_event(self, event: dict[str, Any]) -> dict[str, Any]:
        kind = event.get("type")
        if kind == "version":
            self._validate_version(event)
            return event
        allowed = _EVENT_KEYS.get(kind)
        if allowed is None:
            raise PiBridgeError("unknown pi bridge event type", code="PI_PROTOCOL_INVALID")
        if set(event) != allowed:
            raise PiBridgeError("pi bridge event fields are invalid", code="PI_PROTOCOL_INVALID")
        seq = event.get("seq")
        if type(seq) is not int or seq <= getattr(self, "_last_seq", 0):
            raise PiBridgeError("pi bridge event seq is not monotonic", code="PI_PROTOCOL_INVALID")
        self._last_seq = seq
        if kind != "error":
            for field, expected in self.identity.items():
                if event.get(field) != expected:
                    raise PiBridgeError(
                        "pi bridge event identity mismatch", code="PI_PROTOCOL_INVALID",
                    )
        if kind == "session_event":
            agent_event = event.get("agent_event")
            if not isinstance(agent_event, dict) or agent_event.get("type") not in _KNOWN_AGENT_EVENT_TYPES:
                raise PiBridgeError("invalid agent event payload", code="PI_PROTOCOL_INVALID")
            if set(agent_event) not in ({"type"}, {"type", "reason"}):
                raise PiBridgeError("invalid agent event payload", code="PI_PROTOCOL_INVALID")
            if "reason" in agent_event and not isinstance(agent_event["reason"], str):
                raise PiBridgeError("invalid agent event payload", code="PI_PROTOCOL_INVALID")
        elif kind == "finished":
            if not isinstance(event.get("result"), dict):
                raise PiBridgeError("finished event requires a result object", code="PI_PROTOCOL_INVALID")
            if event.get("status") not in ("completed", "cancelled", "error"):
                raise PiBridgeError("finished event status invalid", code="PI_PROTOCOL_INVALID")
        elif kind == "interrupted":
            if event.get("status") != "cancelled":
                raise PiBridgeError("interrupted event status invalid", code="PI_PROTOCOL_INVALID")
        elif kind == "error":
            error = event.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            if (
                not isinstance(error, dict)
                or set(error) != {"code", "message"}
                or not isinstance(code, str)
                or not code.strip()
                or len(code) > 100
                or any(not (c.isalnum() or c in "_.-") for c in code)
                or not isinstance(message, str)
            ):
                raise PiBridgeError("invalid pi bridge error event", code="PI_PROTOCOL_INVALID")
        return event

    def _envelope(
        self,
        finished: dict[str, Any],
        events: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]],
        outputs: list[str],
        interrupted: bool,
    ) -> dict[str, Any]:
        result = finished.get("result") or {}
        return {
            "status": finished.get("status"),
            "final_output": result.get("final_output"),
            "steps": result.get("steps", 0),
            "tool_calls": result.get("tool_calls", 0),
            "usage": result.get("usage") or {"reported": False, "source": "scripted-model"},
            "budget_stop": bool(result.get("budget_stop")),
            "failure": result.get("failure"),
            "interrupted": interrupted,
            "tool_calls_detail": tool_calls,
            "outputs": outputs,
            "events": events,
            "session_id": self.identity["session_id"],
            "operation_id": self.identity["operation_id"],
        }


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


# ---------------------------------------------------------------- 批量交换（probe / 兼容）

class _BatchExchange:
    """一次性写 stdin、收全部 stdout 的受限交换（仅 probe 使用）。"""

    def __init__(
        self, node: str | None, bridge_path: Path, *, timeout_seconds: float,
    ) -> None:
        self._node = node
        self._bridge_path = bridge_path
        self._timeout = timeout_seconds

    def exchange(self, messages: list[dict[str, Any]], *, mode: str) -> list[dict[str, Any]]:
        if self._node is None or not self._bridge_path.is_file():
            raise PiBridgeError("pi bridge transport is unavailable", code="PI_BRIDGE_UNAVAILABLE")
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        process = subprocess.Popen(
            [self._node, str(self._bridge_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
            creationflags=creationflags,
        )
        payload = "".join(encode_message(message) for message in messages).encode("utf-8")
        if len(payload) > MAX_LINE_BYTES:
            _terminate_process_tree(process)
            raise PiBridgeError("pi bridge input exceeded the protocol limit", code="PI_BRIDGE_INPUT_LIMIT")
        buffers = {"stdout": bytearray()}
        total = [0]
        total_lock = Lock()
        overflow = Event()

        def read_bounded(stream: BinaryIO) -> None:
            while chunk := stream.read(65536):
                with total_lock:
                    total[0] += len(chunk)
                    if total[0] > MAX_OUTPUT_BYTES:
                        first = not overflow.is_set()
                        overflow.set()
                    else:
                        first = False
                        buffers["stdout"].extend(chunk)
                if overflow.is_set():
                    if first:
                        _terminate_process_tree(process)
                    return

        def drain_stderr(stream: BinaryIO) -> None:
            try:
                while stream.read(65536):
                    pass
            except (OSError, ValueError):
                pass

        assert process.stdout is not None and process.stderr is not None
        readers = [
            Thread(target=read_bounded, args=(process.stdout,), daemon=True),
            Thread(target=drain_stderr, args=(process.stderr,), daemon=True),
        ]
        for reader in readers:
            reader.start()
        try:
            assert process.stdin is not None
            try:
                process.stdin.write(payload)
                process.stdin.close()
            except BrokenPipeError:
                pass
            process.wait(timeout=self._timeout)
        except subprocess.TimeoutExpired as error:
            _terminate_process_tree(process)
            raise PiBridgeError("pi bridge timed out", code="PI_BRIDGE_TIMEOUT") from error
        except BaseException:
            _terminate_process_tree(process)
            raise
        finally:
            for reader in readers:
                reader.join(timeout=2)
        if overflow.is_set():
            raise PiBridgeError(
                "pi bridge output exceeded the protocol limit", code="PI_BRIDGE_OUTPUT_LIMIT",
            )
        if readers[0].is_alive():
            _terminate_process_tree(process)
            raise PiBridgeError("pi bridge output did not close", code="PI_BRIDGE_EXITED")
        if process.returncode != 0:
            raise PiBridgeError(
                f"pi bridge exited with status {process.returncode}", code="PI_BRIDGE_EXITED",
            )
        try:
            stdout = bytes(buffers["stdout"]).decode("utf-8", errors="strict")
        except UnicodeError as error:
            raise PiBridgeError(
                "pi bridge emitted invalid UTF-8", code="PI_PROTOCOL_INVALID",
            ) from error
        lines = [line for line in stdout.split("\n") if line.strip()]
        events: list[dict[str, Any]] = []
        for line in lines:
            try:
                event = decode_message(line)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise PiBridgeError(
                    "pi bridge emitted malformed JSONL", code="PI_PROTOCOL_INVALID",
                ) from error
            if not isinstance(event, dict):
                raise PiBridgeError("pi bridge event must be an object", code="PI_PROTOCOL_INVALID")
            events.append(event)
        if not events:
            raise PiBridgeError("pi bridge produced no protocol events", code="PI_PROTOCOL_INVALID")
        first = events[0]
        if first.get("protocol") != PROTOCOL_VERSION or first.get("type") != "version":
            raise PiBridgeError("invalid pi bridge version event", code="PI_PROTOCOL_INVALID")
        if mode == "probe" and len(events) != 1:
            raise PiBridgeError("unexpected event after probe", code="PI_PROTOCOL_INVALID")
        return events
