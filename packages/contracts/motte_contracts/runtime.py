"""M4 Runtime 契约：外部 runtime 的定义、版本档案与配置校验。

RuntimeDefinition/Version 固定 kind、transport、上游版本与能力声明；
RuntimeProfile 是 Run 级执行配置（原生设置、工作区来源、工具/网络/审批
政策、预算与凭据引用）。声明不等于授权：能力字段只描述可观察证据，
工具权限仍取资源、Run policy 与 runtime 可强制能力的交集。
"""
from __future__ import annotations

from typing import Any

import re
import math

from pydantic import Field, field_validator, model_validator

from .messages import Contract

# 凭据引用只能是凭据档案名（safe identifier），不是秘密原文；
# 常见密钥前缀（sk-/AKIA/ghp_/JWT …）在契约层拒绝（M4 规则 6：无秘密原文）。
_CREDENTIAL_REF_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SECRET_VALUE_PREFIXES = (
    "sk-", "pk-", "rk-", "gsk-", "ghp_", "gho_", "ghu_", "xoxb-", "xoxp-",
    "AKIA", "AIza", "Bearer ", "eyJ",
)

RUNTIME_KINDS = ("pi-bridge", "claude-cli", "codex-cli", "codex-app-server")
RUNTIME_TRANSPORTS = (
    "bridge-stdio-jsonl",
    "cli-batch-json",
    "cli-exec-jsonl",
    "app-server-jsonrpc",
)
MODEL_CONTROL_MODES = (
    "platform-controlled",
    "runner-configured",
    "externally-managed",
)
RUNTIME_SUPPORTED_MODES = ("batch", "interactive")
TOOL_ENFORCEMENT_MODES = (
    "bridge-sandbox",
    "process-boundary",
    "not-enforced",
)
NETWORK_POLICIES = ("denied", "allowed")
APPROVAL_POLICIES = ("deny-default", "require-approval", "auto-approve")
WORKSPACE_SOURCES = ("per-case-temp", "pinned-path")

EVIDENCE_CAPABILITY_FIELDS = frozenset({
    "session_events",
    "tool_events",
    "usage",
    "cost",
    "model_identity",
    "artifact_collection",
    "cancel",
    "interactive_commands",
})

# 状态分层（M4-G02）：installed 只说明二进制/包在位；protocol_ready 说明
# 协议探测通过；execution_ready 需要真实任务证据，不由 --version 推出。
READINESS_LEVELS = ("installed", "protocol_ready", "execution_ready")


def validate_runtime_budgets(runtime: str, budgets: dict[str, Any]) -> dict[str, Any]:
    """Reject limits this runtime cannot enforce; never coerce strings or booleans.

    Missing means default. Zero tool calls means no tool execution. Timeouts and
    model-step limits must be positive. Native cost/token hard limits are not
    enforceable by these transports and must not be silently accepted.
    """
    common = {"total_timeout"}
    name = runtime.partition("@")[0]
    supported = common | (
        {"max_steps", "max_tool_calls"} if name == "pi-agent" else {"idle_timeout"}
    )
    unknown = set(budgets) - supported
    if unknown:
        raise ValueError(f"unsupported runtime budget fields: {sorted(unknown)}")
    for key, value in budgets.items():
        if key in {"max_steps", "max_tool_calls"}:
            minimum = 0 if key == "max_tool_calls" else 1
            if type(value) is not int or value < minimum:
                raise ValueError(f"runtime budget {key} must be an integer >= {minimum}")
        elif (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value <= 0
            or value > 1.7976931348623157e308
            or not math.isfinite(value)
        ):
            raise ValueError(f"runtime budget {key} must be a finite positive number")
    return dict(budgets)


class RuntimeToolControl(Contract):
    """工具控制声明：enforcement 描述实际强制主体，不授予权限。"""

    enforcement: str
    enforcement_owner: str
    tools: list[str] = Field(default_factory=list)
    network: str = "denied"
    approval: str = "deny-default"

    @field_validator("enforcement")
    @classmethod
    def known_enforcement(cls, value: str) -> str:
        if value not in TOOL_ENFORCEMENT_MODES:
            raise ValueError(f"unknown tool enforcement: {value}")
        return value

    @field_validator("enforcement_owner")
    @classmethod
    def known_owner(cls, value: str) -> str:
        if value not in ("platform", "runner", "external"):
            raise ValueError(f"unknown enforcement owner: {value}")
        return value

    @field_validator("network")
    @classmethod
    def known_network(cls, value: str) -> str:
        if value not in NETWORK_POLICIES:
            raise ValueError(f"unknown network policy: {value}")
        return value

    @field_validator("approval")
    @classmethod
    def known_approval(cls, value: str) -> str:
        if value not in APPROVAL_POLICIES:
            raise ValueError(f"unknown approval policy: {value}")
        return value

    @field_validator("tools")
    @classmethod
    def unique_tools(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("tool_control.tools must be unique non-empty strings")
        return value


class RuntimeDefinition(Contract):
    """一个 runtime backend 的不可变能力与协议定义。"""

    kind: str
    transport: str
    upstream_version: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    parser_version: str = Field(min_length=1)
    config_schema: dict[str, Any]
    supported_modes: list[str] = Field(min_length=1)
    interactive: bool = False
    model_control: str
    tool_control: RuntimeToolControl
    evidence_capabilities: dict[str, bool] = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def known_kind(cls, value: str) -> str:
        if value not in RUNTIME_KINDS:
            raise ValueError(f"unknown runtime kind: {value}")
        return value

    @field_validator("transport")
    @classmethod
    def known_transport(cls, value: str) -> str:
        if value not in RUNTIME_TRANSPORTS:
            raise ValueError(f"unknown runtime transport: {value}")
        return value

    @field_validator("model_control")
    @classmethod
    def known_model_control(cls, value: str) -> str:
        if value not in MODEL_CONTROL_MODES:
            raise ValueError(f"unknown model control: {value}")
        return value

    @field_validator("supported_modes")
    @classmethod
    def known_modes(cls, value: list[str]) -> list[str]:
        unknown = [mode for mode in value if mode not in RUNTIME_SUPPORTED_MODES]
        if unknown or not value:
            raise ValueError(f"unsupported runtime modes: {unknown}")
        if len(value) != len(set(value)):
            raise ValueError("supported_modes must be unique")
        return value

    @field_validator("config_schema")
    @classmethod
    def schema_shape(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value.get("properties"), dict) or not value["properties"]:
            raise ValueError("config_schema.properties must be a non-empty object")
        for name, spec in value["properties"].items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("config_schema property names must be non-empty")
            if not isinstance(spec, dict) or not isinstance(spec.get("type"), str):
                raise ValueError(f"config_schema property {name} requires a type")
        required = value.get("required", [])
        if not isinstance(required, list) or any(
            not isinstance(item, str) for item in required
        ):
            raise ValueError("config_schema.required must be a list of strings")
        missing = [item for item in required if item not in value["properties"]]
        if missing:
            raise ValueError(f"config_schema.required references unknown keys: {missing}")
        return value

    @field_validator("evidence_capabilities")
    @classmethod
    def known_evidence(cls, value: dict[str, bool]) -> dict[str, bool]:
        unknown = sorted(set(value) - EVIDENCE_CAPABILITY_FIELDS)
        if unknown:
            raise ValueError(f"unknown evidence capabilities: {unknown}")
        if any(type(flag) is not bool for flag in value.values()):
            raise ValueError("evidence capability flags must be booleans")
        return value

    @model_validator(mode="after")
    def interactive_consistency(self) -> RuntimeDefinition:
        if self.interactive and "interactive" not in self.supported_modes:
            raise ValueError("interactive runtime must list interactive in supported_modes")
        if not self.interactive and "interactive" in self.supported_modes:
            raise ValueError("supported_modes interactive requires interactive=true")
        return self


class RuntimeVersion(Contract):
    """已发布的 runtime 版本资源（不可变；name+version 唯一）。"""

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    definition: RuntimeDefinition
    lifecycle: str = "published"
    published_at: str = Field(min_length=1)

    @field_validator("lifecycle")
    @classmethod
    def published_only(cls, value: str) -> str:
        if value != "published":
            raise ValueError("runtime versions publish immediately and are immutable")
        return value


class RuntimeProfileVersion(Contract):
    """已发布的 runtime profile 版本资源（不可变；name+version 唯一）。"""

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    runtime: str = Field(min_length=3)
    native_settings: dict[str, Any] = Field(default_factory=dict)
    workspace: dict[str, Any] = Field(default_factory=lambda: {"source": "per-case-temp"})
    budgets: dict[str, Any] = Field(default_factory=dict)
    credential_refs: list[str] = Field(default_factory=list)
    published_at: str = Field(min_length=1)

    @model_validator(mode="after")
    def enforceable_budgets(self) -> RuntimeProfileVersion:
        validate_runtime_budgets(self.runtime, self.budgets)
        return self

    @field_validator("runtime")
    @classmethod
    def runtime_ref_shape(cls, value: str) -> str:
        name, separator, version = value.rpartition("@")
        if not separator or not name or not version:
            raise ValueError(f"runtime reference must be name@version: {value!r}")
        return value

    @field_validator("workspace")
    @classmethod
    def workspace_shape(cls, value: dict[str, Any]) -> dict[str, Any]:
        source = value.get("source")
        if source not in WORKSPACE_SOURCES:
            raise ValueError(f"unknown workspace source: {source!r}")
        if source == "pinned-path" and not isinstance(value.get("root"), str):
            raise ValueError("pinned-path workspace requires root")
        if source == "per-case-temp" and "root" in value:
            raise ValueError("per-case-temp workspace cannot pin root")
        return value

    @field_validator("credential_refs")
    @classmethod
    def ref_names_only(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(
            _CREDENTIAL_REF_NAME.fullmatch(item) is None
            or item.lower().startswith(tuple(prefix.lower() for prefix in _SECRET_VALUE_PREFIXES))
            for item in value
        ):
            raise ValueError("credential_refs must be unique non-empty names, never secrets")
        return value


class RuntimeProfile(Contract):
    """Run 级 runtime 配置：引用已发布版本并固定原生设置与政策。"""

    runtime: str = Field(min_length=3)
    native_settings: dict[str, Any] = Field(default_factory=dict)
    workspace: dict[str, Any] = Field(default_factory=lambda: {"source": "per-case-temp"})
    budgets: dict[str, Any] = Field(default_factory=dict)
    credential_refs: list[str] = Field(default_factory=list)
    config_hash: str | None = None

    @model_validator(mode="after")
    def enforceable_budgets(self) -> RuntimeProfile:
        validate_runtime_budgets(self.runtime, self.budgets)
        return self

    @field_validator("runtime")
    @classmethod
    def runtime_ref_shape(cls, value: str) -> str:
        name, separator, version = value.rpartition("@")
        if not separator or not name or not version:
            raise ValueError(f"runtime reference must be name@version: {value!r}")
        return value

    @field_validator("workspace")
    @classmethod
    def workspace_shape(cls, value: dict[str, Any]) -> dict[str, Any]:
        source = value.get("source")
        if source not in WORKSPACE_SOURCES:
            raise ValueError(f"unknown workspace source: {source!r}")
        if source == "pinned-path" and not isinstance(value.get("root"), str):
            raise ValueError("pinned-path workspace requires root")
        if source == "per-case-temp" and "root" in value:
            raise ValueError("per-case-temp workspace cannot pin root")
        return value

    @field_validator("credential_refs")
    @classmethod
    def ref_names_only(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(
            _CREDENTIAL_REF_NAME.fullmatch(item) is None
            or item.lower().startswith(tuple(prefix.lower() for prefix in _SECRET_VALUE_PREFIXES))
            for item in value
        ):
            raise ValueError("credential_refs must be unique non-empty names, never secrets")
        return value


_SCHEMA_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
}


def validate_runtime_settings(
    config_schema: dict[str, Any], settings: dict[str, Any]
) -> dict[str, Any]:
    """按 config_schema 字段 allowlist 校验原生设置；未知字段拒绝。"""
    if not isinstance(settings, dict):
        raise ValueError("runtime native settings must be an object")
    if not isinstance(config_schema, dict) or not isinstance(
        config_schema.get("properties"), dict
    ):
        raise ValueError("runtime config schema is missing properties")
    properties = config_schema["properties"]
    unknown = sorted(set(settings) - set(properties))
    if unknown:
        raise ValueError(f"runtime settings contain unknown fields: {unknown}")
    missing = [key for key in config_schema.get("required", []) if key not in settings]
    if missing:
        raise ValueError(f"runtime settings missing required fields: {missing}")
    for key, value in settings.items():
        expected = _SCHEMA_TYPES.get(properties[key]["type"])
        if expected is None:
            raise ValueError(f"runtime setting {key} has unsupported declared type")
        if expected == (int,) and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError(f"runtime setting {key} must be an integer")
        if expected == (bool,) and not isinstance(value, bool):
            raise ValueError(f"runtime setting {key} must be a boolean")
        if not isinstance(value, expected):
            raise ValueError(
                f"runtime setting {key} must be {properties[key]['type']}"
            )
    return settings


class RuntimeApprovalView(Contract):
    approval_id: str
    method: str
    item_id: str
    summary: str
    request_hash: str
    expires_at: str
    state: str


class RuntimeSessionView(Contract):
    session_id: str
    run_id: str
    case_id: str
    attempt_id: str
    state: str
    revision: int
    control_revision: int
    native_thread_id: str | None = None
    active_turn_id: str | None = None
    created_at: str | None = None
    terminal_at: str | None = None
    pending_approvals: list[RuntimeApprovalView] = Field(default_factory=list)


class RuntimeSessionList(Contract):
    items: list[RuntimeSessionView]
    total: int = Field(ge=0)


def runtime_readiness(
    *, installed: bool, protocol_ready: bool, execution_ready: bool,
    reasons: dict[str, str] | None = None,
) -> dict[str, Any]:
    """构造分层就绪状态；每层独立给出原因（M4-G02）。"""
    if execution_ready and not (installed and protocol_ready):
        raise ValueError("execution_ready requires installed and protocol_ready")
    if protocol_ready and not installed:
        raise ValueError("protocol_ready requires installed")
    return {
        "installed": installed,
        "protocol_ready": protocol_ready,
        "execution_ready": execution_ready,
        "reasons": reasons or {},
    }
