"""Claude CLI harness。

probe：`claude --version`；执行：`claude -p <prompt> --output-format json`。
输出经 JSONL parser（parser version 随结果记录）；terminal channel 保持
deny-by-default（见 channel.py）。
"""
from __future__ import annotations

from typing import Any, Callable

from .probe import probe_binary
from .process import ProcessRunner
from .protocol import PARSER_VERSION, parse_jsonl


class ClaudeHarness:
    name = "claude-cli"

    def __init__(
        self,
        binary: str = "claude",
        *,
        timeout: float = 120.0,
        runner: ProcessRunner | None = None,
        arg_builder: Callable[[str], list[str]] | None = None,
    ) -> None:
        self.binary = binary
        self.process = runner or ProcessRunner(timeout=timeout)
        self._arg_builder = arg_builder or (lambda prompt: [binary, "-p", prompt, "--output-format", "json"])

    async def probe(self) -> dict[str, Any]:
        return await probe_binary(self.binary, name=self.name)

    async def run(self, prompt: str) -> dict[str, Any]:
        result = await self.process.run(self._arg_builder(prompt))
        result["harness"] = self.name
        result["parser"] = PARSER_VERSION
        result["events"] = parse_jsonl(result["stdout"]) if result["status"] == "exited" else []
        return result
