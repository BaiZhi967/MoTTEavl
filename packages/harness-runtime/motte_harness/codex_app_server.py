"""M4-T10：codex app-server 传输（JSON-RPC over stdio 子集）——**草案**。

**当前状态（M4 review R17）：未接线生产。** 注册 backend 的 interactive
能力为 False（无持久命令消费者），API 消息端点对 app-server run 返回
501 RUN_COMMANDS_NOT_IMPLEMENTED——不接收 202 让命令永久 queued。

本模块的方法名/审批语义按早期笔记构造，**尚未与官方 app-server 协议**
（initialize/newConversation/sendUserMessage/... + approval request/
respond）对齐；真实接线前不得声称支持。真实二进制（pinned
@openai/codex@0.155.1）经 `codex app-server` 子命令启动；版本漂移在构造
时 fail closed。离线验证用 `SyntheticTransport`
（tests/runtime/test_command_delivery.py）；真实会话证据属 live 单列。
"""
from __future__ import annotations

import json
import subprocess
from typing import Any

from motte_harness.compatibility import CompatibilityError, resolve_pinned_version

PROTOCOL_SUBSET = ("initialize", "thread/start", "turn/create", "thread/stop")


def _check_pinned(binary: str, version_output: str) -> None:
    from motte_harness.install import _parse_version

    pinned = resolve_pinned_version("codex-app-server")
    installed = _parse_version(version_output)
    if installed != pinned:
        raise CompatibilityError(
            "CODEX_APP_SERVER_VERSION_DRIFT",
            f"codex app-server requires {pinned}, found {installed}",
        )


class CodexAppServerTransport:
    """真实 app-server 传输骨架：initialize 握手 + turn/create 投递。

    stdout JSON-RPC 逐行读取；投递返回 ack 与否由 turn/create 的响应
    决定。超时/断连向上抛出（消费者据此记 delivery_unknown）。
    """

    def __init__(
        self,
        *,
        binary: str = "codex",
        version_output: str | None = None,
        cwd: str | None = None,
        request_timeout: float = 30.0,
    ) -> None:
        if version_output is not None:
            _check_pinned(binary, version_output)
        self._binary = binary
        self._cwd = cwd
        self._timeout = request_timeout
        self._process: subprocess.Popen[Any] | None = None
        self._next_id = 0

    def _ensure_started(self) -> None:
        if self._process is not None:
            return
        self._process = subprocess.Popen(
            [self._binary, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=self._cwd,
        )
        self._request("initialize", {"clientInfo": {"name": "motteavl", "version": "1"}})

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._ensure_started()
        assert self._process is not None and self._process.stdout is not None
        self._next_id += 1
        request_id = self._next_id
        message = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        assert self._process.stdin is not None
        self._process.stdin.write((message + "\n").encode("utf-8"))
        self._process.stdin.flush()
        import threading

        result_box: dict[str, Any] = {}

        def read_reply() -> None:
            for raw in self._process.stdout:  # type: ignore[union-attr]
                try:
                    reply = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if reply.get("id") == request_id:
                    result_box["reply"] = reply
                    return

        reader = threading.Thread(target=read_reply, daemon=True)
        reader.start()
        reader.join(timeout=self._timeout)
        if "reply" not in result_box:
            raise TimeoutError(f"app-server request timed out: {method}")
        return result_box["reply"]

    def deliver(self, command: dict[str, Any]) -> dict[str, Any]:
        kind = command.get("type")
        if kind == "interrupt":
            reply = self._request("thread/stop", {"threadId": command.get("session_id")})
            ok = "result" in reply
            return {"acked": ok, "reason": None if ok else "thread/stop error"}
        params: dict[str, Any] = {"threadId": command.get("session_id")}
        if kind == "user_message":
            params["input"] = [{"type": "text", "text": command.get("content") or ""}]
        elif kind in ("approve", "reject"):
            params["input"] = [{
                "type": "text",
                "text": f"[platform:{kind}] request_hash={command.get('request_hash')}",
            }]
        else:
            return {"acked": False, "reason": f"unsupported kind: {kind}"}
        reply = self._request("turn/create", params)
        ok = "result" in reply
        return {"acked": ok, "reason": None if ok else "turn/create error"}

    def close(self) -> None:
        if self._process is None:
            return
        try:
            if self._process.stdin:
                self._process.stdin.close()
            self._process.wait(timeout=3)
        except Exception:  # noqa: BLE001 - 关闭失败不掩盖主流程
            self._process.kill()
        finally:
            self._process = None
