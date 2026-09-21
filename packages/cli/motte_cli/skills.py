"""Skill 版本资源的 CLI 实现（M5，验收 F-05）。

与 API 共用同一个事实源 motte_skill.versions.SkillVersion：同一份文档在两侧得到
同一接受/拒绝集合与同一 content_hash。

发布只写不可变版本资源：不执行入口、不注入、不创建 Run、不调用模型。旧实现
（apps/api/app/main.py 的注释）声称"API 进程尚未接内容存储"所以不暴露发布，于是
Skill 注入与三臂对照在产品面完全不可达；实际上 content_store 早已由
create_resource_store() 装配，CLI 侧现在也显式装配同一个目录。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from motte_cli.workflows import WorkflowDocumentError, load_document

#: 与 Workflow / Fixture 共用同一形状的结构化失败（稳定 code + 逐字段错误）。
SkillDocumentError = WorkflowDocumentError

__all__ = ["SkillDocumentError", "load_document", "publication_record", "publish_skill"]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _field_errors(error: Any) -> list[dict[str, str]]:
    fields: list[dict[str, str]] = []
    for issue in error.errors():
        location = ".".join(str(part) for part in issue.get("loc", ())) or "$"
        fields.append({
            "field": location,
            "code": str(issue.get("type") or "invalid"),
            "message": str(issue.get("msg") or "invalid value"),
        })
    return fields


def publication_record(
    document: Any, *, published_at: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """把文档规范化为可发布的 SkillVersion 记录（与 API 发布同一规则）。

    草稿被拒绝（版本仓库只接受已发布内容）；依赖必须固定版本且可解析；
    content_hash 由本函数固定，客户端提交不一致的 hash 立即拒绝。
    """
    if not isinstance(document, dict):
        raise SkillDocumentError("CONTRACT_INVALID", "skill document must be a JSON object")
    managed = sorted(set(document).intersection({"_deleted"}))
    if managed:
        raise SkillDocumentError(
            "SERVER_MANAGED_FIELD", f"server-managed fields are not accepted: {managed}"
        )
    from motte_sdk.resolve import find_secret_paths

    leaked = find_secret_paths(document)
    if leaked:
        raise SkillDocumentError(
            "CREDENTIALS_REJECTED", f"plaintext credential fields are not accepted: {leaked}"
        )
    from motte_skill.versions import (
        SkillContentHashMismatch,
        SkillVersion,
        skill_content_hash,
        verify_dependency_refs,
    )
    from pydantic import ValidationError

    defaulted: list[str] = []
    candidate = dict(document)
    if candidate.get("lifecycle", "published") == "draft":
        raise SkillDocumentError(
            "SKILL_NOT_PUBLISHED",
            "only published skill versions can be stored; a draft has no identity to freeze",
        )
    if not candidate.get("published_at"):
        candidate["published_at"] = published_at or _utc_now()
        defaulted.append("published_at")
    try:
        skill = SkillVersion.model_validate(candidate)
    except ValidationError as error:
        raise SkillDocumentError(
            "SKILL_INVALID",
            f"skill document is not a valid version: {error.error_count()} field error(s)",
            fields=_field_errors(error),
        ) from error
    try:
        verify_dependency_refs(skill.dependency_refs)
        computed = skill_content_hash(skill)
        if skill.content_hash is not None and skill.content_hash != computed:
            raise SkillContentHashMismatch("skill content_hash does not match its content")
    except (SkillContentHashMismatch, ValueError) as error:
        raise SkillDocumentError("SKILL_INVALID", str(error)) from error
    return {**skill.model_dump(mode="json"), "content_hash": computed}, defaulted


def publish_skill(resources: Any, document: Any) -> dict[str, Any]:
    """规范并发布一个 SkillVersion（同内容幂等，异内容冲突，弃用受控转换）。"""
    record, _defaulted = publication_record(document)
    return resources.publish_skill(record)
