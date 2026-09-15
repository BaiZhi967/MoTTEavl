"""Codex CLI harness。

probe：`codex --version`；执行：`codex exec --json <prompt>`（app-server
JSON-RPC 会话是后续传输形态，先以 CLI 通道打通生命周期与解析）。
"""
from __future__ import annotations

from typing import Any, Callable

from .install import inspect_installation
from .probe import probe_binary
from .process import ProcessRunner
from .protocol import PARSER_VERSION, parse_jsonl


class CodexHarness:
    name = "codex-cli"
    transport = "cli"  # app-server 传输接入后切换

    def __init__(
        self,
        binary: str = "codex",
        *,
        timeout: float = 120.0,
        runner: ProcessRunner | None = None,
        arg_builder: Callable[[str], list[str]] | None = None,
    ) -> None:
        self.binary = binary
        self.process = runner or ProcessRunner(timeout=timeout)
        self._arg_builder = arg_builder or (lambda prompt: [binary, "exec", "--json", prompt])

    async def probe(self) -> dict[str, Any]:
        return await probe_binary(self.binary, name=self.name)

    async def inspect(self) -> dict[str, Any]:
        """本地安装情况：是否安装、路径、来源（npm/homebrew/...）、版本与可用性。"""
        report = await inspect_installation(self.binary, name=self.name)
        report["runnable"] = report["installed"] and report["version_ok"]
        return report

    async def run(self, prompt: str) -> dict[str, Any]:
        result = await self.process.run(self._arg_builder(prompt))
        result["harness"] = self.name
        result["transport"] = self.transport
        result["parser"] = PARSER_VERSION
        result["events"] = parse_jsonl(result["stdout"]) if result["status"] == "exited" else []
        return result
