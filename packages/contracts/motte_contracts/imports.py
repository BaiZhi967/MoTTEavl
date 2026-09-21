"""历史迁移契约（M7，协议 docs/protocols/sdk-and-migration.md frozen@1 §5.1）。

ImportManifest 描述一个受控来源导出包；SourceIdentity + ``mapping_key_for``
是跨系统幂等导入的唯一身份；MappingRecord 是导入账本
（``motte_storage.platform.ImportLedger``）每行 payload 的规范形状；
ImportReport 是 dry-run / apply / 对账共用的只读结果。

所有契约 ``extra=forbid``：manifest 未知字段 fail closed；record 里的未知
字段由迁移层收进 ImportReport.unknown_fields 显式登记，绝不静默丢弃
（协议 §5.2）。
"""
from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import Field, field_validator

from .hashing import canonical_hash
from .messages import Contract

#: 单元状态（协议 §5.1）：映射行/报告里单条来源记录的结局。
MAPPING_STATUSES: frozenset[str] = frozenset({
    "planned", "created", "reused", "conflicted", "rejected", "rolled_back",
})

MappingStatus = Literal[
    "planned", "created", "reused", "conflicted", "rejected", "rolled_back"
]

#: 批次状态（motte_imports.status；协议 §5.4 rolled_back / inactive 皆合法终态）。
IMPORT_BATCH_STATUSES: frozenset[str] = frozenset({
    "planned", "applying", "applied", "rolled_back", "inactive",
})

#: 来源包可声明的已知许可证；"unknown" 合法但标记 restricted（协议 §5.2）。
KNOWN_LICENSES: frozenset[str] = frozenset({
    "mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause", "isc", "mpl-2.0",
    "cc0-1.0", "cc-by-4.0", "cc-by-sa-4.0", "proprietary", "unknown",
})

#: 必须携带许可证字段的 record 类型；缺失即 restricted + 警告（协议 §5.2）。
LICENSE_REQUIRED_TYPES: frozenset[str] = frozenset({"dataset", "scenario"})


def mapping_key_for(
    source_system: str, record_type: str, source_id: str,
    source_version: str, transform_version: str,
) -> str:
    """协议 §5.4 唯一 key。

    sha256(source_system|record_type|source_id|source_version|transform_version)；
    纯十六进制（这是账本主键，不是内容 hash，不加 ``sha256:`` 前缀）。
    """
    material = "|".join(
        (source_system, record_type, source_id, source_version, transform_version)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ImportManifest(Contract):
    """来源导出包清单（manifest.json）。"""

    import_id: str = Field(min_length=1)
    importer_version: str = Field(min_length=1)
    transform_version: str = Field(min_length=1)
    source_system: str = Field(min_length=1)
    source_repository: str | None = None
    source_revision: str | None = None
    source_schema_version: str = Field(min_length=1)
    exported_at: str = Field(min_length=1)
    record_counts: dict[str, int] = Field(default_factory=dict)
    artifact_manifest_sha256: str = Field(min_length=1)
    scope: tuple[str, ...] = ()
    #: 整个来源包的内容 hash；apply 前用同一算法重算复验（协议 §5.2）。
    content_sha256: str = Field(min_length=1)

    def manifest_hash(self) -> str:
        """批次绑定 hash：排除自指的 content_sha256 字段的 canonical hash。

        begin_import 用它判定"同 import_id 是否换了 manifest"：record 文件
        变化不改批次绑定（那属于单元级 conflict 的正确语义），manifest 自身
        （transform_version / scope / 来源 revision 等）变化才冲突。
        """
        payload = self.model_dump(mode="json")
        payload.pop("content_sha256")
        return canonical_hash(payload)


class SourceIdentity(Contract):
    """来源记录身份 + 内容 hash（内容变即身份变）。"""

    source_system: str = Field(min_length=1)
    source_record_type: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_version: str = "1"
    content_hash: str = Field(min_length=1)


class MappingRecord(Contract):
    """一条导入映射（导入账本行的规范形状）。

    账本行写入时以此契约校验核心字段后，可再合并 operator/committed_at
    等审计扩展字段（协议 §5.4 审计要求），账本 payload 是其超集。
    """

    mapping_key: str = Field(min_length=1)
    import_id: str = Field(min_length=1)
    source: SourceIdentity
    target_type: str = ""
    target_id: str = ""
    status: MappingStatus = "planned"
    diagnostics: tuple[str, ...] = ()

    @field_validator("status")
    @classmethod
    def _status_must_be_known(cls, value: MappingStatus) -> MappingStatus:
        if value not in MAPPING_STATUSES:
            raise ValueError(
                f"unknown mapping status: {value!r} "
                f"(known: {','.join(sorted(MAPPING_STATUSES))})"
            )
        return value


class ImportVerification(Contract):
    """apply 完成后的对账结论（协议 §5.4）。dry-run 阶段保持缺省 False。"""

    counts_match: bool = False
    hashes_match: bool = False
    references_ok: bool = False


class ImportReport(Contract):
    """dry-run / apply / 对账共用的只读结果（协议 §5.1）。

    planned 是**全部**来源单元数（对账恒等式：planned == created + reused +
    rejected + conflicted + pending；apply 完成后 pending 为 0）。
    credentials_to_rebind 只携带引用名（env 名/credentials profile 名），
    永不携带 secret 值（协议 §5.2）。
    """

    import_id: str = Field(min_length=1)
    source_system: str = Field(min_length=1)
    content_sha256: str = Field(min_length=1)
    planned: int = Field(default=0, ge=0)
    created: int = Field(default=0, ge=0)
    reused: int = Field(default=0, ge=0)
    rejected: int = Field(default=0, ge=0)
    conflicted: int = Field(default=0, ge=0)
    pending: int = Field(default=0, ge=0)
    missing_artifacts: tuple[str, ...] = ()
    unknown_fields: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    credentials_to_rebind: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    checkpoint: int = Field(default=0, ge=0)
    verification: ImportVerification = Field(default_factory=ImportVerification)
    notes: tuple[str, ...] = ()
