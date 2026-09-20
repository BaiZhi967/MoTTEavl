"""Claude CLI harness（M4-T06：batch transport 固定为 `-p --output-format json`）。

argv 按 docs/protocols/runtime-compatibility.json 的 pinned 形态构造；
进程经 SupervisedProcess 有界监督（双管道/env allowlist/interrupt/残留
清理）；结果经原生 parser（claude-json-v1）。原生重试只在观测内，平台
不做外围完整重跑。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .install import inspect_installation
from .parsers.claude import PARSER_VERSION, parse_claude_batch
from .probe import probe_binary
from .process import ProcessRunner
from .supervisor import SupervisedLimits, SupervisedProcess


class ClaudeHarness:
    name = "claude-cli"
    transport = "cli-batch-json"

    def __init__(
        self,
        binary: str = "claude",
        *,
        timeout: float = 120.0,
        runner: ProcessRunner | None = None,
        arg_builder=None,
    ) -> None:
        self.binary = binary
        self.process = runner or ProcessRunner(timeout=timeout)
        self._arg_builder = arg_builder or (
            lambda prompt: [binary, "-p", prompt, "--output-format", "json"]
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
        result["parser"] = PARSER_VERSION
        result["events"] = (
            parse_claude_batch(result["stdout"]) if result["status"] == "exited" else None
        )
        return result

    def batch_argv(
        self,
        prompt: str,
        *,
        model: str | None = None,
        max_turns: int | None = None,
        permission_mode: str | None = None,
        settings_file: str | Path | None = None,
        extra_dirs: list[str] | None = None,
    ) -> list[str]:
        """pinned batch argv：模型/审批/步数/设置以原生 flag 传递，不能映射就不进 argv。"""
        argv = [self.binary, "-p", prompt, "--output-format", "json"]
        if model:
            argv += ["--model", str(model)]
        if max_turns is not None:
            argv += ["--max-turns", str(int(max_turns))]
        if permission_mode:
            argv += ["--permission-mode", str(permission_mode)]
        if settings_file is not None:
            argv += ["--settings", str(settings_file)]
        for directory in extra_dirs or []:
            argv += ["--add-dir", str(directory)]
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
        cancel_check=None,
        on_spawned=None,
    ) -> dict[str, Any]:
        """受控 batch 执行：SupervisedProcess + 原生 parser。

        ``cancel_check``：运行期间轮询的取消探测（返回原因字符串即打断）；
        ``on_spawned``：spawn 成功后立即回调（持久 session 身份用）；
        原始 stdout/stderr 随结果返回（字节量已被监督上限约束），供上层
        冻结为证据（M4 review R04/R14/R15）。
        """
        effective_argv = argv if argv is not None else self.batch_argv(prompt)
        process = SupervisedProcess(
            effective_argv, cwd=str(cwd), env=env,
            limits=limits or SupervisedLimits(total_timeout=300.0),
            on_stderr=on_event, name=self.name,
            cancel_check=cancel_check,
        )
        process.start()
        if on_spawned is not None:
            try:
                on_spawned(process)
            except Exception:  # noqa: BLE001 - 记录钩子失败不中断执行
                pass
        outcome = process.wait()
        parsed = None
        if outcome.stdout:
            parsed = parse_claude_batch(outcome.stdout)
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
                "detail": outcome.detail,
            },
            "parsed": parsed,
            "argv": list(effective_argv),
            "stdout": outcome.stdout,
            "stderr": outcome.stderr,
        }
