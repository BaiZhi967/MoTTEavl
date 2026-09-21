"""M5-T07 受控 executable Skill fixture（唯一消费者）。

一个 (run, case, attempt) 一个受控实例：入口是**已发布的固定 interpreter+argv**
（不经 shell、内联代码在契约层就被拒绝），cwd / env / 期限 / 清理都来自冻结
声明与沙箱策略，并复用既有受控进程机制（motte_harness.SupervisedProcess：
cwd/env/argv、总期限、interrupt → 宽限期 → 进程树终止、residual pid 上报），
不复制进程终止实现，也不新建调度器。

受控输入输出协议（本模块定义，写入 docs/operations/skills.md）：

* 受控输入按 `input_schema` 校验后写到执行 cwd 的 `inputs.json`；
* 入口把结果 JSON 打到 stdout，字段 `output` 必填，可选
  `tools_used` / `network` / `filesystem_write` 声明它用掉的权限；
* 输出不合 `output_schema`、越权声明（工具/网络/路径）或写到自己的受控根之外
  都是**失败**，绝不因为进程退出码是 0 就当作"已执行通过"；
* 只有**确认停止**才清理现场；无法确认时保留工作区，并且 close 不给
  "executed and passed" 的结论。

诚实边界：本模块没有容器/OS 级文件系统隔离能力，因此"是否真的没写别处"只能
证伪到可观测范围（受控根之外的 anchor 内新增文件、以及入口自己声明的写入）。
未在 `env_allowlist` 里的宿主变量不会进入子进程；网络能力平台侧恒为 none。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

#: 受控输入文件名（执行 cwd 下）。
INPUT_FILE_NAME = "inputs.json"
#: 实例所有权标记：清理前必须核验，避免删掉别人的现场。
OWNER_MARKER = ".motte-executable-owner.json"
SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class ExecutableFixtureError(RuntimeError):
    """受控 executable fixture 无法准备/执行；code 供结构化错误映射。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ExecutableInstance:
    instance_id: str
    skill_id: str
    version: str
    root: Path
    workspace: Path
    owner_token: str
    command: tuple[str, ...]
    env_allowlist: tuple[str, ...]
    effective_permissions: dict[str, Any] = field(default_factory=dict)
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    state: str = "prepared"
    outcome: "ExecutableOutcome | None" = None
    created: list[str] = field(default_factory=list)


@dataclass
class ExecutableOutcome:
    status: str
    exit_code: int | None
    output: Any = None
    passed: bool | None = None
    error_code: str | None = None
    detail: str | None = None
    duration_ms: int = 0
    stdout: str = ""
    stderr: str = ""
    residual_pids: list[int] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    escaped: list[str] = field(default_factory=list)
    stop_confirmed: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "output": self.output,
            "passed": self.passed,
            "error_code": self.error_code,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
            "residual_pids": list(self.residual_pids),
            "created": list(self.created),
            "escaped": list(self.escaped),
            "stop_confirmed": self.stop_confirmed,
        }


def _safe_component(value: Any, label: str) -> str:
    text = str(value)
    if SAFE_COMPONENT.fullmatch(text) is None or text in {".", ".."}:
        raise ExecutableFixtureError(
            "EXECUTABLE_SKILL_INSTANCE_INVALID",
            label + " must be a single safe path component: " + repr(value),
        )
    return text


def _relative_file(value: Any, label: str) -> str:
    text = str(value)
    if not text or Path(text).is_absolute():
        raise ExecutableFixtureError(
            "EXECUTABLE_SKILL_RESOURCE_INVALID",
            label + " must be a relative path: " + repr(value),
        )
    parts = Path(text).parts
    if ".." in parts:
        raise ExecutableFixtureError(
            "EXECUTABLE_SKILL_RESOURCE_INVALID",
            label + " escapes the workspace: " + repr(value),
        )
    return text


def _validate_json(schema: Mapping[str, Any], payload: Any, *, code: str, label: str) -> None:
    """按冻结 schema 本地校验；schema 本身不可用同样是失败（fail closed）。"""
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    try:
        validator = Draft202012Validator(dict(schema))
        errors = sorted(validator.iter_errors(payload), key=lambda item: list(item.path))
    except SchemaError as error:
        raise ExecutableFixtureError(
            code, label + " schema is not a usable JSON schema: " + str(error)
        ) from error
    if errors:
        raise ExecutableFixtureError(
            code,
            label + " does not satisfy its schema: " + "; ".join(
                error.message for error in errors[:3]
            ),
        )


class ExecutableSkillFixture:
    """受控 executable Skill 实例的 prepare / execute / close 生命周期。"""

    def __init__(
        self,
        *,
        anchor: str | Path,
        content_store: Any = None,
        base_env: Mapping[str, str] | None = None,
        clock: Any = None,
    ) -> None:
        self.anchor = Path(anchor)
        self.content_store = content_store
        self.base_env = dict(base_env or {})
        self._clock = clock or time.monotonic

    # ------------------------------------------------------------ prepare

    def prepare(
        self,
        skill: Mapping[str, Any],
        *,
        run_id: str,
        case_id: str,
        attempt_id: str,
        owner_token: str,
        inputs: Mapping[str, Any] | None = None,
        effective_permissions: Mapping[str, Any] | None = None,
    ) -> ExecutableInstance:
        from motte_skill.versions import SkillEntrypoint

        kind = str((skill or {}).get("kind") or "")
        if kind != "executable":
            raise ExecutableFixtureError(
                "EXECUTABLE_SKILL_ENTRYPOINT_REQUIRED",
                "only executable skills have a controlled entry; " + repr(kind) + " is "
                "validated statically and is never run as a program",
            )
        payload = skill.get("entrypoint")
        if not isinstance(payload, Mapping):
            raise ExecutableFixtureError(
                "EXECUTABLE_SKILL_ENTRYPOINT_REQUIRED",
                "executable skill " + str(skill.get("skill_id")) + " declares no entrypoint",
            )
        try:
            entrypoint = SkillEntrypoint.model_validate(dict(payload))
            command = entrypoint.validated_command()
        except Exception as error:  # noqa: BLE001 - 契约/沙箱拒绝都具名转换
            raise ExecutableFixtureError(
                "EXECUTABLE_SKILL_ENTRYPOINT_INVALID", str(error)
            ) from error
        if shutil.which(command[0]) is None:
            raise ExecutableFixtureError(
                "EXECUTABLE_SKILL_INTERPRETER_UNAVAILABLE",
                "interpreter " + repr(command[0]) + " is not available on this host",
            )
        components = [
            _safe_component(run_id, "run_id"),
            _safe_component(case_id, "case_id"),
            _safe_component(attempt_id, "attempt_id"),
        ]
        root = self.anchor.joinpath(*components)
        cwd = "" if entrypoint.cwd == "." else entrypoint.cwd
        workspace = root / "workspace" / cwd if cwd else root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        (root / OWNER_MARKER).write_text(
            json.dumps({"owner_token": owner_token, "instance": "/".join(components)}),
            encoding="utf-8",
        )
        for resource in (skill.get("resource_manifest") or ()):
            entry = resource if isinstance(resource, Mapping) else {}
            relative = _relative_file(entry.get("path"), "resource path")
            reference = str(entry.get("sha256") or "")
            data = self.content_store.get(reference) if self.content_store is not None else None
            if data is None:
                raise ExecutableFixtureError(
                    "EXECUTABLE_SKILL_RESOURCE_MISSING",
                    "resource bytes are missing from the content store: " + reference,
                )
            if (
                "sha256:" + hashlib.sha256(bytes(data)).hexdigest() != reference
                or len(data) != int(entry.get("size_bytes") or -1)
            ):
                raise ExecutableFixtureError(
                    "EXECUTABLE_SKILL_RESOURCE_MISMATCH",
                    "resource bytes drifted for " + relative,
                )
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bytes(data))
        input_schema = dict(skill.get("input_schema") or {})
        payload_inputs = dict(inputs or {})
        if input_schema:
            _validate_json(
                input_schema, payload_inputs,
                code="EXECUTABLE_SKILL_INPUT_INVALID", label="controlled input",
            )
        (workspace / INPUT_FILE_NAME).write_text(
            json.dumps(payload_inputs, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        return ExecutableInstance(
            instance_id=uuid4().hex,
            skill_id=str(skill.get("skill_id") or ""),
            version=str(skill.get("version") or ""),
            root=root,
            workspace=workspace,
            owner_token=owner_token,
            command=tuple(command),
            env_allowlist=tuple(entrypoint.env_allowlist),
            effective_permissions=dict(effective_permissions or {}),
            input_schema=input_schema,
            output_schema=dict(skill.get("output_schema") or {}),
        )

    # ------------------------------------------------------------ execute

    def execute(
        self,
        instance: ExecutableInstance,
        *,
        deadline: float | None = None,
        timeout_seconds: float | None = None,
    ) -> ExecutableOutcome:
        """在受控沙箱里执行一次入口；返回**实际发生**的结局。"""
        from motte_harness.supervisor import SupervisedLimits, SupervisedProcess

        if instance.state != "prepared":
            raise ExecutableFixtureError(
                "EXECUTABLE_SKILL_STATE", "instance is not prepared: " + instance.state
            )
        before = self._tree(self.anchor)
        budget = timeout_seconds if timeout_seconds is not None else 60.0
        if deadline is not None:
            budget = min(budget, max(0.0, float(deadline) - self._clock()))
        if budget <= 0:
            outcome = ExecutableOutcome(
                status="timeout", exit_code=None, passed=False,
                error_code="EXECUTABLE_SKILL_TIMEOUT",
                detail="deadline was already exhausted before the entry started",
                stop_confirmed=True, created=sorted(self._tree(instance.root) - before),
            )
            instance.outcome = outcome
            instance.state = "timeout"
            return outcome
        env = {
            name: self.base_env[name]
            for name in instance.env_allowlist
            if name in self.base_env
        }
        process = SupervisedProcess(
            list(instance.command),
            cwd=str(instance.workspace),
            env=_minimal_env(env),
            limits=SupervisedLimits(
                total_timeout=float(budget), idle_timeout=float(budget),
                interrupt_grace=2.0, residual_grace=2.0,
            ),
            name="executable-skill",
        )
        process.start()
        raw = process.wait()
        created = sorted(self._tree(instance.root) - before)
        escaped = sorted(self._escaped(before, instance.root))
        outcome = self._judge(instance, raw, created, escaped)
        instance.outcome = outcome
        instance.state = outcome.status
        instance.created = created
        return outcome

    def _judge(self, instance: ExecutableInstance, raw: Any, created: list[str],
               escaped: list[str]) -> ExecutableOutcome:
        residual = list(getattr(raw, "residual_pids", []) or [])
        confirmed = not residual
        base: dict[str, Any] = {
            "exit_code": getattr(raw, "exit_code", None),
            "duration_ms": int(getattr(raw, "duration_ms", 0) or 0),
            "stdout": str(getattr(raw, "stdout", ""))[:20000],
            "stderr": str(getattr(raw, "stderr", ""))[:20000],
            "residual_pids": residual,
            "created": created,
            "escaped": escaped,
            "stop_confirmed": confirmed,
        }
        status = str(getattr(raw, "status", "failed"))
        if status in {"timeout", "idle_timeout"}:
            return ExecutableOutcome(
                status="timeout", passed=False, error_code="EXECUTABLE_SKILL_TIMEOUT",
                detail="deadline exceeded: " + str(getattr(raw, "detail", "") or status), **base,
            )
        if status == "interrupted":
            return ExecutableOutcome(
                status="interrupted", passed=False, error_code="EXECUTABLE_SKILL_INTERRUPTED",
                detail="entry was interrupted; no success verdict is issued", **base,
            )
        if escaped:
            return ExecutableOutcome(
                status="failed", passed=False, error_code="EXECUTABLE_SKILL_WORKSPACE_ESCAPE",
                detail="the entry wrote outside its owned root: " + ", ".join(escaped), **base,
            )
        if status != "exited" or base["exit_code"] not in (0, None):
            return ExecutableOutcome(
                status="failed", passed=False, error_code="EXECUTABLE_SKILL_EXIT_CODE",
                detail="entry exited with " + repr(base["exit_code"]) + " (" + status + ")", **base,
            )
        stdout = base["stdout"].strip()
        if not stdout:
            return ExecutableOutcome(
                status="failed", passed=False, error_code="EXECUTABLE_SKILL_OUTPUT_INVALID",
                detail="entry produced no JSON result on stdout", **base,
            )
        try:
            payload = json.loads(stdout.splitlines()[-1])
        except ValueError as error:
            return ExecutableOutcome(
                status="failed", passed=False, error_code="EXECUTABLE_SKILL_OUTPUT_INVALID",
                detail="entry output is not JSON: " + str(error), **base,
            )
        if instance.output_schema:
            try:
                _validate_json(
                    instance.output_schema, payload,
                    code="EXECUTABLE_SKILL_OUTPUT_INVALID", label="controlled output",
                )
            except ExecutableFixtureError as error:
                return ExecutableOutcome(
                    status="failed", passed=False, error_code=error.code,
                    detail=str(error), **base,
                )
        violation = self._permission_violation(instance, payload)
        if violation is not None:
            return ExecutableOutcome(
                status="failed", passed=False,
                error_code="EXECUTABLE_SKILL_PERMISSION_VIOLATION", detail=violation, **base,
            )
        if not confirmed:
            return ExecutableOutcome(
                status="failed", passed=None,
                error_code="EXECUTABLE_SKILL_STOP_UNCONFIRMED",
                detail="residual processes remained after the entry exited: " + repr(residual),
                **base,
            )
        return ExecutableOutcome(
            status="exited", passed=True,
            output=payload.get("output", payload), **base,
        )

    def _permission_violation(
        self, instance: ExecutableInstance, payload: Any,
    ) -> str | None:
        """越权返回：声明的工具/网络/写入必须落在冻结有效权限之内。"""
        if not isinstance(payload, Mapping):
            return None
        permissions = instance.effective_permissions or {}
        granted = {str(item) for item in (permissions.get("tools") or ())}
        used = {str(item) for item in (payload.get("tools_used") or ())}
        extra = sorted(used - granted)
        if extra:
            return "the entry reports tools outside its effective permissions: " + ", ".join(extra)
        if payload.get("network"):
            return "the entry reports network access but the effective network policy is none"
        allowed_write = {
            str(item) for item in (permissions.get("filesystem_write") or ())
        }
        for item in payload.get("filesystem_write") or ():
            text = str(item)
            if Path(text).is_absolute() or ".." in Path(text).parts:
                return "the entry reports a write outside the workspace: " + repr(text)
            if allowed_write and text not in allowed_write:
                return "the entry reports a write outside its effective permissions: " + repr(text)
            target = (instance.workspace / text).resolve()
            if instance.root.resolve() not in target.parents:
                return "the entry reports a write outside its owned root: " + repr(text)
        return None

    # ------------------------------------------------------------ close

    def close(self, instance: ExecutableInstance, *, interrupted: bool = False) -> dict[str, Any]:
        """清理或保留现场；**绝不在结论不确定时报告已执行通过**。"""
        outcome = instance.outcome
        confirmed = (
            not interrupted
            and (outcome is None or (outcome.stop_confirmed and not outcome.residual_pids))
        )
        report: dict[str, Any] = {
            "instance_id": instance.instance_id,
            "skill": instance.skill_id + "@" + instance.version,
            "owner_token": instance.owner_token,
            "workspace": str(instance.root),
            "executed": outcome is not None,
            "outcome": outcome.status if outcome is not None else None,
            "residual_pids": list(getattr(outcome, "residual_pids", []) or []),
        }
        if not confirmed:
            report["status"] = "retained"
            report["passed"] = None
            report["retained_reason"] = (
                "interrupted" if interrupted else "stop could not be confirmed"
            )
            return report
        self._assert_owner(instance)
        self._remove_root(instance.root)
        report["status"] = "cleaned"
        report["passed"] = outcome.passed if outcome is not None else None
        return report

    # ------------------------------------------------------------ 内部

    def _assert_owner(self, instance: ExecutableInstance) -> None:
        marker = instance.root / OWNER_MARKER
        if not marker.is_file():
            raise ExecutableFixtureError(
                "EXECUTABLE_SKILL_OWNERSHIP_UNKNOWN",
                "instance root has no ownership marker: " + str(instance.root),
            )
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if payload.get("owner_token") != instance.owner_token:
            raise ExecutableFixtureError(
                "EXECUTABLE_SKILL_OWNERSHIP_MISMATCH",
                "instance root is owned by another token",
            )

    @staticmethod
    def _remove_root(root: Path) -> None:
        if root.is_dir():
            shutil.rmtree(root, ignore_errors=True)

    @staticmethod
    def _tree(root: Path) -> set[str]:
        if not root.exists():
            return set()
        return {
            str(path.relative_to(root)).replace(os.sep, "/")
            for path in root.rglob("*") if path.is_file()
        }

    def _escaped(self, before: set[str], owned: Path) -> set[str]:
        """anchor 内、受控根之外的新增文件（可观测的越界副作用）。"""
        prefix = str(owned.relative_to(self.anchor)).replace(os.sep, "/")
        escaped: set[str] = set()
        for relative in self._tree(self.anchor) - before:
            if relative == prefix or relative.startswith(prefix + "/"):
                continue
            escaped.add(relative)
        return escaped


def _minimal_env(extra: Mapping[str, str]) -> dict[str, str]:
    from motte_harness.process import minimal_env

    return minimal_env(dict(extra))


__all__ = [
    "INPUT_FILE_NAME",
    "OWNER_MARKER",
    "ExecutableFixtureError",
    "ExecutableInstance",
    "ExecutableOutcome",
    "ExecutableSkillFixture",
]
