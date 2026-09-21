"""版本感知的 Skill 加载/缓存；持久仓库是唯一版本权威。

旧调用方仍然可以 `register(skill)` + `get(name)` 使用内存清单；一旦绑定仓库，
所有版本判定（存在性、内容 hash、lifecycle）都以仓库记录为准，内存注册只是历史
兼容路径，不参与版本权威。缓存只缓存**已校验**的契约模型，且每次读仓库都会比对
lifecycle/content_hash，因此 deprecate 不会被过期缓存掩盖。
"""
from __future__ import annotations

import re
from typing import Any

from .versions import (
    SkillDeprecated,
    SkillError,
    SkillNotFound,
    SkillSetRef,
    SkillVersion,
    select_skills,
)

_VERSION_SPLIT = re.compile(r"[._-]")


def _version_key(version: Any) -> tuple[tuple[int, Any], ...]:
    """数字感知的版本排序键（"1.10" > "1.9"），非数字段退化为字符串比较。"""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in _VERSION_SPLIT.split(str(version))
    )


class SkillRegistry:
    """版本化查找/缓存门面；无仓库时保持旧的按名称内存注册行为。"""

    def __init__(self, repository: Any = None) -> None:
        self._repository = repository
        self._legacy: dict[tuple[str, str], Any] = {}
        self._cache: dict[tuple[str, str], SkillVersion] = {}

    @property
    def repository(self) -> Any:
        return self._repository

    def bind(self, repository: Any) -> None:
        """绑定/替换持久仓库；缓存全部失效，仓库从此是唯一版本权威。"""
        self._repository = repository
        self.clear_cache()

    # ------------------------------------------------------------ 旧 API 兼容

    def register(self, skill: Any, *, version: str | None = None) -> Any:
        """注册内存 Skill（旧调用方）。

        SkillManifest.name / SkillVersion.skill_id 都能解析；带仓库时它不参与
        版本查找，只是兼容旧代码不报错。
        """
        name = getattr(skill, "skill_id", None) or getattr(skill, "name", None)
        if name is None:
            raise SkillError("registered skills must expose skill_id or name")
        resolved = version or getattr(skill, "version", None)
        if resolved is None:
            raise SkillError("registered skills must expose version")
        self._legacy[(str(name), str(resolved))] = skill
        return skill

    def get(self, name: str, version: str | None = None) -> Any:
        """按名称（或名称+版本）取 Skill；缺失返回 None（旧行为）。

        配置仓库时先查仓库（含 deprecated，因为历史读取必须可复现），再退回
        内存注册。
        """
        if self._repository is not None:
            record = self._optional(
                str(name), None if version is None else str(version), allow_deprecated=True
            )
            if record is not None:
                return record
        key = (str(name), str(version))
        if version is not None:
            return self._legacy.get(key)
        matches = [
            skill for (registered_name, _), skill in self._legacy.items()
            if registered_name == str(name)
        ]
        return matches[-1] if matches else None

    # ------------------------------------------------------------ 版本化 API

    def resolve(
        self, skill_id: str, version: str, *, allow_deprecated: bool = False
    ) -> SkillVersion:
        """严格解析一个已发布版本；deprecated 默认拒绝新选择。"""
        record = self._load(str(skill_id), str(version))
        if record is None:
            raise SkillNotFound(f"skill version {skill_id}@{version} is not published")
        if record.lifecycle == "deprecated" and not allow_deprecated:
            raise SkillDeprecated(
                f"skill version {skill_id}@{version} is deprecated; select a new version"
            )
        return record

    def latest(self, skill_id: str, *, allow_deprecated: bool = False) -> SkillVersion | None:
        """按版本号取该 Skill 的最新记录；无仓库或不存在时返回 None。"""
        if self._repository is None:
            return None
        records = [
            raw for raw in self._repository.list()
            if raw.get("skill_id") == skill_id
            and (allow_deprecated or raw.get("lifecycle") == "published")
        ]
        if not records:
            return None
        newest = max(records, key=lambda raw: _version_key(raw.get("version", "")))
        return self._cache_record(newest)

    def versions(self, skill_id: str, *, allow_deprecated: bool = True) -> tuple[str, ...]:
        """按版本号降序列出该 Skill 的版本字符串。"""
        if self._repository is None:
            return ()
        records = [
            raw for raw in self._repository.list()
            if raw.get("skill_id") == skill_id
            and (allow_deprecated or raw.get("lifecycle") == "published")
        ]
        return tuple(
            str(raw["version"])
            for raw in sorted(records, key=lambda raw: _version_key(raw.get("version", "")), reverse=True)
        )

    def list(self, *, lifecycle: str | None = None) -> list[dict[str, Any]]:
        """仓库记录原样列出（仓库是权威，这里不做二次解释）。"""
        if self._repository is None:
            return []
        records = list(self._repository.list())
        if lifecycle is None:
            return records
        return [raw for raw in records if raw.get("lifecycle") == lifecycle]

    def select(self, selection: Any, *, allow_deprecated: bool = False) -> tuple[SkillVersion, ...]:
        """按声明顺序解析 Skill 集合（顺序是身份的一部分）。"""
        if self._repository is None:
            raise SkillError("version selection requires a bound repository")
        set_ref = (
            selection if isinstance(selection, SkillSetRef) else SkillSetRef.model_validate(selection)
        )
        return select_skills(self._repository, set_ref, allow_deprecated=allow_deprecated)

    def cached(self, skill_id: str, version: str) -> SkillVersion | None:
        return self._cache.get((str(skill_id), str(version)))

    def clear_cache(self) -> None:
        self._cache.clear()

    # ------------------------------------------------------------------ 内部

    def _optional(
        self, skill_id: str, version: str | None, *, allow_deprecated: bool
    ) -> SkillVersion | None:
        if version is None:
            return self.latest(skill_id, allow_deprecated=allow_deprecated)
        record = self._load(skill_id, version)
        if record is None:
            return None
        if record.lifecycle == "deprecated" and not allow_deprecated:
            return None
        return record

    def _load(self, skill_id: str, version: str) -> SkillVersion | None:
        raw = self._repository.get(skill_id, version)
        if raw is None:
            return None
        cached = self._cache.get((skill_id, version))
        if (
            cached is not None
            and cached.lifecycle == raw.get("lifecycle")
            and cached.content_hash == raw.get("content_hash")
        ):
            return cached
        return self._cache_record(raw)

    def _cache_record(self, raw: dict[str, Any]) -> SkillVersion:
        record = SkillVersion.model_validate(raw)
        self._cache[(record.skill_id, record.version)] = record
        return record
