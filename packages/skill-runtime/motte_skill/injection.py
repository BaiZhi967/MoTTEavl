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

#: Run 冻结快照里注入声明的键（resource_snapshots[SKILL_INJECTION_SNAPSHOT_KEY]）。
#: 声明由创建期生成、执行期只读；执行侧重建计划并复核 hash（篡改即具名拒绝）。
SKILL_INJECTION_SNAPSHOT_KEY = "skill_injection"
DECLARATION_SCHEMA_VERSION = 1

#: Agent 请求里每个 Skill 段落的固定前缀：顺序与切分都由它决定，不是展示装饰。
INJECTION_SECTION_PREFIX = "## MoTTE Skill: "

#: 内置上下文注入器能承载的适配方式；native-loader 必须由原生加载器交付，
#: 不能"顺带"当作文本塞进 system prompt 冒充已生效。
CONTEXT_ADAPTERS: tuple[str, ...] = ("builtin-context-section",)
NATIVE_LOADER_ADAPTER = "platform-native-loader"


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

    declared_any: set[str] = set()
    for value in declared_everywhere:
        declared_any |= _as_set(value)
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
    # 审计：确实被声明过、却没能通过交集/硬否决的工具要具名记录原因。
    for tool in sorted(declared_any - tools):
        if not any(item == "tool:" + tool for item in denied):
            deny("tool", tool)

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
            #: 冻结的渲染文本：Target 只做拼接，不做二次渲染（hash 由它重算核验）。
            "rendered": self.rendered,
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


def _plan_hash(
    entries: Sequence[InjectionEntry],
    permissions: EffectivePermissions,
    scope: str,
) -> str:
    """计划身份：有序条目（含渲染 hash）+ 有效权限 + 验证范围。

    创建期与执行侧重算共用这一份实现；任何一份不一致都改变 hash。
    """
    if not entries:
        return canonical_sha256({"entries": []})
    return canonical_sha256({
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
    plan_hash = _plan_hash(entries, permissions, scope)
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


# ---------------------------------------------------------------- 冻结声明


def _entry_from_payload(payload: Mapping[str, Any]) -> InjectionEntry:
    try:
        return InjectionEntry(
            position=int(payload["position"]),
            skill_id=str(payload["skill_id"]),
            version=str(payload["version"]),
            kind=str(payload.get("kind") or ""),
            content_hash=payload.get("content_hash"),
            injection_mode=str(payload.get("injection_mode") or "system-prompt"),
            rendered=str(payload.get("rendered") or ""),
            rendered_hash=str(payload["rendered_hash"]),
            resource_refs=tuple(
                dict(item) for item in (payload.get("resource_refs") or ())
            ),
            instruction_tokens=dict(payload.get("instruction_tokens") or {}),
            adapter=str(payload.get("adapter") or "builtin-context-section"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise InjectionError(
            "INJECTION_DECLARATION_INVALID",
            f"frozen injection entry is not a valid declaration: {error}",
        ) from error


def _permissions_from_payload(payload: Mapping[str, Any]) -> EffectivePermissions:
    if not isinstance(payload, Mapping):
        raise InjectionError(
            "INJECTION_DECLARATION_INVALID", "effective_permissions must be an object"
        )
    modes = payload.get("tool_modes") or {}
    return EffectivePermissions(
        tools=tuple(str(item) for item in (payload.get("tools") or ())),
        tool_modes={str(key): str(value) for key, value in modes.items()},
        filesystem_read=tuple(str(item) for item in (payload.get("filesystem_read") or ())),
        filesystem_write=tuple(str(item) for item in (payload.get("filesystem_write") or ())),
        network=str(payload.get("network") or "none"),
        credential_refs=tuple(str(item) for item in (payload.get("credential_refs") or ())),
        denied=tuple(str(item) for item in (payload.get("denied") or ())),
    )


def plan_from_declaration(declaration: Mapping[str, Any] | None) -> InjectionPlan:
    """从冻结声明重建注入计划，并**重算 hash**（创建期与执行期同一身份）。

    声明可以是一份完整冻结快照（带 declaration 键）或计划本身的 as_dict()。
    渲染内容被改写、条目被重排、权限被放宽都会让重算的 plan_hash 与冻结值
    不一致；这里 fail closed，绝不静默采用被改过的声明。
    """
    if declaration is None:
        raise InjectionError("INJECTION_DECLARATION_INVALID", "injection declaration is missing")
    if not isinstance(declaration, Mapping):
        raise InjectionError(
            "INJECTION_DECLARATION_INVALID", "injection declaration must be an object"
        )
    payload = declaration.get("declaration", declaration)
    if not isinstance(payload, Mapping):
        raise InjectionError(
            "INJECTION_DECLARATION_INVALID", "injection declaration payload must be an object"
        )
    entries = tuple(_entry_from_payload(item) for item in (payload.get("entries") or ()))
    for index, entry in enumerate(entries):
        if entry.position != index:
            raise InjectionError(
                "INJECTION_DECLARATION_TAMPERED",
                f"frozen injection entry {index} declares position {entry.position}",
            )
        recomputed = canonical_sha256({
            "skill": f"{entry.skill_id}@{entry.version}",
            "rendered": entry.rendered,
            "position": entry.position,
        })
        if recomputed != entry.rendered_hash:
            raise InjectionError(
                "INJECTION_DECLARATION_TAMPERED",
                f"frozen injection entry {entry.skill_id}@{entry.version} "
                "does not match its rendered_hash",
            )
    permissions = _permissions_from_payload(payload.get("effective_permissions") or {})
    scope = str(payload.get("validation_scope") or "static")
    if scope not in VALIDATION_SCOPES:
        raise InjectionError(
            "INJECTION_DECLARATION_INVALID", f"unknown validation scope: {scope!r}"
        )
    computed = _plan_hash(entries, permissions, scope)
    if payload.get("plan_hash") != computed:
        raise InjectionError(
            "INJECTION_DECLARATION_TAMPERED",
            "frozen injection declaration does not match its own plan_hash",
        )
    digest = declaration.get("injection_digest")
    if digest is not None and digest != canonical_sha256(dict(payload)):
        raise InjectionError(
            "INJECTION_DECLARATION_TAMPERED",
            "frozen injection declaration does not match its injection_digest",
        )
    modes = {entry.injection_mode for entry in entries}
    default_observability = "partial" if "native-loader" in modes else "complete"
    return InjectionPlan(
        entries=entries,
        plan_hash=computed,
        effective_permissions=permissions,
        validation_scope=scope,
        observability=str(payload.get("observability") or default_observability),
        conflicts=tuple(str(item) for item in (payload.get("conflicts") or ())),
        platform_native_loader=payload.get("platform_native_loader"),
    )


def plan_as_declaration(
    plan: InjectionPlan,
    *,
    refs: Sequence[str] = (),
    skills: Sequence[Any] = (),
    render_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把计划冻结成 Run 快照里的声明（创建期**唯一**生成点）。

    skills 是选中的已发布版本：executable 形态连同入口/输入输出 schema 与资源
    清单一起冻结，执行侧不再按可变名称重新解析资源。
    """
    declaration = plan.as_dict()
    executables: list[dict[str, Any]] = []
    for skill in skills:
        if str(getattr(skill, "kind", "")) != "executable":
            continue
        entrypoint = getattr(skill, "entrypoint", None)
        executables.append({
            "skill_id": skill.skill_id,
            "version": str(skill.version),
            "kind": str(skill.kind),
            "content_hash": getattr(skill, "content_hash", None),
            "entrypoint": entrypoint.model_dump(mode="json") if entrypoint is not None else None,
            "input_schema": dict(getattr(skill, "input_schema", {}) or {}),
            "output_schema": dict(getattr(skill, "output_schema", {}) or {}),
            "resource_manifest": [
                {
                    "path": resource.path, "sha256": resource.sha256,
                    "size_bytes": resource.size_bytes,
                }
                for resource in (getattr(skill, "resource_manifest", ()) or ())
            ],
        })
    return {
        "schema_version": DECLARATION_SCHEMA_VERSION,
        "refs": [str(ref) for ref in refs],
        "plan_hash": plan.plan_hash,
        "injection_digest": canonical_sha256(declaration),
        "declaration": declaration,
        "render_context_sha256": canonical_sha256(dict(render_context or {})),
        "executables": executables,
    }


def instruction_overhead_totals(plan: InjectionPlan) -> dict[str, Any]:
    """指令 token 开销合计；estimated 单列，绝不重复计入模型费用。"""
    estimates = [
        int(entry.instruction_tokens.get("estimate") or 0) for entry in plan.entries
    ]
    methods = sorted({
        str(entry.instruction_tokens.get("method") or "chars-over-4")
        for entry in plan.entries
    })
    return {
        "method": ",".join(methods),
        "estimate": sum(estimates),
        "measured": False,
        "source": "skill-instruction",
        "entries": len(plan.entries),
        "billed": False,
        "note": (
            "instruction overhead is an estimate and is not added to billed model usage"
        ),
    }


def compose_agent_system_prompt(
    declaration: Mapping[str, Any] | InjectionPlan | None,
    *,
    base_prompt: str | None = None,
    adapters: Sequence[str] = CONTEXT_ADAPTERS,
) -> str:
    """把冻结的注入声明渲染成**实际送入 Agent 请求**的 system prompt。

    顺序、段落边界与渲染文本都来自声明；调用方（Target/runtime 装配）只做
    拼接，不做二次渲染。native-loader 形态不能由上下文注入器承载：这里具名
    拒绝，而不是把它当普通文本塞进去冒充已生效。
    """
    if declaration is None:
        return base_prompt or ""
    plan = (
        declaration if isinstance(declaration, InjectionPlan)
        else plan_from_declaration(declaration)
    )
    if not plan.entries:
        return base_prompt or ""
    allowed = set(adapters)
    parts: list[str] = []
    if base_prompt:
        parts.append(base_prompt)
    for entry in plan.entries:
        if entry.adapter not in allowed:
            raise InjectionError(
                "INJECTION_ADAPTER_UNSUPPORTED",
                f"skill {entry.skill_id}@{entry.version} uses adapter {entry.adapter!r}, "
                "which a context-section endpoint cannot deliver",
            )
        parts.append(
            f"{INJECTION_SECTION_PREFIX}{entry.skill_id}@{entry.version} "
            f"kind={entry.kind} injection={entry.injection_mode}"
        )
        parts.append(entry.rendered)
    return "\n\n".join(part for part in parts if part != "")


def executable_declarations(
    declaration: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], ...]:
    """冻结声明里的 executable 形态快照（执行侧只读它，不重新解析资源）。"""
    if not isinstance(declaration, Mapping):
        return ()
    items = declaration.get("executables") or ()
    return tuple(dict(item) for item in items if isinstance(item, Mapping))

