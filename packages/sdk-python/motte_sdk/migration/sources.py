"""受控来源导出包的加载与安全边界（M7-T06；协议 §5.2，验收 A11）。

来源包 = 操作者显式提供的受控目录：``manifest.json`` +
``records/<type>/<id>.json`` + ``artifacts/<sha256[:2]>/<sha256>``。

安全边界（在读取/解析任何 record 内容的过程中强制）：

- 拒绝绝对路径引用（POSIX/Windows 形态都算）、``..`` 段、包内任何位置的
  symlink（含中间目录；``os.walk(followlinks=False)`` 逐项 islink 检查）；
  每个被打开的文件都先 resolve 再验证仍在包根之下，绝不读包外路径。
- 配额：文件数 ≤ 20k、未压缩总量 ≤ 512MiB、单 record ≤ 4MiB、record 总数
  ≤ 10k（协议原文针对 zip 解压炸弹；目录包执行同一防线）。
- manifest.json 按 ImportManifest 契约 fail-closed 解析（extra=forbid），
  解析失败映射为带 code 的 SourcePackageError。
- 许可证：携带 license 字段的 record 取值必须已知；"unknown" 或缺失
  （dataset/scenario 必填）→ restricted + 警告，不阻断加载。
- secret 形状字段（key 命中 api_key/token/secret/password/authorization 且
  值不是引用形状：非 ``env:NAME`` / 环境变量名形状 / 显式脱敏标记）→ 该
  record 整条拒绝，诊断 ``secret_field_rejected:<字段路径>`` 只携带字段名，
  永不携带字段值。

content_sha256 取值约定（协议 §5.1 "整个来源包的内容 hash"）：对包内每个
文件取 ``(posix relpath, sha256(bytes))`` 二元组、按 relpath 排序后做
``canonical_sha256``；manifest.json 自身参与哈希但排除其 content_sha256
字段（避免自指）。apply 前用同一函数对磁盘重算复验（协议 §5.2）。
manifest 声明的 content_sha256 与实际不符时只降级为警告并采用重算值：
来源被篡改/变更的情形由 apply 复验与单元级 conflict 收口，不能在加载处
把"重新 dry-run 一个已变更的来源"堵死。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from motte_contracts.imports import (
    KNOWN_LICENSES,
    LICENSE_REQUIRED_TYPES,
    ImportManifest,
    mapping_key_for,
)
from motte_contracts.identity import canonical_json_bytes, canonical_sha256

#: 配额（协议 §5.2）。
MAX_PACKAGE_FILES = 20_000
MAX_PACKAGE_BYTES = 512 * 1024 * 1024
MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_RECORDS = 10_000

#: record 数据里按路径解释、需要做穿越/绝对路径检查的字段名。
PATH_REF_KEYS: frozenset[str] = frozenset({
    "path", "relpath", "file", "artifact_path", "source_path", "record_path", "ref",
})

_SECRET_KEY = re.compile(r"(api_key|token|secret|password|authorization)", re.IGNORECASE)
#: 引用形状：``env:NAME``、环境变量名形状（大写/数字/下划线）。
_REF_VALUE = re.compile(r"^(env:)?[A-Z][A-Z0-9_]*$")
#: 显式脱敏标记：不携带任何 secret 材料。
_SAFE_MARKERS = frozenset({"", "unknown", "none", "null", "redacted", "[redacted]"})
#: 键名后缀表明自身就是引用（api_key_env 等），不是 secret 值。
_REF_KEY_SUFFIXES = ("_env", "_ref", "_name", "_profile")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class SourcePackageError(ValueError):
    """来源包不可接受；``code`` 供上层映射为结构化诊断。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class SourceRecord:
    """一条通过安全检查、可进入计划/导入的来源记录。"""

    record_type: str
    source_id: str
    source_version: str
    relpath: str
    data: dict[str, Any]
    content_hash: str
    mapping_key: str

    def unit_ref(self) -> str:
        return f"{self.record_type}/{self.source_id}"


@dataclass(frozen=True)
class RejectedSourceRecord:
    """加载期即被拒绝的来源记录（当前唯一来源：secret 形状字段）。"""

    record_type: str
    source_id: str
    source_version: str
    content_hash: str
    mapping_key: str
    diagnostic: str

    def unit_ref(self) -> str:
        return f"{self.record_type}/{self.source_id}"


@dataclass
class LoadedSource:
    """解析完成的来源包。"""

    root: Path
    manifest: ImportManifest
    content_sha256: str
    records: list[SourceRecord] = field(default_factory=list)
    rejected: list[RejectedSourceRecord] = field(default_factory=list)
    artifact_files: dict[str, Path] = field(default_factory=dict)
    restricted: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def records_of(self, record_type: str) -> list[SourceRecord]:
        return [record for record in self.records if record.record_type == record_type]

    def find(self, record_type: str, source_id: str) -> SourceRecord | None:
        for record in self.records:
            if record.record_type == record_type and record.source_id == source_id:
                return record
        return None

    def declared_artifact_shas(self) -> dict[str, list[str]]:
        """声明过的 artifact 内容 hash → 声明它的单元引用列表。

        artifact 记录的 ``sha256`` 与 run 记录 ``artifacts[].sha256`` 都算声明；
        声明了但包内没有对应文件 → dry-run 的 missing_artifacts。
        """
        declared: dict[str, list[str]] = {}

        def _declare(sha: Any, unit_ref: str) -> None:
            if isinstance(sha, str) and sha:
                declared.setdefault(sha, []).append(unit_ref)

        for record in self.records:
            if record.record_type == "artifact":
                _declare(record.data.get("sha256"), record.unit_ref())
            elif record.record_type == "run":
                entries = record.data.get("artifacts")
                if isinstance(entries, list):
                    for entry in entries:
                        if isinstance(entry, dict):
                            _declare(entry.get("sha256"), record.unit_ref())
        return declared


def _is_unsafe_relative(value: str) -> bool:
    """绝对路径（含 POSIX/Windows 形态）或含 ``..`` 段的引用都拒绝。"""
    if _WINDOWS_DRIVE.match(value) or value.startswith(("/", "\\")):
        return True
    if os.path.isabs(value):
        return True
    return ".." in Path(value).parts


def _scan_path_refs(node: Any, prefix: str = "") -> list[str]:
    """递归收集 PATH_REF_KEYS 字段中不安全的引用位置（只报字段路径）。"""
    hits: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            location = f"{prefix}.{key}" if prefix else str(key)
            if key in PATH_REF_KEYS and isinstance(value, str) and _is_unsafe_relative(value):
                hits.append(location)
            hits.extend(_scan_path_refs(value, location))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            hits.extend(_scan_path_refs(value, f"{prefix}[{index}]"))
    return hits


def _scan_secret_fields(node: Any, prefix: str = "") -> list[str]:
    """递归收集 secret 形状字段位置（只报字段路径，绝不携带值）。"""
    hits: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            location = f"{prefix}.{key}" if prefix else str(key)
            if (
                isinstance(value, str)
                and _SECRET_KEY.search(str(key))
                and not str(key).endswith(_REF_KEY_SUFFIXES)
                and value not in _SAFE_MARKERS
                and not _REF_VALUE.match(value)
            ):
                hits.append(location)
            else:
                hits.extend(_scan_secret_fields(value, location))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            hits.extend(_scan_secret_fields(value, f"{prefix}[{index}]"))
    return hits


def _license_state(record_type: str, unit_ref: str, data: dict[str, Any]) -> tuple[bool, str | None]:
    """返回 (是否 restricted, 警告文案或 None)。"""
    if "license" in data:
        value = data.get("license")
        normalized = value.strip().lower() if isinstance(value, str) else ""
        if normalized == "unknown":
            return True, f"license unknown on {unit_ref}: marked restricted"
        if normalized not in KNOWN_LICENSES:
            return True, f"unknown license value on {unit_ref}: marked restricted"
        return False, None
    if record_type in LICENSE_REQUIRED_TYPES:
        return True, f"missing license on {unit_ref}: marked restricted"
    return False, None


def _walk_files(root: Path) -> list[tuple[str, Path]]:
    """按 relpath 排序收集包内文件；强制 symlink/配额/越界检查。"""
    root_resolve = root.resolve()
    collected: list[tuple[str, Path]] = []
    total_bytes = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        filenames.sort()
        for name in dirnames:
            if os.path.islink(os.path.join(dirpath, name)):
                raise SourcePackageError(
                    "symlink_in_package", f"symlinked directory in package: {name}"
                )
        for name in filenames:
            full = Path(dirpath) / name
            if full.is_symlink():
                raise SourcePackageError(
                    "symlink_in_package", f"symlinked file in package: {name}"
                )
            rel = full.relative_to(root).as_posix()
            resolved = full.resolve()
            if root_resolve not in resolved.parents:
                raise SourcePackageError(
                    "path_escape", f"package member escapes the package root: {rel}"
                )
            total_bytes += resolved.stat().st_size
            collected.append((rel, full))
    if len(collected) > MAX_PACKAGE_FILES:
        raise SourcePackageError(
            "too_many_files", f"package has {len(collected)} files; limit {MAX_PACKAGE_FILES}"
        )
    if total_bytes > MAX_PACKAGE_BYTES:
        raise SourcePackageError(
            "package_too_large", f"package unpacked size {total_bytes} exceeds limit"
        )
    return collected


def package_content_sha256(root: str | Path) -> str:
    """整个来源包的内容 hash（协议 §5.2 apply 前复验用同一函数）。

    见模块 docstring 的取值约定：排序后的 (relpath, sha256) 二元组做
    canonical_sha256；manifest.json 排除自身 content_sha256 字段后参与。
    """
    root = Path(root)
    if not root.is_dir():
        raise SourcePackageError("package_not_found", f"source package not found: {root}")
    entries: list[list[str]] = []
    for rel, full in _walk_files(root):
        if rel == "manifest.json":
            data = json.loads(full.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.pop("content_sha256", None)
            digest = hashlib.sha256(canonical_json_bytes(data)).hexdigest()
        else:
            digest = hashlib.sha256(full.read_bytes()).hexdigest()
        entries.append([rel, digest])
    return canonical_sha256(entries)


def load_source_package(path: str | Path) -> LoadedSource:
    """加载并校验一个受控来源导出包；任何违规抛 SourcePackageError。"""
    root = Path(path)
    if not root.is_dir():
        raise SourcePackageError("package_not_found", f"source package not found: {root}")
    files = _walk_files(root)
    by_rel = dict(files)

    manifest_path = root / "manifest.json"
    if "manifest.json" not in by_rel:
        raise SourcePackageError("manifest_missing", "manifest.json is missing")
    try:
        manifest = ImportManifest.model_validate(
            json.loads(manifest_path.read_text(encoding="utf-8"))
        )
    except ValidationError as error:
        raise SourcePackageError(
            "invalid_manifest", f"manifest.json failed contract validation: {error}"
        ) from error
    except json.JSONDecodeError as error:
        raise SourcePackageError(
            "invalid_manifest", f"manifest.json is not valid JSON: {error}"
        ) from error

    for scope_entry in manifest.scope:
        if _is_unsafe_relative(scope_entry):
            raise SourcePackageError(
                "unsafe_path_reference", f"unsafe manifest scope entry: {scope_entry}"
            )

    # ---- records -----------------------------------------------------------
    record_files = [(rel, full) for rel, full in files if rel.startswith("records/")]
    if len(record_files) > MAX_RECORDS:
        raise SourcePackageError(
            "too_many_records", f"package has {len(record_files)} records; limit {MAX_RECORDS}"
        )

    loaded: LoadedSource = LoadedSource(
        root=root, manifest=manifest, content_sha256=package_content_sha256(root)
    )
    if manifest.content_sha256 != loaded.content_sha256:
        loaded.warnings.append(
            "declared manifest content_sha256 differs from computed value; using computed"
        )

    # ---- artifacts ---------------------------------------------------------
    artifact_entries: list[list[str]] = []
    for rel, full in files:
        if rel.startswith("artifacts/"):
            digest = hashlib.sha256(full.read_bytes()).hexdigest()
            artifact_entries.append([rel, digest])
            parts = rel.split("/")
            if len(parts) == 3 and _HEX64.match(parts[2]):
                # 按路径声明的 sha 索引；内容与声明是否一致由 apply 暂存校验
                # （协议 §5.3：hash 不匹配 → 该 record 拒绝，不提交引用）。
                loaded.artifact_files[parts[2]] = full
    computed_artifact_manifest = canonical_sha256(artifact_entries)
    if artifact_entries and computed_artifact_manifest != manifest.artifact_manifest_sha256:
        loaded.warnings.append(
            "declared artifact_manifest_sha256 differs from computed artifact listing"
        )

    # ---- record files ------------------------------------------------------
    for rel, full in record_files:
        parts = rel.split("/")
        if len(parts) != 3 or not parts[1] or not parts[2].endswith(".json"):
            raise SourcePackageError(
                "record_path_invalid", f"record files must be records/<type>/<id>.json: {rel}"
            )
        record_type, stem = parts[1], parts[2][: -len(".json")]
        size = full.stat().st_size
        if size > MAX_RECORD_BYTES:
            raise SourcePackageError(
                "record_too_large", f"record exceeds 4MiB limit: {rel} ({size} bytes)"
            )
        try:
            data = json.loads(full.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise SourcePackageError(
                "invalid_record", f"record is not valid JSON: {rel}: {error}"
            ) from error
        if not isinstance(data, dict):
            raise SourcePackageError("invalid_record", f"record must be a JSON object: {rel}")

        unsafe = _scan_path_refs(data)
        if unsafe:
            raise SourcePackageError(
                "unsafe_path_reference", f"unsafe path reference in {rel}: {unsafe[0]}"
            )

        declared_id = data.get("id")
        source_id = declared_id if isinstance(declared_id, str) and declared_id else stem
        if declared_id is not None and declared_id != source_id:
            loaded.warnings.append(f"record id differs from file name: {rel}")
        declared_version = data.get("version")
        source_version = (
            declared_version if isinstance(declared_version, str) and declared_version else "1"
        )
        content_hash = canonical_sha256(data)
        mapping_key = mapping_key_for(
            manifest.source_system, record_type, source_id, source_version,
            manifest.transform_version,
        )

        secret_fields = _scan_secret_fields(data)
        if secret_fields:
            loaded.rejected.append(RejectedSourceRecord(
                record_type=record_type, source_id=source_id, source_version=source_version,
                content_hash=content_hash, mapping_key=mapping_key,
                diagnostic=f"secret_field_rejected:{secret_fields[0]}",
            ))
            continue

        restricted, warning = _license_state(record_type, f"{record_type}/{source_id}", data)
        if restricted:
            loaded.restricted.append(f"{record_type}/{source_id}")
        if warning:
            loaded.warnings.append(warning)

        loaded.records.append(SourceRecord(
            record_type=record_type, source_id=source_id, source_version=source_version,
            relpath=rel, data=data, content_hash=content_hash, mapping_key=mapping_key,
        ))

    known_types = {record.record_type for record in loaded.records}
    unknown_scopes = [entry for entry in manifest.scope if entry not in known_types]
    for entry in unknown_scopes:
        loaded.warnings.append(f"manifest scope entry has no records in package: {entry}")
    return loaded
