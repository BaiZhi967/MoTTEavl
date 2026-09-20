"""Codex CLI harness（M4-T07：batch transport 固定为 `exec --json`）。

app-server JSON-RPC 是独立 transport/version（M4-T10），不作为 batch 的
fallback。进程经 SupervisedProcess 有界监督；事件流经 codex-jsonl-v1
parser（thread/turn/item + 终态语义）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .install import inspect_installation
from .parsers.codex import PARSER_VERSION, parse_codex_exec_events
from .probe import probe_binary
from .process import ProcessRunner
from .supervisor import SupervisedLimits, SupervisedProcess


class CodexHarness:
    name = "codex-cli"
    transport = "cli-exec-jsonl"

    def __init__(
        self,
        binary: str = "codex",
        *,
        timeout: float = 120.0,
        runner: ProcessRunner | None = None,
        arg_builder=None,
    ) -> None:
        self.binary = binary
        self.process = runner or ProcessRunner(timeout=timeout)
        self._arg_builder = arg_builder or (
            lambda prompt: [binary, "exec", "--json", prompt]
        )

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
        result["events"] = (
            parse_codex_exec_events(result["stdout"])
            if result["status"] == "exited" else None
        )
        return result

    def batch_argv(
        self,
        prompt: str,
        *,
        model: str | None = None,
        config_overrides: list[tuple[str, str]] | None = None,
        sandbox: str | None = None,
        skip_git_repo_check: bool = True,
    ) -> list[str]:
        argv = [self.binary, "exec", "--json"]
        if model:
            argv += ["-m", str(model)]
        for key, value in config_overrides or []:
            argv += ["-c", f"{key}={value}"]
        if sandbox:
            argv += ["--sandbox", str(sandbox)]
        if skip_git_repo_check:
            argv += ["--skip-git-repo-check"]
        argv.append(prompt)
        return argv

    def run_batch(
        self,
        prompt: str,
        *,
        cwd: str | Path,
        env: dict[str, str] | None = None,
        argv: list[str] | None = None,
        limits: SupervisedLimits | None = None,
        on_event=None,
    ) -> dict[str, Any]:
        effective_argv = argv if argv is not None else self.batch_argv(prompt)
        process = SupervisedProcess(
            effective_argv, cwd=str(cwd), env=env,
            limits=limits or SupervisedLimits(total_timeout=300.0),
            on_stderr=on_event, name=self.name,
        )
        process.start()
        outcome = process.wait()
        parsed = None
        if outcome.stdout:
            parsed = parse_codex_exec_events(outcome.stdout)
        return {
            "harness": self.name,
            "transport": self.transport,
            "parser": PARSER_VERSION,
            "process": {
                "status": outcome.status,
                "exit_code": outcome.exit_code,
                "stdout_bytes": outcome.stdout_bytes,
                "stderr_bytes": outcome.stderr_bytes,
                "truncated": outcome.truncated,
                "residual_pids": outcome.residual_pids,
                "duration_ms": outcome.duration_ms,
            },
            "parsed": parsed,
            "argv": list(effective_argv),
        }
