"""M4-T06/T07：Claude / Codex batch 的平台执行器（共享 CLI 纵向链路）。

每 CaseAttempt 独立 session/workspace：CLI 进程按 Case 生命周期启动与
终止（SupervisedProcess 有界监督）；事件经 RunService 持久 trace（带
source_seq/parser_version/coverage）；产物经 ArtifactStore 冻结；
Observation 沿用 M1 契约（exit=0 不等于质量通过；usage/cost 只取原生
回报，缺失保持 unknown）。
"""
from __future__ import annotations

import hashlib
import os
import sys
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from motte_contracts.evaluation import observation_evidence_hash
from motte_trace.redaction import redact_secrets as redact

from .pi_runtime import RuntimeCaseWorkspace

_TERMINATION_MAP = {
    "final": "final_answer",
    "error": "error",
    "insufficient": "invalid_state",
}


class CliRuntimeCaseExecutor:
    """一个 Case 的 CLI batch 执行器（claude-cli@1 / codex-cli@1）。"""

    def __init__(
        self,
        run: dict[str, Any],
        *,
        backend: str,
        service: Any = None,
        binary: str | None = None,
    ) -> None:
        if backend not in ("claude-cli", "codex-cli"):
            raise ValueError(f"unsupported cli runtime backend: {backend}")
        self.backend = backend
        self.run = run
        self.manifest = run.get("manifest") or {}
        self.profile = self.manifest.get("runtime_profile") or {}
        settings = self.profile.get("native_settings") or {}
        self.settings = settings
        self.binary = binary or settings.get("binary")
        self._service = service

    def bind_service(self, service: Any) -> None:
        self._service = service

    # ------------------------------------------------------------ 公共入口

    def invoke(self, case_id: str) -> dict[str, Any]:
        from motte_sdk.agent_tasks import selected_agent_cases_from_snapshot

        cases = selected_agent_cases_from_snapshot(self.run)
        case = next((item for item in cases if item["case_id"] == case_id), None)
        if case is None:
            raise KeyError(case_id)

        from motte_harness.supervisor import SupervisedLimits
        from motte_harness.process import minimal_env

        workspace = RuntimeCaseWorkspace(self.run["id"], case_id)
        workspace.materialize_fixture(case.get("fixture") or {})
        before = workspace.snapshot()

        session_id = f"{self.backend}-session-{uuid4().hex[:16]}"
        operation_id = f"{self.backend}-op-{uuid4().hex[:16]}"

        harness = self._harness()
        prompt = case["input"]
        argv = self._argv(harness, prompt, workspace)
        env_names = sorted((self.settings.get("env") or {}).keys())
        env = minimal_env(dict(self.settings.get("env") or {}))

        event_count = 0

        def on_stderr(line: str) -> None:
            nonlocal event_count
            service = self._service
            if service is None or not line.strip():
                return
            event_count += 1
            service.emit_run_event(
                self.run["id"], f"{self.backend}_stderr",
                {"case_id": case_id, "session_id": session_id,
                 "source_seq": event_count, "parser_version": self._parser_version(),
                 "coverage": "partial", "text": redact({"line": line})["line"][:2000]},
            )

        limits = SupervisedLimits(
            total_timeout=float(os.environ.get("MOTTE_CLI_TOTAL_TIMEOUT", "600")),
            idle_timeout=float(os.environ.get("MOTTE_CLI_IDLE_TIMEOUT", "120")),
        )

        started = monotonic()
        pending_error: BaseException | None = None
        parsed: dict[str, Any] | None = None
        process_view: dict[str, Any] = {}
        cleanup: dict[str, Any] | None = None
        try:
            harness_result = harness.run_batch(
                prompt, cwd=str(workspace.root), env=env, argv=argv,
                limits=limits, on_event=on_stderr,
            )
            process_view = dict(harness_result.get("process") or {})
            parsed = harness_result.get("parsed")
        except BaseException as error:  # noqa: BLE001 - 异常路径也保留证据与清理
            pending_error = error
        finally:
            try:
                observation = self._capture(
                    workspace, case, parsed, process_view, before,
                    session_id, operation_id,
                )
            except BaseException:  # noqa: BLE001 - 采集失败降级为空观测
                observation = {}
            try:
                cleanup = workspace.cleanup()
            except BaseException as error:  # noqa: BLE001
                cleanup = {"status": "failed", "residual": [], "error": str(error)}

        duration_ms = round((monotonic() - started) * 1000, 3)
        status = (parsed or {}).get("status") or "insufficient"
        envelope = {
            "agent": {
                "final_output": (parsed or {}).get("final_output"),
                "termination_reason": _TERMINATION_MAP.get(status, "error"),
                "termination_detail": (
                    (parsed or {}).get("reason")
                    or "; ".join((parsed or {}).get("turn_failures") or [])
                    or None
                ),
                "steps": (parsed or {}).get("num_turns") or len((parsed or {}).get("commands") or []),
                "tool_calls": len((parsed or {}).get("commands") or []),
                "duration_ms": duration_ms,
                "runtime": {
                    "backend": f"{self.backend}@1",
                    "session_id": session_id,
                    "operation_id": operation_id,
                    "parser_version": self._parser_version(),
                    "model_control": "runner-configured",
                    "observed_model": (parsed or {}).get("model"),
                    "config_hash": self.profile.get("config_hash"),
                    "argv_redacted": redact({"argv": argv})["argv"],
                    "env_names": env_names,
                    "process": process_view,
                },
            },
            "observation": observation,
            "events": [],
            "artifacts_captured": len(observation.get("artifact_refs") or []),
            "capture_errors": [],
            "cleanup": cleanup,
        }
        if pending_error is not None:
            if not getattr(pending_error, "quarantine", False):
                pending_error.evidence = {**envelope, "error": {
                    "class": type(pending_error).__name__,
                    "message": str(pending_error),
                }}
            raise pending_error
        return envelope

    # ------------------------------------------------------------ 组装

    def _harness(self):
        if self.backend == "claude-cli":
            from motte_harness.claude import ClaudeHarness

            return ClaudeHarness(binary=self.binary or "claude")
        from motte_harness.codex import CodexHarness

        return CodexHarness(binary=self.binary or "codex")

    def _parser_version(self) -> str:
        if self.backend == "claude-cli":
            from motte_harness.parsers.claude import PARSER_VERSION
        else:
            from motte_harness.parsers.codex import PARSER_VERSION
        return PARSER_VERSION

    def _argv(self, harness, prompt: str, workspace: RuntimeCaseWorkspace) -> list[str]:
        argv = self._raw_argv(harness, prompt, workspace)
        binary = self.binary or self.settings.get("binary")
        if binary and str(binary).lower().endswith(".py"):
            # 脚本形态的 pinned binary（测试/本地 fixture）：预置解释器，
            # 不经 shell。真实 claude/codex 二进制直接执行。
            return [sys.executable, *argv]
        return argv

    def _raw_argv(self, harness, prompt: str, workspace: RuntimeCaseWorkspace) -> list[str]:
        if self.backend == "claude-cli":
            settings_file = workspace.root / "motte-settings.json"
            settings_file.write_text("{}", encoding="utf-8")
            return harness.batch_argv(
                prompt,
                model=self.settings.get("model"),
                max_turns=self.settings.get("max_turns"),
                permission_mode=self.settings.get("permission_mode"),
                settings_file=settings_file,
            )
        overrides = [
            (str(key)[2:], str(value))
            for key, value in (self.settings.get("codex_config") or {}).items()
            if str(key).startswith("c_")
        ]
        return harness.batch_argv(
            prompt,
            model=self.settings.get("model"),
            config_overrides=overrides,
            sandbox=self.settings.get("sandbox"),
        )

    # ------------------------------------------------------------ Observation

    def _capture(
        self,
        workspace: RuntimeCaseWorkspace,
        case: dict[str, Any],
        parsed: dict[str, Any] | None,
        process_view: dict[str, Any],
        before: dict[str, Any],
        session_id: str,
        operation_id: str,
    ) -> dict[str, Any]:
        from motte_sdk.agent_backend import _guess_media_type, _read_nofollow
        from motte_storage.artifacts import ArtifactStore

        after = workspace.snapshot()
        artifacts: list[dict[str, Any]] = []
        errors: list[str] = []
        artifact_store = ArtifactStore(
            Path(os.environ.get("ARTIFACT_ROOT", "var/artifacts")),
        )
        before_files = set(before.get("files") or [])
        after_files = set(after.get("files") or [])
        for rel in sorted(after_files):
            artifact_id = f"{self.backend}/{self.run['id']}/{case['case_id']}/{rel}"
            try:
                data = _read_nofollow(workspace.root / rel)
                artifact_store.put_bytes(
                    artifact_id, data, kind="case-artifact",
                    media_type=_guess_media_type(rel),
                )
                artifacts.append({
                    "artifact_id": artifact_id, "path": rel,
                    "media_type": _guess_media_type(rel),
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "available": True, "truncated": False,
                })
            except Exception as error:  # noqa: BLE001
                errors.append(f"{rel}: {type(error).__name__}: {error}")
                artifacts.append({
                    "artifact_id": artifact_id, "path": rel, "available": False,
                })
        usage = (parsed or {}).get("usage") or {}
        reported_usage = {
            "reported": bool(usage.get("reported")),
            "total_tokens": (
                (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
                if usage.get("reported") else None
            ),
            "cost_total": (parsed or {}).get("cost_usd"),
        }
        coverage = (parsed or {}).get("coverage") or "partial"
        if errors:
            coverage = "partial"
        observation: dict[str, Any] = {
            "observation_id": f"obs-{uuid4().hex}",
            "run_id": self.run["id"],
            "case_id": case["case_id"],
            "attempt_id": None,
            "final_output": (parsed or {}).get("final_output"),
            "termination": {
                "reason": _TERMINATION_MAP.get(
                    (parsed or {}).get("status") or "insufficient", "error",
                ),
                "detail": (parsed or {}).get("reason"),
            },
            "event_refs": [],
            "artifact_refs": artifacts,
            "coverage": {
                "complete": coverage == "complete" and not errors,
                "events_captured": 0,
                "artifacts_captured": len([a for a in artifacts if a.get("available")]),
                "artifacts_expected": len(after_files),
                "missing": errors,
            },
            "usage": reported_usage,
            "tool_calls": [
                {
                    "call_id": f"cmd-{index}",
                    "tool_name": "command_execution",
                    "arguments": {"command": command.get("command")},
                    "status": (
                        "succeeded" if command.get("exit_code") == 0
                        else "failed" if command.get("exit_code") is not None
                        else "unknown"
                    ),
                    "step": index + 1,
                }
                for index, command in enumerate((parsed or {}).get("commands") or [])
            ],
            "workspace": {
                "before": sorted(before_files),
                "after": sorted(after_files),
                "complete": bool(before.get("complete", True) and after.get("complete", True)),
                "before_hashes": dict(before.get("hashes") or {}),
                "after_hashes": dict(after.get("hashes") or {}),
            },
            "processes": [],
        }
        observation["evidence_hash"] = observation_evidence_hash(observation)
        observation["recorded_at"] = datetime.now(UTC).isoformat()
        del session_id, operation_id
        return observation
