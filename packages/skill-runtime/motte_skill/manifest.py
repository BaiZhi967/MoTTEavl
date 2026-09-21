"""兼容门面：SkillVersion 契约的正式家是 :mod:`motte_skill.versions`。

旧代码 `from motte_skill.manifest import SkillManifest` 继续可用；SkillManifest 是
v0 的内存清单（entrypoint 是字符串），它**不是**可发布契约：任意 shell 字符串永远
进不了 SkillVersion（M5-T06 规则 1）。迁移时把字符串入口换成
`SkillEntrypoint(interpreter="python3", argv=["run.py", ...])`。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .versions import (
    INJECTION_MODES,
    SKILL_KINDS,
    SKILL_LIFECYCLES,
    SKILL_SCHEMA_VERSION,
    SkillBinding,
    SkillContentHashMismatch,
    SkillDependency,
    SkillDependencyNotPinned,
    SkillDeprecated,
    SkillDraft,
    SkillEntrypoint,
    SkillError,
    SkillFixtureRef,
    SkillNotFound,
    SkillPermissions,
    SkillResource,
    SkillSetRef,
    SkillVersion,
    SkillVersionConflict,
    deprecate_skill,
    is_exact_pin,
    publish_skill,
    select_skills,
    skill_content_hash,
    skill_version_transition,
    verify_dependency_refs,
    verify_resources,
)

__all__ = [
    "INJECTION_MODES",
    "SKILL_KINDS",
    "SKILL_LIFECYCLES",
    "SKILL_SCHEMA_VERSION",
    "SkillBinding",
    "SkillContentHashMismatch",
    "SkillDependency",
    "SkillDependencyNotPinned",
    "SkillDeprecated",
    "SkillDraft",
    "SkillEntrypoint",
    "SkillError",
    "SkillFixtureRef",
    "SkillManifest",
    "SkillNotFound",
    "SkillPermissions",
    "SkillResource",
    "SkillSetRef",
    "SkillVersion",
    "SkillVersionConflict",
    "deprecate_skill",
    "is_exact_pin",
    "publish_skill",
    "select_skills",
    "skill_content_hash",
    "skill_version_transition",
    "verify_dependency_refs",
    "verify_resources",
]


@dataclass(frozen=True)
class SkillManifest:
    """旧版内存 Skill 清单（保留兼容）。

    entrypoint 是**字符串**，因此它只能用于旧的内存注册路径；要发布必须显式迁移
    成 kind 区分的 SkillDraft/SkillVersion，不能把字符串悄悄解释为 argv。
    """

    name: str
    version: str
    entrypoint: str
    permissions: tuple[str, ...] = field(default_factory=tuple)
