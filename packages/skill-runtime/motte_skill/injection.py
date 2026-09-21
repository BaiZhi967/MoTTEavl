"""M5-T07 Skill 注入编译、有效权限与验证范围。

三件事在这一层固定下来，并且**只有一份实现**：

1. 注入计划：有序 Skill、渲染结果与 hash、冲突政策、资源落位、适配方式。
   只记录"选中了 Skill"不够——必须能核对最终进入目标的内容或原生加载清单。
2. 有效权限：平台 ∩ Scenario ∩ Target 能力 ∩ Skill 请求，**deny 优先**。
   Skill 不能扩大路径/网络，也不能把 mock/replay 提升为 real。
3. 验证范围：static / executable-fixture / agent-behaviour 三者分别标注，
   纯指令的静态校验绝不能被说成"独立程序已执行"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from motte_contracts.identity import canonical_sha256

#: 权限来源，按优先级从高到低；高优先级先裁剪，低优先级只能收缩。
PERMISSION_SOURCES: tuple[str, ...] = ("platform", "scenario", "target", "skill")

#: 工具模式强度：deny 最强，其次 mock/replay，real 最弱（不可提升）。
TOOL_MODE_STRENGTH: dict[str, int] = {
    "deny": 3, "mock": 2, "replay": 2, "real": 1,
}

VALIDATION_SCOPES: tuple[str, ...] = (
    "static", "executable-fixture", "agent-behaviour",
)
OBSERVABILITY = ("complete", "partial")


class InjectionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class EffectivePermissions:
    """权限交集结果；denied 记录被裁剪掉的能力，便于审计与解释。"""

    tools: tuple[str, ...] = ()
    tool_modes: Mapping[str, str] = field(default_factory=dict)
    filesystem_read: tuple[str, ...] = ()
    filesystem_write: tuple[str, ...] = ()
    network: str = "none"
    credential_refs: tuple[str, ...] = ()
    denied: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "tools": list(self.tools),
            "tool_modes": dict(sorted(self.tool_modes.items())),
            "filesystem_read": list(self.filesystem_read),
            "filesystem_write": list(self.filesystem_write),
            "network": self.network,
            "credential_refs": list(self.credential_refs),
            "denied": list(self.denied),
        }


def _as_set(value: Any) -> set[str]:
    return {str(item) for item in (value or ())}


def _mode_of(policy: Mapping[str, Any], tool: str) -> str:
    modes = policy.get("tool_modes") or {}
    if isinstance(modes, Mapping) and tool in modes:
        return str(modes[tool])
    return str(policy.get("default_tool_mode") or "real")


def intersect_permissions(policies: Mapping[str, Mapping[str, Any]]) -> EffectivePermissions:
    """求权限交集：只有所有来源都允许的能力才生效，deny 优先。

    传入映射的键是 PERMISSION_SOURCES 里的来源名。缺失来源按"未声明"处理
    （不额外授予）；平台来源的 deny_* 是硬否决，任何 Skill 都不能绕过。
    """
    unknown = sorted(set(policies) - set(PERMISSION_SOURCES))
    if unknown:
        raise InjectionError(
            "INJECTION_POLICY_UNKNOWN", "unknown permission sources: " + ", ".join(unknown)
        )
    ordered = [policies[name] for name in PERMISSION_SOURCES if name in policies]
    if not ordered:
        raise InjectionError("INJECTION_POLICY_EMPTY", "at least the platform policy is required")
    platform = policies.get("platform") or {}
    denied: list[str] = []

    def deny(name: str, reason: str) -> None:
        denied.append(f"{name}:{reason}")

    declarations = [policy.get("tools") for policy in ordered]
    declared_everywhere = [value for value in declarations if value is not None]
    if not declared_everywhere:
        tools: set[str] = set()
    else:
        tools = _as_set(declared_everywhere[0])
        for value in declared_everywhere[1:]:
            tools &= _as_set(value)

    denied_tools = _as_set(platform.get("deny_tools"))
    for tool in sorted(tools & denied_tools):
        deny("tool", tool)
    tools -= denied_tools
    # 模式取最强（最保守）的一个：Skill 只能收紧，不能把 mock 变 real。
    modes: dict[str, str] = {}
    for tool in sorted(tools):
        candidates = {_mode_of(policy, tool) for policy in ordered}
        strongest = max(candidates, key=lambda mode: TOOL_MODE_STRENGTH.get(mode, 99))
        if strongest == "deny":
            deny("tool_mode", tool)
            continue
        modes[tool] = strongest
    tools = {tool for tool in tools if tool in modes}

    def narrow(field: str, allow_field: str, deny_field: str) -> tuple[str, ...]:
        values = [policy.get(field) for policy in ordered]
        scoped = [value for value in values if value is not None]
        if not scoped:
            return ()
        common = _as_set(scoped[0])
        for value in scoped[1:]:
            common &= _as_set(value)
        allowed = _as_set(platform.get(allow_field))
        blocked = _as_set(platform.get(deny_field))
        if allowed:
            for item in sorted(common - allowed):
                deny(field, item)
            common &= allowed
        for item in sorted(common & blocked):
            deny(field, item)
        common -= blocked
        return tuple(sorted(common))

    reads = narrow("filesystem_read", "allow_read", "deny_paths")
    writes = narrow("filesystem_write", "allow_write", "deny_paths")

    network_values = {str(policy.get("network") or "none") for policy in ordered}
    network = "none" if "none" in network_values else "allowed"
    if str(platform.get("network") or "none") == "none":
        network = "none"
    if network != "none":  # 首批只允许 none：放开网络需要显式的新政策与证据
        deny("network", network)
        network = "none"

    credential_sets = [policy.get("credential_refs") for policy in ordered]
    scoped_credentials = [value for value in credential_sets if value is not None]
    credentials: tuple[str, ...] = ()
    if scoped_credentials:
        common_credentials = _as_set(scoped_credentials[0])
        for value in scoped_credentials[1:]:
            common_credentials &= _as_set(value)
        allowed_credentials = _as_set(platform.get("allow_credentials"))
        if allowed_credentials:
            common_credentials &= allowed_credentials
        credentials = tuple(sorted(common_credentials))
    return EffectivePermissions(
        tools=tuple(sorted(tools)),
        tool_modes=modes,
        filesystem_read=reads,
        filesystem_write=writes,
        network=network,
        credential_refs=credentials,
        denied=tuple(sorted(set(denied))),
    )


@dataclass(frozen=True)
class InjectionEntry:
    position: int
    skill_id: str
    version: str
    kind: str
    content_hash: str | None
    injection_mode: str
    rendered: str
    rendered_hash: str
    resource_refs: tuple[dict[str, Any], ...] = ()
    instruction_tokens: dict[str, Any] = field(default_factory=dict)
    adapter: str = "builtin-context-section"

    def as_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "skill_id": self.skill_id,
            "version": self.version,
            "kind": self.kind,
            "content_hash": self.content_hash,
            "injection_mode": self.injection_mode,
            "rendered_hash": self.rendered_hash,
            "resource_refs": [dict(item) for item in self.resource_refs],
            "instruction_tokens": dict(self.instruction_tokens),
            "adapter": self.adapter,
        }


@dataclass(frozen=True)
class InjectionPlan:
    entries: tuple[InjectionEntry, ...]
    plan_hash: str
    effective_permissions: EffectivePermissions
    validation_scope: str
    observability: str
    conflicts: tuple[str, ...] = ()
    platform_native_loader: str | None = None

    @property
    def injects_instruction(self) -> bool:
        return bool(self.entries)

    def as_dict(self) -> dict[str, Any]:
        return {
            "entries": [entry.as_dict() for entry in self.entries],
            "plan_hash": self.plan_hash,
            "effective_permissions": self.effective_permissions.as_dict(),
            "validation_scope": self.validation_scope,
            "observability": self.observability,
            "conflicts": list(self.conflicts),
            "platform_native_loader": self.platform_native_loader,
        }


def _render(skill: Any, context: Mapping[str, Any] | None) -> str:
    template = getattr(skill, "instruction", None) or ""
    if not template:
        ref = getattr(skill, "instruction_ref", None)
        if ref:
            raise InjectionError(
                "INJECTION_INSTRUCTION_UNRESOLVED",
                f"skill {skill.skill_id}@{skill.version} references {ref!r} without resolved bytes",
            )
        return ""
    rendered = str(template)
    for key, value in sorted((context or {}).items()):
        rendered = rendered.replace("{{" + str(key) + "}}", str(value))
    if "{{" in rendered:
        raise InjectionError(
            "INJECTION_UNRESOLVED_PLACEHOLDER",
            f"skill {skill.skill_id}@{skill.version} has unresolved render placeholders",
        )
    return rendered


def _token_overhead(text: str) -> dict[str, Any]:
    """指令 token 开销单列，并如实标注是估算而不是实际计费。"""
    return {
        "method": "chars-over-4",
        "estimate": max(1, len(text) // 4) if text else 0,
        "measured": False,
        "source": "skill-instruction",
    }


def compile_injection(
    *,
    selected: Sequence[Any],
    policies: Mapping[str, Mapping[str, Any]],
    render_context: Mapping[str, Any] | None = None,
    behaviour_tested: bool = False,
) -> InjectionPlan:
    """编译有序注入计划 + 有效权限 + 验证范围。

    selected 必须按声明顺序传入：多 Skill 的**次序也是身份的一部分**，
    因此交换顺序会改变 plan_hash。
    """
    permissions = intersect_permissions(policies)
    if not selected:
        return InjectionPlan(
            entries=(), plan_hash=canonical_sha256({"entries": []}),
            effective_permissions=permissions, validation_scope="static",
            observability="complete",
        )
    conflicts: list[str] = []
    entries: list[InjectionEntry] = []
    modes: set[str] = set()
    for position, skill in enumerate(selected):
        mode = str(getattr(skill, "injection_mode", "system-prompt"))
        modes.add(mode)
        rendered = _render(skill, render_context)
        if not rendered and str(getattr(skill, "kind", "")) in (
            "instruction", "instruction_with_resources",
        ):
            conflicts.append(f"EMPTY_INSTRUCTION:{skill.skill_id}@{skill.version}")
        entries.append(InjectionEntry(
            position=position,
            skill_id=skill.skill_id,
            version=str(skill.version),
            kind=str(getattr(skill, "kind", "")),
            content_hash=getattr(skill, "content_hash", None),
            injection_mode=mode,
            rendered=rendered,
            rendered_hash=canonical_sha256({
                "skill": f"{skill.skill_id}@{skill.version}",
                "rendered": rendered,
                "position": position,
            }),
            resource_refs=tuple(
                {
                    "path": resource.path, "sha256": resource.sha256,
                    "size_bytes": resource.size_bytes,
                }
                for resource in (getattr(skill, "resource_manifest", ()) or ())
            ),
            instruction_tokens=_token_overhead(rendered),
            adapter=(
                "platform-native-loader" if mode == "native-loader"
                else "builtin-context-section"
            ),
        ))
    if len(modes) > 1:
        conflicts.append("INJECTION_MODE_MIXED:" + ",".join(sorted(modes)))
    kinds = {entry.kind for entry in entries}
    if behaviour_tested:
        scope = "agent-behaviour"
    elif "executable" in kinds:
        scope = "executable-fixture"
    else:
        scope = "static"
    # 原生加载无法观测实际送入的内容：只能标 partial，不能宣称已完整生效。
    observability = "partial" if "native-loader" in modes else "complete"
    plan_hash = canonical_sha256({
        "entries": [
            {
                "position": entry.position,
                "skill_id": entry.skill_id,
                "version": entry.version,
                "rendered_hash": entry.rendered_hash,
                "injection_mode": entry.injection_mode,
                "content_hash": entry.content_hash,
            }
            for entry in entries
        ],
        "effective_permissions": permissions.as_dict(),
        "validation_scope": scope,
    })
    return InjectionPlan(
        entries=tuple(entries),
        plan_hash=plan_hash,
        effective_permissions=permissions,
        validation_scope=scope,
        observability=observability,
        conflicts=tuple(conflicts),
        platform_native_loader=None,
    )


def select_for_execution(plan: InjectionPlan, tool_modes: Mapping[str, str]) -> dict[str, Any]:
    """执行期网关：把有效权限变成真正可执行的政策。

    夹具工具不能因为 Skill 申请而升级成真实业务工具——这里的模式来自
    intersect_permissions，而不是 Skill 自己声明的值。
    """
    effective = plan.effective_permissions
    for tool, mode in (tool_modes or {}).items():
        granted = effective.tool_modes.get(tool)
        if granted is None:
            raise InjectionError(
                "INJECTION_TOOL_NOT_GRANTED",
                f"tool {tool!r} is not granted by the effective permission intersection",
            )
        if TOOL_MODE_STRENGTH.get(mode, 99) < TOOL_MODE_STRENGTH.get(granted, 0):
            raise InjectionError(
                "INJECTION_TOOL_MODE_ESCALATION",
                f"tool {tool!r} requests mode {mode!r} but the intersection grants {granted!r}",
            )
    return {
        "tools": list(effective.tools),
        "tool_modes": dict(effective.tool_modes),
        "network": effective.network,
        "filesystem_read": list(effective.filesystem_read),
        "filesystem_write": list(effective.filesystem_write),
    }


def injection_digest(plan: InjectionPlan) -> str:
    """进入 Run 冻结快照的注入摘要：顺序、渲染 hash 与权限一起固定。"""
    return canonical_sha256(plan.as_dict())
