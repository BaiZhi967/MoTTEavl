"""Pi Agent 桥接驱动：spawn `node bridge.mjs`，按协议 v1 双向 JSONL 通信。

默认 bridge 为 bridges/pi/bridge.mjs 的确定性 echo 实现；真实 Pi runtime
接入时替换 bridge 脚本即可，协议不变。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .protocol import decode_message, encode_message
from .runtime import AgentRuntime

DEFAULT_BRIDGE = Path(__file__).resolve().parents[3] / "bridges" / "pi" / "bridge.mjs"


class PiBridgeError(RuntimeError):
    pass


class PiAgentRuntime(AgentRuntime):
    def __init__(self, bridge_path: str | Path | None = None, *, node_binary: str | None = None, timeout_seconds: float = 30.0) -> None:
        self.bridge_path = Path(bridge_path) if bridge_path else DEFAULT_BRIDGE
        self._node = node_binary or shutil.which("node")
        self.timeout_seconds = timeout_seconds
        self.protocol_version: str | None = None
        self.bridge_version: str | None = None

    def available(self) -> bool:
        return self._node is not None and self.bridge_path.exists()

    def probe(self) -> dict[str, Any]:
        """启动 bridge 并完成 probe/version 握手。"""
        events = self._exchange([{"type": "probe"}], expect="version")
        self.protocol_version = events[0].get("protocol")
        self.bridge_version = events[0].get("version")
        return {
            "name": "pi-bridge",
            "version": self.bridge_version,
            "protocol": self.protocol_version,
            "available": True,
        }

    def run(self, prompt: str) -> dict[str, Any]:
        """发送 prompt，收集 started/output/finished 事件直到终态。"""
        if not self.available():
            raise PiBridgeError(f"pi bridge unavailable: node={self._node}, bridge={self.bridge_path}")
        messages = [
            {"type": "probe"},
            {"type": "prompt", "id": "prompt-1", "text": prompt},
        ]
        events = self._exchange(messages, expect="finished")
        self.protocol_version = events[0].get("protocol")
        outputs = [event.get("text", "") for event in events if event["type"] == "output"]
        finished = [event for event in events if event["type"] == "finished"][-1]
        return {
            "status": finished.get("status", "unknown"),
            "outputs": outputs,
            "answer": outputs[-1] if outputs else None,
            "events": events,
        }

    # ------------------------------------------------------------------ 内部

    def _exchange(self, messages: list[dict], expect: str) -> list[dict[str, Any]]:
        process = subprocess.Popen(
            [self._node, str(self.bridge_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        collected: list[dict[str, Any]] = []
        try:
            for message in messages:
                process.stdin.write(encode_message(message))
            process.stdin.close()
            for line in process.stdout:
                try:
                    event = decode_message(line)
                except ValueError as error:
                    raise PiBridgeError(f"bridge protocol violation: {error}") from error
                collected.append(event)
                if event.get("type") == expect:
                    break
            remaining = process.wait(timeout=self.timeout_seconds)
            if remaining != 0:
                raise PiBridgeError(f"bridge exited with {remaining}: {process.stderr.read()[:200]}")
        finally:
            if process.poll() is None:
                process.kill()
        return collected
