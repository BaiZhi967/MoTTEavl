"""Fixture 版本资源的 CLI 实现（M5，验收 F-04）。

与 API 共用**同一个事实源** motte_contracts.fixture.FixtureSpec：同一份文档在两侧
得到同一接受/拒绝集合、同一逐字段错误与同一 content_hash。

发布只写不可变版本资源：不初始化 fixture、不执行任何工具、不创建 Run、不调用
模型。旧实现只有库内 publish_fixture，没有任何用户入口，于是引用 fixture 的
Workflow 只能发布、不能运行（创建期 WORKFLOW_FIXTURE_MISSING）。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from motte_cli.workflows import WorkflowDocumentError, load_document

#: 与 Workflow 共用同一形状的结构化失败（稳定 code + 逐字段错误）。
FixtureDocumentError = WorkflowDocumentError

__all__ = ["FixtureDocumentError", "load_document", "publication_record"]


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
    """把文档规范化为可发布的 FixtureSpec 记录（与 API 发布同一规则）。

    草稿被拒绝（版本仓库只接受已发布内容）；content_hash 由本函数固定，客户端
    提交不一致的 hash 立即拒绝；published_at 缺失时补当前时刻并记入 defaulted。
    """
    if not isinstance(document, dict):
        raise FixtureDocumentError("CONTRACT_INVALID", "fixture document must be a JSON object")
    managed = sorted(set(document).intersection({"_deleted"}))
    if managed:
        raise FixtureDocumentError(
            "SERVER_MANAGED_FIELD", f"server-managed fields are not accepted: {managed}"
        )
    from motte_sdk.resolve import find_secret_paths

    leaked = find_secret_paths(document)
    if leaked:
        raise FixtureDocumentError(
            "CREDENTIALS_REJECTED",
            f"plaintext credential fields are not accepted: {leaked}",
        )
    from motte_contracts.fixture import FixtureSpec, fixture_content_hash
    from pydantic import ValidationError

    defaulted: list[str] = []
    candidate = dict(document)
    if not candidate.get("published_at"):
        candidate["published_at"] = published_at or _utc_now()
        defaulted.append("published_at")
    if candidate.get("lifecycle", "published") != "published":
        raise FixtureDocumentError(
            "FIXTURE_NOT_PUBLISHED",
            "only published fixture versions can be stored; a draft has no identity to freeze",
        )
    try:
        fixture = FixtureSpec.model_validate(candidate)
    except ValidationError as error:
        raise FixtureDocumentError(
            "FIXTURE_INVALID",
            f"fixture document is not a publishable version: {error.error_count()} field error(s)",
            fields=_field_errors(error),
        ) from error
    computed = fixture_content_hash(fixture)
    if fixture.content_hash is not None and fixture.content_hash != computed:
        raise FixtureDocumentError(
            "FIXTURE_CONTENT_HASH_MISMATCH",
            "fixture content_hash does not match its content; omit it to let the tool pin it",
        )
    return {**fixture.model_dump(mode="json"), "content_hash": computed}, defaulted


def publish_fixture(resources: Any, document: Any) -> dict[str, Any]:
    """规范并发布一个 FixtureSpec（同内容幂等，异内容冲突）。"""
    record, _defaulted = publication_record(document)
    return resources.publish_fixture(record)
