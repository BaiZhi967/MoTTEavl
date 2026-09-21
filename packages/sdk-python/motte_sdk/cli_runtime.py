"""M4-T06/T07：Claude / Codex batch 的平台执行器（共享 CLI 纵向链路）。

每 CaseAttempt 独立 session/workspace：CLI 进程按 Case 生命周期启动与
终止（SupervisedProcess 有界监督）；事件经 RunService 持久 trace（带
source_seq/parser_version/coverage）；产物与**原始 stdout/stderr** 经
ArtifactStore 冻结（M4 review R14）；Observation 沿用 M1 契约（exit=0
不等于质量通过；usage/cost 只取原生回报，缺失保持 unknown）。

M4 review 落实：

- R04：RunService 取消在运行期间轮询探测，命中即打断受控进程并等
  清理结束后再冻结证据（取消不是只改平台状态）。
- R06：终态由原生 terminal、process outcome（exit code / status /
  截断）合成；非零退出不得产生 final_answer。
- R07：profile 声明编译为有效配置——预算 total_timeout 进监督上限、
  pinned workspace 生效；无法兑现的约束（credential_refs）具名拒绝。
- R13：工具轨迹完整度按 parser 声明如实进入 coverage；claude 单对象
  结果没有工具轨迹，coverage 不冒充 complete。
- R15：spawn 前持久 session 记录（start_token），spawn 后记 PID/身份，
  终态落盘；Worker 崩溃后可用 recover_runtime_sessions 只读观察。
- R18：spawn 前按实际二进制跑零费用 ``--version`` 门，与快照 pinned
  upstream 不符即 fail closed。
"""
from __future__ import annotations

import hashlib
import os
import sys
import queue
import tempfile
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from motte_contracts.evaluation import EvidenceRef, observation_evidence_hash
from motte_trace.redaction import redact_secrets as redact

from .pi_runtime import RuntimeCaseWorkspace

_TERMINATION_MAP = {
    "final": "final_answer",
    "error": "error",
    "cancelled": "cancelled",
    "timeout": "wall_time",
    "insufficient": "invalid_state",
}
_PROCESS_TERMINATION_MAP = {
    "interrupted": "cancelled",
    "timeout": "wall_time",
    "invalid_utf8": "error",
    "line_limit": "error",
    "byte_limit": "error",
}
SESSION_ANCHOR = Path("var/runtime-sessions")


def _backend_error(code: str, message: str):
    from .execution_backends import ExecutionBackendError

    return ExecutionBackendError(code, message)


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
        self.snapshot = self.manifest.get("runtime_snapshot") or {}
        settings = self.profile.get("native_settings") or {}
        self.settings = settings
        self.binary = binary or settings.get("binary")
        self._service = service
        self._attempt_id: str | None = None
        self._credential_values: tuple[str, ...] = ()

    def _clean(self, value: Any) -> Any:
        if isinstance(value, str):
            for secret in self._credential_values:
                value = value.replace(secret, '[REDACTED-CREDENTIAL]')
            return value
        if isinstance(value, dict):
            return {self._clean(key): self._clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._clean(item) for item in value]
        return value

    def _artifact_bytes(self, data: bytes) -> bytes:
        for secret in self._credential_values:
            data = data.replace(secret.encode('utf-8'), b'[REDACTED-CREDENTIAL]')
        return data

    def bind_service(self, service: Any) -> None:
        self._service = service

    def run_build_gate(self) -> None:
        """dispatcher build 阶段的公共预检（每 Run 一次，具名失败）。

        预检（R07）+ 启动前版本门（R18）在这里执行：ExecutionBackendError
        由 dispatcher 映射为 unsupported + 具名 code，不会进入逐题执行。
        """
        effective = self._preflight()
        self._enforce_binary_version(effective["expected_version"])

    # ------------------------------------------------------------ 预检（R07）

    def _preflight(self) -> dict[str, Any]:
        from .native_config import validate_native_policy

        validate_native_policy(self.backend, self.profile.get('credential_refs') or [])
        from motte_contracts.runtime import validate_runtime_budgets

        try:
            budgets = validate_runtime_budgets(
                f"{self.backend}@1", dict(self.profile.get("budgets") or {}),
            )
            for name, default in (("total_timeout", "600"), ("idle_timeout", "120")):
                if name not in budgets:
                    budgets[name] = float(os.environ.get(f"MOTTE_CLI_{name.upper()}", default))
            budgets = validate_runtime_budgets(f"{self.backend}@1", budgets)
            total_timeout = float(budgets["total_timeout"])
            idle_timeout = float(budgets["idle_timeout"])
        except (TypeError, ValueError, OverflowError) as error:
            raise _backend_error("RUNTIME_BUDGET_INVALID", str(error)) from error
        upstream = self.snapshot.get("upstream_version") or ""
        expected_version = upstream.rpartition("@")[2] if "@" in upstream else ""
        return {
            "total_timeout": total_timeout,
            "idle_timeout": idle_timeout,
            "workspace": dict(self.profile.get("workspace") or {}),
            "expected_version": expected_version,
        }

    # ------------------------------------------------------------ 版本门（R18）

    def _enforce_binary_version(self, expected_version: str) -> dict[str, Any]:
        """spawn 前零费用 ``--version`` 门：漂移/无法判定都 fail closed。"""
        if not expected_version:
            return {"checked": False}
        from motte_harness.compatibility import version_from_output
        from motte_harness.process import minimal_env
        from motte_harness.supervisor import SupervisedLimits, SupervisedProcess

        binary = self.binary or ("claude" if self.backend == "claude-cli" else "codex")
        probe_argv = [str(binary)]
        if str(binary).lower().endswith(".py"):
            probe_argv = [sys.executable, str(binary)]
        probe_argv = [*probe_argv, "--version"]
        try:
            with tempfile.TemporaryDirectory(prefix='motte-version-') as probe_home:
                process = SupervisedProcess(probe_argv, cwd=probe_home,
                    env=minimal_env({'HOME': probe_home, 'USERPROFILE': probe_home,
                                     'CODEX_HOME': probe_home, 'CLAUDE_CONFIG_DIR': probe_home}),
                    limits=SupervisedLimits(total_timeout=15, idle_timeout=15,
                                           max_line_bytes=4096, max_total_bytes=16384))
                process.start()
                completed = process.wait()
                if (completed.status != 'exited' or completed.exit_code != 0
                        or completed.truncated or completed.residual_pids):
                    raise OSError('version probe did not exit cleanly')
        except (OSError, RuntimeError) as error:
            raise _backend_error(
                "RUNTIME_BINARY_VERSION_UNKNOWN",
                f"{self.backend} binary {binary!r} --version probe failed: {error}",
            ) from error
        output = (completed.stdout or "") + (completed.stderr or "")
        observed = version_from_output(output)
        if observed is None:
            raise _backend_error(
                "RUNTIME_BINARY_VERSION_UNKNOWN",
                f"{self.backend} binary {binary!r} --version output could not be "
                f"parsed: {output.strip()[:120]!r}",
            )
        if observed != expected_version:
            raise _backend_error(
                "RUNTIME_VERSION_DRIFT",
                f"{self.backend} binary version drift: installed {observed}, "
                f"pinned {expected_version} (fail closed; align the binary or "
                "publish an updated runtime version)",
            )
        return {"checked": True, "observed": observed}

    # ------------------------------------------------------------ 公共入口

    def invoke(self, case_id: str) -> dict[str, Any]:
        from motte_sdk.agent_tasks import selected_agent_cases_from_snapshot

        effective = self._preflight()

        cases = selected_agent_cases_from_snapshot(self.run)
        case = next((item for item in cases if item["case_id"] == case_id), None)
        if case is None:
            raise KeyError(case_id)

        from motte_harness.supervisor import SupervisedLimits
        from .native_config import prepare_native_environment
        from motte_harness.session import (
            SESSION_STORE_ROOT,
            mark_spawned,
            mark_terminal,
            new_session_record,
            persist_session,
            session_path,
        )

        workspace = self._workspace_for(case_id, effective["workspace"])
        workspace.materialize_fixture(case.get("fixture") or {})
        before = workspace.snapshot()

        session_id = f"{self.backend}-session-{uuid4().hex[:16]}"
        operation_id = f"{self.backend}-op-{uuid4().hex[:16]}"

        harness = self._harness()
        prompt = case["input"]
        argv = self._argv(harness, prompt, workspace)
        native = prepare_native_environment(
            self.backend, self.profile.get('credential_refs') or [],
            workspace=workspace.root, binary=self.binary or harness.binary, settings=self.settings,
        )
        env = native.env
        self._credential_values = tuple(env[ref] for ref in self.profile.get('credential_refs') or [])
        env_names = sorted(env)

        event_count = 0
        stderr_lines: queue.SimpleQueue[str] = queue.SimpleQueue()

        def on_stderr(line: str) -> None:
            # Reader callbacks never touch durable service state. Drain the queue
            # on the invoking thread after wait freezes callback admission.
            stderr_lines.put(line)

        def emit_stderr() -> None:
            nonlocal event_count
            while not stderr_lines.empty():
                line = self._clean(stderr_lines.get_nowait())
                service = self._service
                if service is None or not line.strip():
                    continue
                event_count += 1
                service.emit_run_event(
                    self.run["id"], f"{self.backend}_stderr",
                    {"case_id": case_id, "session_id": session_id,
                     "source_seq": event_count, "parser_version": self._parser_version(),
                     "coverage": "partial", "text": redact({"line": line})["line"][:2000]},
                )

        def cancel_check() -> str | None:
            service = self._service
            if service is None:
                return None
            try:
                current = service.store.runs.get(self.run["id"])
            except Exception:  # noqa: BLE001 - 探测失败按未取消处理
                return None
            if current and current.get("cancellation"):
                reason = (current.get("cancellation") or {}).get("reason") \
                    if isinstance(current.get("cancellation"), dict) else None
                return str(reason or "run-cancelled")
            return None

        limits = SupervisedLimits(
            total_timeout=effective["total_timeout"],
            idle_timeout=effective["idle_timeout"],
        )

        # Bind the real dispatched CaseAttempt; never invent an identity when
        # running under the production service. Standalone harness use is explicit.
        self._attempt_id = None
        if self._service is not None:
            attempts = [
                item for item in self._service.store.attempts.list_open(self.run["id"])
                if item.get("case_id") == case_id and item.get("status") == "dispatching"
            ]
            if len(attempts) != 1:
                workspace.cleanup()
                raise _backend_error("RUNTIME_ATTEMPT_UNRESOLVED", "expected one dispatched CaseAttempt")
            self._attempt_id = attempts[0]["id"]
        session_record = new_session_record(
            run_id=self.run["id"], case_id=case_id,
            attempt_id=self._attempt_id,
            operation_id=operation_id, backend=self.backend, argv=argv,
            workspace=str(workspace.root), config_hash=self.profile.get("config_hash"),
        )
        session_record["session_id"] = session_id
        session_record['config_snapshot'] = native.evidence
        record_file = session_path(
            os.environ.get("MOTTE_RUNTIME_SESSION_ROOT", str(SESSION_STORE_ROOT)),
            self.run["id"], case_id, session_id,
        )
        session_record = persist_session(record_file, session_record)

        def on_spawned(process: Any) -> None:
            nonlocal session_record
            if process.pid is not None:
                session_record = mark_spawned(session_record, process.pid, path=record_file)

        started = monotonic()
        pending_error: BaseException | None = None
        parsed: dict[str, Any] | None = None
        process_view: dict[str, Any] = {}
        raw_stdout = ""
        raw_stderr = ""
        cleanup: dict[str, Any] | None = None
        evidence_refs: list[EvidenceRef] = []
        try:
            harness_result = harness.run_batch(
                prompt, cwd=str(workspace.root), env=env, argv=argv,
                limits=limits, on_event=on_stderr,
                cancel_check=cancel_check, on_spawned=on_spawned,
            )
            process_view = dict(harness_result.get("process") or {})
            raw_stdout = harness_result.get("stdout") or ""
            raw_stderr = harness_result.get("stderr") or ""
            parsed = self._clean(harness_result.get("parsed"))
            emit_stderr()
        except BaseException as error:  # noqa: BLE001 - 异常路径也保留证据与清理
            pending_error = error
        finally:
            # R14：原始输出在 finally 冻结——成功与失败路径都保留现场。
            try:
                parsed, evidence_refs = self._freeze_raw_evidence(
                    parsed, raw_stdout, raw_stderr, case_id, evidence_refs,
                )
            except BaseException:  # noqa: BLE001
                pass
            unconfirmed = (
                "callback_unconfirmed" in str(process_view.get("detail") or "")
                or bool(process_view.get("residual_pids"))
            )
            if unconfirmed:
                native.retain()
                observation = {}
                cleanup = {"status": "unconfirmed", "retained_workspace": str(workspace.root),
                           "residual_pids": process_view.get("residual_pids") or []}
                pending_error = _backend_error(
                    "RUNTIME_STOP_UNCONFIRMED", "runtime cleanup unconfirmed; retained workspace requires review",
                )
                pending_error.quarantine = True
                # Keep nonterminal session available to the recovery scanner.
                session_record = persist_session(record_file, {
                    **session_record, "revision": session_record["revision"] + 1,
                    "cleanup": cleanup,
                })
            else:
                try:
                    observation = self._capture(
                        workspace, case, parsed, process_view, before,
                        session_id, operation_id, evidence_refs,
                    )
                except BaseException:  # noqa: BLE001 - capture failure cannot claim completeness
                    observation = {}
                try:
                    cleanup = workspace.cleanup()
                except BaseException as error:  # noqa: BLE001
                    cleanup = {"status": "failed", "residual": [], "error": str(error)}
                terminal_status = self._combined_status(parsed, process_view)[0]
                try:
                    session_record = mark_terminal(
                        session_record, terminal_status, path=record_file, cleanup=cleanup,
                    )
                except Exception:  # noqa: BLE001 - recovery scanner retains nonterminal record
                    pass
                native.close()

        duration_ms = round((monotonic() - started) * 1000, 3)
        status, termination_detail = self._combined_status(parsed, process_view)
        envelope = {
            "agent": {
                "final_output": (parsed or {}).get("final_output"),
                "termination_reason": _TERMINATION_MAP.get(status, "error"),
                "termination_detail": termination_detail,
                "steps": (parsed or {}).get("num_turns") or len((parsed or {}).get("commands") or []),
                "tool_calls": len((parsed or {}).get("tool_calls") or []),
                "duration_ms": duration_ms,
                "runtime": {
                    "backend": f"{self.backend}@1",
                    "session_id": session_id,
                    "operation_id": operation_id,
                    "parser_version": self._parser_version(),
                    "model_control": "runner-configured",
                    "observed_model": (parsed or {}).get("model"),
                    "config_hash": self.profile.get("config_hash"),
                    "config_snapshot": native.evidence,
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

    def _workspace_for(self, case_id: str, workspace_decl: dict[str, Any]):
        if workspace_decl.get("source") == "pinned-path":
            root = workspace_decl.get("root")
            if not isinstance(root, str) or not root.strip():
                raise _backend_error(
                    "RUNTIME_PROFILE_INVALID",
                    "pinned-path workspace requires a non-empty root",
                )
            anchor = Path(root)
            try:
                anchor.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise _backend_error(
                    "RUNTIME_WORKSPACE_INVALID",
                    f"pinned workspace root is not usable: {error}",
                ) from error
            return RuntimeCaseWorkspace(self.run["id"], case_id, anchor=anchor)
        return RuntimeCaseWorkspace(self.run["id"], case_id)

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
            return [*harness.batch_argv(
                prompt,
                model=self.settings.get("model"),
                max_turns=self.settings.get("max_turns"),
                permission_mode=self.settings.get("permission_mode"),
                settings_file=settings_file,
            ), '--bare', '--setting-sources', '', '--strict-mcp-config', '--disable-slash-commands']
        overrides = [
            (str(key)[2:], str(value))
            for key, value in (self.settings.get("codex_config") or {}).items()
            if str(key).startswith("c_")
        ]
        return [*harness.batch_argv(
            prompt,
            model=self.settings.get("model"),
            config_overrides=overrides,
            sandbox=self.settings.get("sandbox"),
        ), '--ignore-user-config', '--ignore-rules', '--ephemeral']

    # ------------------------------------------------------------ 证据冻结（R14）

    def _freeze_raw_evidence(
        self,
        parsed: dict[str, Any] | None,
        raw_stdout: str,
        raw_stderr: str,
        case_id: str,
        evidence_refs: list[EvidenceRef],
    ) -> tuple[dict[str, Any] | None, list[EvidenceRef]]:
        """原始 stdout/stderr 冻结为证据产物；parsed 获得 raw_ref 指针。"""
        if not raw_stdout and not raw_stderr:
            return parsed, evidence_refs
        from motte_storage.artifacts import ArtifactStore

        store = ArtifactStore(Path(os.environ.get("ARTIFACT_ROOT", "var/artifacts")))
        refs = list(evidence_refs)
        if raw_stdout:
            payload = redact({"stdout": self._clean(raw_stdout)})["stdout"]
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            artifact_id = f"{self.backend}/{self.run['id']}/{case_id}/evidence/stdout/{digest}"
            store.put_bytes(
                artifact_id, payload.encode("utf-8"),
                kind="harness-raw", media_type="text/plain",
            )
            refs.append(EvidenceRef(
                kind="artifact", run_id=self.run["id"], locator=artifact_id,
            ))
            if parsed is not None and not parsed.get("raw_ref"):
                parsed = {**parsed, "raw_ref": artifact_id}
        if raw_stderr:
            payload = redact({"stderr": self._clean(raw_stderr)})["stderr"]
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            artifact_id = f"{self.backend}/{self.run['id']}/{case_id}/evidence/stderr/{digest}"
            store.put_bytes(
                artifact_id, payload.encode("utf-8"),
                kind="harness-raw", media_type="text/plain",
            )
            refs.append(EvidenceRef(
                kind="artifact", run_id=self.run["id"], locator=artifact_id,
            ))
        return parsed, refs

    # ------------------------------------------------------------ 终态合成（R06）

    @staticmethod
    def _combined_status(
        parsed: dict[str, Any] | None, process_view: dict[str, Any],
    ) -> tuple[str, str | None]:
        """原生 terminal × process outcome 的合成终态。

        成功文本不能覆盖执行错误：非零退出、非 exited 状态、截断都让
        终态离开 final（M4 review R06）。
        """
        parsed_status = (parsed or {}).get("status") or "insufficient"
        process_status = process_view.get("status") or "exited"
        exit_code = process_view.get("exit_code")
        detail_parts: list[str] = []
        if process_status != "exited":
            detail_parts.append(f"process status: {process_status}")
            mapped = _PROCESS_TERMINATION_MAP.get(process_status)
            if mapped == "cancelled":
                return "cancelled", "; ".join(detail_parts) or None
            if process_status == "timeout":
                return "timeout", "; ".join(detail_parts) or None
            # invalid_utf8 / line_limit / byte_limit：证据不完整
            return "insufficient", "; ".join(detail_parts) or None
        if exit_code is not None and exit_code != 0:
            detail_parts.append(f"process exited with code {exit_code}")
            return "error", "; ".join(detail_parts)
        if process_view.get("truncated"):
            detail_parts.append("process output truncated")
            return parsed_status if parsed_status == "error" else "insufficient", \
                "; ".join(detail_parts)
        if parsed_status == "error":
            detail_parts.append(
                str((parsed or {}).get("reason")
                    or "; ".join((parsed or {}).get("turn_failures") or [])
                    or "parser reported error")
            )
        return parsed_status, "; ".join(detail_parts) or None

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
        evidence_refs: list[EvidenceRef] | None = None,
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
            logical_path = self._clean(rel)
            artifact_id = f"{self.backend}/{self.run['id']}/{case['case_id']}/files/{logical_path}"
            try:
                data = _read_nofollow(workspace.root / rel)
                original = data
                data = self._artifact_bytes(data)
                artifact_id = f"{artifact_id}/{hashlib.sha256(data).hexdigest()}"
                artifact_store.put_bytes(
                    artifact_id, data, kind="case-artifact",
                    media_type=_guess_media_type(rel),
                )
                artifacts.append({
                    "artifact_id": artifact_id, "path": logical_path,
                    "media_type": _guess_media_type(rel),
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "available": True, "truncated": False,
                    "redacted": data != original or logical_path != rel,
                })
            except Exception as error:  # noqa: BLE001
                errors.append(f"{rel}: {type(error).__name__}: {error}")
                artifacts.append({
                    "artifact_id": artifact_id, "path": logical_path, "available": False,
                })
        usage = (parsed or {}).get("usage") or {}
        reported_usage = {
            "reported": bool(usage.get("reported")),
            "total_tokens": (
                usage["input_tokens"] + usage["output_tokens"]
                if usage.get("reported") and usage.get("input_tokens") is not None
                and usage.get("output_tokens") is not None else None
            ),
            "cost_total": (parsed or {}).get("cost_usd"),
        }
        # R13：coverage 只在工具轨迹完整 + 原生覆盖完整 + 进程正常退出时
        # 才声称 complete；claude 单对象结果没有工具轨迹 → 永不 complete。
        trajectory = (parsed or {}).get("tool_trajectory") or "absent"
        coverage = (parsed or {}).get("coverage") or "partial"
        process_ok = (
            (process_view.get("status") or "exited") == "exited"
            and not process_view.get("truncated")
        )
        missing = list(errors)
        if trajectory != "complete":
            missing.append(f"tool_trajectory:{trajectory}")
        if not process_ok:
            missing.append(
                f"process:{process_view.get('status')}"
                f"{'/truncated' if process_view.get('truncated') else ''}"
            )
        observation: dict[str, Any] = {
            "observation_id": f"obs-{uuid4().hex}",
            "run_id": self.run["id"],
            "case_id": case["case_id"],
            "attempt_id": self._attempt_id,
            "final_output": (parsed or {}).get("final_output"),
            "termination": {
                "reason": _TERMINATION_MAP.get(
                    self._combined_status(parsed, process_view)[0], "error",
                ),
                "detail": self._combined_status(parsed, process_view)[1],
            },
            "event_refs": [ref.model_dump() for ref in (evidence_refs or [])],
            "artifact_refs": artifacts,
            "coverage": {
                "complete": (
                    trajectory == "complete"
                    and coverage == "complete"
                    and process_ok
                    and not errors
                ),
                "events_captured": 0,
                "artifacts_captured": len([a for a in artifacts if a.get("available")]),
                "artifacts_expected": len(after_files),
                "missing": missing,
            },
            "usage": reported_usage,
            "tool_calls": [
                {key: value for key, value in call.items() if key != "native_item"}
                for call in (parsed or {}).get("tool_calls", [])
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
        observation = self._clean(observation)
        observation["evidence_hash"] = observation_evidence_hash(observation)
        observation["recorded_at"] = datetime.now(UTC).isoformat()
        del session_id, operation_id
        return observation
