"""Workflow 资源与 scenario 入口的 CLI 实现（M5-T11）。

与 API 共用**同一个事实源**：motte_contracts.workflow 的 WorkflowVersion 契约
与 motte_scenario.compiler 的纯编译器。API 侧路由在 apps/api/app/main.py 的
"M5 workflow 资源"段；两侧对同一文档的接受/拒绝集合由
tests/cli/test_scenario_skill_cli.py 的等价性测试固定（同一 code、同一逐字段错误）。

本模块不执行任何 Workflow 步骤、不创建 Run、不调用模型：预检与转换都是纯函数。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class WorkflowDocumentError(ValueError):
    """CLI 侧结构化失败：稳定 code + 可选逐字段错误（与 API 错误体同形）。"""

    def __init__(
        self, code: str, message: str, *, fields: list[dict[str, str]] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.fields = list(fields or ())

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.fields:
            payload["fields"] = self.fields
        return payload


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


def load_document(path: str) -> dict[str, Any]:
    """读取一份 JSON 文档；YAML 不在依赖内，因此只接受 JSON（NOT_JSON 具名拒绝）。"""
    try:
        # utf-8-sig：Windows 编辑器大概率写 BOM，BOM 不是文档内容错误。
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as error:
        raise WorkflowDocumentError("DOCUMENT_UNREADABLE", f"cannot read document: {error}") from error
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise WorkflowDocumentError(
            "DOCUMENT_NOT_JSON",
            f"document is not valid JSON (line {error.lineno} column {error.colno}); "
            "YAML is not supported without an explicit converter",
        ) from error
    if not isinstance(payload, dict):
        raise WorkflowDocumentError("DOCUMENT_NOT_OBJECT", "document must be a JSON object")
    return payload


def publication_record(
    document: Any, *, published_at: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """把文档规范化为可发布的 WorkflowVersion 记录（与 API 发布/预检同一规则）。

    草稿在契约层被拒绝；内容 hash 由本地固定（发布时资源仓库再核验一次）；
    published_at 缺失时补当前时刻并记入 defaulted_fields。
    """
    if not isinstance(document, dict):
        raise WorkflowDocumentError("CONTRACT_INVALID", "workflow document must be a JSON object")
    managed = sorted(set(document).intersection({"_deleted"}))
    if managed:
        raise WorkflowDocumentError(
            "SERVER_MANAGED_FIELD", f"server-managed fields are not accepted: {managed}"
        )
    from motte_sdk.resolve import find_secret_paths

    leaked = find_secret_paths(document)
    if leaked:
        raise WorkflowDocumentError(
            "CREDENTIALS_REJECTED",
            f"plaintext credential fields are not accepted: {leaked}",
        )
    from motte_contracts.workflow import WorkflowVersion, workflow_content_hash
    from pydantic import ValidationError

    defaulted: list[str] = []
    candidate = dict(document)
    if not candidate.get("published_at"):
        candidate["published_at"] = published_at or _utc_now()
        defaulted.append("published_at")
    try:
        workflow = WorkflowVersion.model_validate(candidate)
    except ValidationError as error:
        raise WorkflowDocumentError(
            "WORKFLOW_INVALID",
            f"workflow document is not a publishable version: {error.error_count()} field error(s)",
            fields=_field_errors(error),
        ) from error
    computed = workflow_content_hash(workflow)
    if workflow.content_hash is not None and workflow.content_hash != computed:
        raise WorkflowDocumentError(
            "WORKFLOW_CONTENT_HASH_MISMATCH",
            "workflow content_hash does not match its content; omit it to let the tool pin it",
        )
    return {**workflow.model_dump(mode="json"), "content_hash": computed}, defaulted


def preflight_report(document: Any, *, resources: Any = None) -> dict[str, Any]:
    """纯预检：编译 Workflow 并返回与 API /api/v1/workflows/validate 同形的结果。"""
    from motte_contracts.workflow import WorkflowVersion
    from motte_scenario.compiler import WorkflowResolutionError, compile_workflow

    record, defaulted = publication_record(document)
    try:
        compiled = compile_workflow(WorkflowVersion.model_validate(record), resources=resources)
    except WorkflowResolutionError as error:
        raise WorkflowDocumentError(error.code, str(error)) from error
    return {
        "ok": True,
        "publishable": True,
        "executed": False,
        "workflow_id": compiled.workflow_id,
        "version": compiled.version,
        "ref": compiled.ref,
        "schema_version": compiled.schema_version,
        "content_hash": compiled.content_hash,
        "step_count": len(compiled.steps),
        "top_level_step_ids": [step.step_id for step in compiled.top_level_steps],
        "condition_count": len(compiled.conditions),
        "target_requirements": compiled.target_requirements.model_dump(mode="json"),
        "limits": compiled.limits.model_dump(mode="json"),
        "fixture_refs": [ref.model_dump(mode="json") for ref in compiled.fixture_refs],
        "defaulted_fields": defaulted,
    }


def publish_workflow(resources: Any, document: Any) -> dict[str, Any]:
    """发布一份已校验的 WorkflowVersion；同内容幂等，同版本异内容冲突。"""
    from motte_storage.resource_store import ResourceConflictError

    record, _defaulted = publication_record(document)
    try:
        return resources.publish_workflow(record)
    except ResourceConflictError:
        existing = resources.workflows.get(record["workflow_id"], record["version"])
        comparison = dict(record)
        if "published_at" not in document and existing is not None:
            comparison["published_at"] = existing.get("published_at")
        if existing != comparison:
            raise
        return existing


def conversion_report(
    legacy: Any,
    *,
    source: dict[str, Any] | None = None,
    workflow_id: str | None = None,
    version: str | None = None,
    published_at: str | None = None,
) -> dict[str, Any]:
    """旧 DSL 只读转换报告；不发布、不执行，runs_executed 恒为 0。"""
    from motte_scenario.conversion import ConversionIncompleteError, convert_legacy_scenario

    if not isinstance(legacy, dict):
        raise WorkflowDocumentError("LEGACY_DOCUMENT_REQUIRED", "legacy document must be a JSON object")
    result = convert_legacy_scenario(
        legacy, source=source, workflow_id=workflow_id, version=version
    )
    diagnostics = result.diagnostic_dicts()
    publishable = result.publishable
    candidate: dict[str, Any] | None = None
    if publishable:
        stamp = published_at or _utc_now()
        try:
            workflow = result.to_publishable_workflow(published_at=stamp)
        except ConversionIncompleteError as error:
            publishable = False
            known = {json.dumps(item, sort_keys=True) for item in diagnostics}
            for item in error.diagnostics:
                marker = json.dumps(item, sort_keys=True)
                if marker not in known:
                    diagnostics.append(item)
        else:
            from motte_contracts.workflow import workflow_content_hash

            candidate = {
                "ref": f"{workflow.workflow_id}@{workflow.version}",
                "content_hash": workflow_content_hash(workflow),
            }
    return {
        "publishable": publishable,
        "published": False,
        "executed": False,
        "runs_executed": result.runs_executed,
        "blocking_codes": list(result.blocking_codes),
        "diagnostics": diagnostics,
        "mapping": [dict(item) for item in result.mapping],
        "workflow_draft": result.workflow,
        "fixture_draft": result.fixture_draft,
        "source": dict(result.source or {}),
        "candidate": candidate,
    }


def scenario_targets() -> list[dict[str, Any]]:
    """已注册 target 及其真实能力；注册表为空即返回空列表（不猜测能力）。

    验收 F-09：内置 target adapter 是 motte_sdk.scenario_backend 的导入副作用
    （与 API / Worker 同一条注册路径）。以前这里只 import describe_targets，于是
    同一个 CLI 里 scenario targets 报「没有注册的 target」、而 scenario run 却能
    创建 Run——列子命令必须装配同一个注册入口，不能自成一个更空的世界。
    """
    try:  # pragma: no cover - motte-agent 缺失时该目标保持不可用
        import motte_sdk.scenario_backend  # noqa: F401
    except ImportError:
        pass
    from motte_scenario.targets import describe_targets

    return describe_targets()


def published_document(resources: Any, reference: str) -> dict[str, Any]:
    """按 name@version 读取已发布 Workflow（预检已发布版本的只读入口）。"""
    name, separator, version = str(reference).rpartition("@")
    if not separator or not name or not version:
        raise WorkflowDocumentError(
            "WORKFLOW_REF_INVALID", f"workflow reference must be name@version: {reference!r}"
        )
    record = resources.workflows.get(name, version)
    if record is None:
        raise WorkflowDocumentError("WORKFLOW_NOT_FOUND", f"workflow not found: {reference}")
    return record

