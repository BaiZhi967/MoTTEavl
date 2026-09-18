"""Strict JSONL client for the optional Pi Agent bridge protocol v1."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, BinaryIO

from .protocol import decode_message, encode_message
from .runtime import AgentRuntime

DEFAULT_BRIDGE = Path(__file__).resolve().parents[3] / "bridges" / "pi" / "bridge.mjs"
MAX_OUTPUT_BYTES = 1_000_000


class PiBridgeError(RuntimeError):
    def __init__(self, message: str, *, code: str = "PI_BRIDGE_ERROR") -> None:
        super().__init__(message)
        self.code = code


class PiAgentRuntime(AgentRuntime):
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
        self.execution_ready = False

    def available(self) -> bool:
        """Return whether the bridge transport can be probed, not execution readiness."""
        return self._node is not None and self.bridge_path.is_file()

    def probe(self) -> dict[str, Any]:
        events = self._exchange([{"type": "probe"}], mode="probe")
        version = events[0]
        self._remember_version(version)
        return {
            "name": "pi-bridge",
            "version": self.bridge_version,
            "protocol": self.protocol_version,
            "available": self.execution_ready,
            "transport_available": True,
            "execution_ready": self.execution_ready,
        }

    def run(self, prompt: str) -> dict[str, Any]:
        if not self.available():
            raise PiBridgeError("pi bridge transport is unavailable", code="PI_BRIDGE_UNAVAILABLE")
        events = self._exchange(
            [
                {"type": "probe"},
                {"type": "prompt", "id": "prompt-1", "text": prompt},
            ],
            mode="run",
        )
        self._remember_version(events[0])
        outputs = [event["text"] for event in events if event["type"] == "output"]
        finished = events[-1]
        return {
            "status": finished["status"],
            "outputs": outputs,
            "answer": outputs[-1] if outputs else None,
            "result": finished.get("result", {}),
            "events": events,
        }

    def _remember_version(self, version: dict[str, Any]) -> None:
        self.protocol_version = version["protocol"]
        self.bridge_version = version["version"]
        self.execution_ready = version.get("execution_ready", True)

    def _exchange(self, messages: list[dict[str, Any]], *, mode: str) -> list[dict[str, Any]]:
        if not self.available():
            raise PiBridgeError("pi bridge transport is unavailable", code="PI_BRIDGE_UNAVAILABLE")
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        process = subprocess.Popen(
            [self._node, str(self.bridge_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
            creationflags=creationflags,
        )
        payload = "".join(encode_message(message) for message in messages).encode("utf-8")
        if len(payload) > MAX_OUTPUT_BYTES:
            self._terminate_process_tree(process)
            raise PiBridgeError("pi bridge input exceeded the protocol limit", code="PI_BRIDGE_INPUT_LIMIT")
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        total = [0]
        total_lock = Lock()
        overflow = Event()

        def read_bounded(name: str, stream: BinaryIO) -> None:
            while chunk := stream.read(65536):
                with total_lock:
                    total[0] += len(chunk)
                    if total[0] > MAX_OUTPUT_BYTES:
                        first_overflow = not overflow.is_set()
                        overflow.set()
                    else:
                        first_overflow = False
                        buffers[name].extend(chunk)
                if overflow.is_set():
                    if first_overflow:
                        self._terminate_process_tree(process)
                    return

        assert process.stdout is not None and process.stderr is not None
        readers = [
            Thread(target=read_bounded, args=("stdout", process.stdout), daemon=True),
            Thread(target=read_bounded, args=("stderr", process.stderr), daemon=True),
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
            process.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as error:
            self._terminate_process_tree(process)
            raise PiBridgeError("pi bridge timed out", code="PI_BRIDGE_TIMEOUT") from error
        except BaseException:
            self._terminate_process_tree(process)
            raise
        finally:
            for reader in readers:
                reader.join(timeout=2)
        if overflow.is_set():
            raise PiBridgeError(
                "pi bridge output exceeded the protocol limit", code="PI_BRIDGE_OUTPUT_LIMIT"
            )
        if any(reader.is_alive() for reader in readers):
            self._terminate_process_tree(process)
            raise PiBridgeError("pi bridge output did not close", code="PI_BRIDGE_EXITED")
        if process.returncode != 0:
            raise PiBridgeError(
                f"pi bridge exited with status {process.returncode}",
                code="PI_BRIDGE_EXITED",
            )
        try:
            stdout = bytes(buffers["stdout"]).decode("utf-8", errors="strict")
            bytes(buffers["stderr"]).decode("utf-8", errors="strict")
        except UnicodeError as error:
            raise PiBridgeError(
                "pi bridge emitted invalid UTF-8", code="PI_PROTOCOL_INVALID"
            ) from error
        events = self._decode_output(stdout)
        self._validate_events(events, mode=mode)
        return events

    @staticmethod
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
                except ProcessLookupError:
                    pass
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass

    @staticmethod
    def _decode_output(stdout: str) -> list[dict[str, Any]]:
        if not stdout:
            raise PiBridgeError("pi bridge produced no protocol events", code="PI_PROTOCOL_INVALID")
        lines = stdout.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        lines = [line[:-1] if line.endswith("\r") else line for line in lines]
        if any(not line.strip() for line in lines):
            raise PiBridgeError("pi bridge emitted a blank protocol line", code="PI_PROTOCOL_INVALID")
        events: list[dict[str, Any]] = []
        for line in lines:
            try:
                event = decode_message(line)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise PiBridgeError(
                    "pi bridge emitted malformed JSONL", code="PI_PROTOCOL_INVALID"
                ) from error
            if not isinstance(event, dict):
                raise PiBridgeError(
                    "pi bridge event must be an object", code="PI_PROTOCOL_INVALID"
                )
            events.append(event)
        return events

    @staticmethod
    def _require_keys(event: dict[str, Any], expected: set[str]) -> None:
        if set(event) != expected:
            raise PiBridgeError("pi bridge event fields are invalid", code="PI_PROTOCOL_INVALID")

    @classmethod
    def _validate_version(cls, event: dict[str, Any]) -> None:
        if set(event) not in (
            {"type", "version", "protocol"},
            {"type", "version", "protocol", "execution_ready"},
        ):
            raise PiBridgeError("invalid pi bridge version event", code="PI_PROTOCOL_INVALID")
        if (
            event.get("type") != "version"
            or event.get("protocol") != "v1"
            or not isinstance(event.get("version"), str)
            or not event["version"].strip()
            or ("execution_ready" in event and type(event["execution_ready"]) is not bool)
        ):
            raise PiBridgeError("invalid pi bridge version event", code="PI_PROTOCOL_INVALID")

    @classmethod
    def _validate_events(cls, events: list[dict[str, Any]], *, mode: str) -> None:
        if not events:
            raise PiBridgeError("pi bridge produced no protocol events", code="PI_PROTOCOL_INVALID")
        cls._validate_version(events[0])
        if mode == "probe":
            if len(events) != 1:
                raise PiBridgeError("unexpected event after probe", code="PI_PROTOCOL_INVALID")
            return
        if mode != "run":
            raise ValueError(f"unknown exchange mode: {mode}")
        if len(events) < 2:
            raise PiBridgeError("pi bridge omitted a terminal event", code="PI_PROTOCOL_INVALID")

        expected_id = "prompt-1"
        started = False
        for index, event in enumerate(events[1:], start=1):
            event_type = event.get("type")
            if event_type not in {"started", "output", "finished", "error"}:
                raise PiBridgeError("unknown pi bridge event type", code="PI_PROTOCOL_INVALID")
            expected_keys = {
                "started": {"type", "id"},
                "output": {"type", "id", "text"},
                "finished": (
                    {"type", "id", "status", "result"}
                    if "result" in event else {"type", "id", "status"}
                ),
                "error": {"type", "id", "error"},
            }[event_type]
            cls._require_keys(event, expected_keys)
            if event.get("id") != expected_id:
                raise PiBridgeError("pi bridge event id mismatch", code="PI_PROTOCOL_INVALID")
            if event_type == "error":
                error = event.get("error")
                code = error.get("code") if isinstance(error, dict) else None
                message = error.get("message") if isinstance(error, dict) else None
                if (
                    index != len(events) - 1
                    or not isinstance(error, dict)
                    or set(error) != {"code", "message"}
                    or not isinstance(code, str)
                    or not code.strip()
                    or len(code) > 100
                    or any(not (character.isalnum() or character in "_.-") for character in code)
                    or not isinstance(message, str)
                    or not message.strip()
                ):
                    raise PiBridgeError("invalid pi bridge error event", code="PI_PROTOCOL_INVALID")
                raise PiBridgeError("pi bridge reported an execution error", code=code)
            if event_type == "started":
                if started or index != 1:
                    raise PiBridgeError("invalid pi bridge started event", code="PI_PROTOCOL_INVALID")
                started = True
            elif event_type == "output":
                if not started or not isinstance(event.get("text"), str):
                    raise PiBridgeError("invalid pi bridge output event", code="PI_PROTOCOL_INVALID")
            elif event_type == "finished":
                if (
                    not started
                    or index != len(events) - 1
                    or event.get("status") != "completed"
                    or ("result" in event and not isinstance(event.get("result"), dict))
                ):
                    raise PiBridgeError("invalid pi bridge finished event", code="PI_PROTOCOL_INVALID")
        if events[-1].get("type") not in {"finished", "error"}:
            raise PiBridgeError("pi bridge omitted a terminal event", code="PI_PROTOCOL_INVALID")
