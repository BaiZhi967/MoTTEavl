"""M5-T06：Skill 源文件的安全导入（只读取与校验，绝不执行任何东西）。

导入器不解压到工作区、不运行安装钩子、不下载依赖、不执行入口、不 import 资源；
它只把归档/目录读成有界的内存字节，逐文件分类、限额、算 hash，然后产出一个
SkillDraft 与导入报告（来源 + 许可证）。资源字节由调用方显式 ingest 到内容寻址
store（content_store.FileContentStore / ContentAddressedMemoryStore）；非空资源
清单的发布必须有该存储，publish_skill 会再次核验字节与依赖 pin。

每个拒绝都有独立错误码（SkillImportCode.*），便于 API/CLI 给出稳定诊断。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import zlib
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

# 内容寻址存储的唯一实现在 content_store；这里保留旧导入路径
# (motte_skill.importer.ContentAddressedMemoryStore) 只是兼容别名。
from .content_store import ContentAddressedMemoryStore as ContentAddressedMemoryStore
from .versions import (
    SkillDependency,
    SkillDraft,
    SkillResource,
    is_exact_pin,
)

SKILL_MANIFEST_NAME = "skill.json"
IMPORTER_VERSION = "motte-skill-importer@1"

MAX_RESOURCE_FILES = 256
MAX_RESOURCE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_RESOURCE_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100.0
MIN_RATIO_CHECK_BYTES = 64 * 1024
MAX_PATH_DEPTH = 16

_MANIFEST_FIELDS = frozenset({
    "skill_id", "version", "kind", "description", "licence", "license",
    "instruction", "instruction_ref", "resources", "dependencies",
    "requested_permissions", "input_schema", "output_schema", "fixture_refs",
    "injection_mode", "entrypoint",
})
_DEFAULT_INJECTION_MODE = {
    "instruction": "system-prompt",
    "instruction_with_resources": "context-section",
    "executable": "none",
}
_RESOURCE_FIELDS = frozenset({"path", "executable", "sha256"})

_VENDORED_DIRS = frozenset({
    "node_modules", ".venv", "venv", "virtualenv", "site-packages", "dist-packages",
    "__pypackages__", ".tox", "bower_components", ".pnpm-store", ".yarn",
})
_CREDENTIAL_DIRS = frozenset({".ssh", ".aws", ".azure", ".gnupg", ".kube", ".docker"})
_CREDENTIAL_NAMES = frozenset({
    ".env", ".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials", ".dockercfg",
    ".htpasswd", "credentials", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "kubeconfig",
})
_CREDENTIAL_SUFFIXES = (".pem", ".key", ".pfx", ".p12", ".jks", ".keystore", ".ppk")
_EXECUTABLE_SUFFIXES = (
    ".sh", ".bash", ".ps1", ".bat", ".cmd", ".exe", ".dll", ".so", ".dylib",
    ".com", ".msi", ".run", ".jar",
)
_INSTALL_HOOK_NAMES = frozenset({"setup.py", "postinstall.js", "preinstall.js", "install.js"})

_TEXT_MEDIA_TYPES = {
    ".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain",
    ".json": "application/json", ".jsonl": "application/x-ndjson",
    ".yaml": "application/yaml", ".yml": "application/yaml", ".toml": "application/toml",
    ".csv": "text/csv", ".tsv": "text/tab-separated-values", ".py": "text/x-python",
    ".js": "text/javascript", ".mjs": "text/javascript", ".cjs": "text/javascript",
    ".ts": "text/typescript", ".sh": "text/x-shellscript", ".ps1": "text/x-powershell",
    ".bat": "text/x-batch", ".html": "text/html", ".htm": "text/html", ".css": "text/css",
    ".sql": "application/sql", ".xml": "application/xml", ".ini": "text/plain",
    ".cfg": "text/plain", ".conf": "text/plain", ".diff": "text/x-diff",
    ".patch": "text/x-diff", ".rst": "text/x-rst", ".lua": "text/x-lua",
    ".rb": "text/x-ruby", ".pl": "text/x-perl", ".vbs": "text/x-vbscript",
}
_ARCHIVE_MAGIC = (
    (b"PK\x03\x04", "application/zip"),
    (b"PK\x05\x06", "application/zip"),
    (b"\x1f\x8b", "application/gzip"),
    (b"7z\xbc\xaf\x27\x1c", "application/x-7z-compressed"),
    (b"BZh", "application/x-bzip2"),
    (b"\xfd7zXZ", "application/x-xz"),
)
_NATIVE_BINARY_MAGIC = (
    (b"\x7fELF", "application/x-elf"),
    (b"MZ", "application/x-dosexec"),
    (b"\xca\xfe\xba\xbe", "application/x-mach-o"),
    (b"\xfe\xed\xfa\xce", "application/x-mach-o"),
    (b"\xfe\xed\xfa\xcf", "application/x-mach-o"),
    (b"\xcf\xfa\xed\xfe", "application/x-mach-o"),
)
_KNOWN_BINARY_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
    (b"OTTO", "font/otf"),
    (b"\x00\x01\x00\x00", "font/ttf"),
)
_ARCHIVE_MEDIA_TYPES = frozenset(media for _, media in _ARCHIVE_MAGIC)
_NATIVE_BINARY_MEDIA_TYPES = frozenset(media for _, media in _NATIVE_BINARY_MAGIC)
_DRIVE_LETTER = re.compile(r"^[A-Za-z]:")


class SkillImportCode:
    """导入拒绝的稳定错误码（一个拒绝一类，测试逐一覆盖）。"""

    SOURCE_MISSING = "source_missing"
    SOURCE_UNSUPPORTED = "source_unsupported"
    MANIFEST_MISSING = "manifest_missing"
    MANIFEST_INVALID = "manifest_invalid"
    LICENCE_MISSING = "licence_missing"
    PATH_TRAVERSAL = "path_traversal"
    SYMLINK_RESOURCE = "symlink_resource"
    TOO_MANY_FILES = "too_many_files"
    OVERSIZED_RESOURCE = "oversized_resource"
    OVERSIZED_EXTRACTION = "oversized_extraction"
    ZIP_BOMB_DECLARED_MISMATCH = "zip_bomb_declared_mismatch"
    ZIP_BOMB_RATIO = "zip_bomb_ratio"
    ARCHIVE_COMPRESSION_UNSUPPORTED = "archive_compression_unsupported"
    ARCHIVE_INVALID = "archive_invalid"
    NESTED_ARCHIVE = "nested_archive"
    UNKNOWN_BINARY = "unknown_binary"
    UNDECLARED_RESOURCE = "undeclared_resource"
    UNDECLARED_EXECUTABLE = "undeclared_executable"
    EXECUTABLE_NOT_PERMITTED = "executable_not_permitted"
    DECLARED_RESOURCE_MISSING = "declared_resource_missing"
    RESOURCE_HASH_MISMATCH = "resource_hash_mismatch"
    INSTRUCTION_REF_UNDECLARED = "instruction_ref_undeclared"
    UNPINNED_DEPENDENCY = "unpinned_dependency"
    CREDENTIALS_ARTIFACT = "credentials_artifact"
    VENDORED_DEPENDENCY = "vendored_dependency"


class SkillImportError(ValueError):
    """带稳定错误码的导入拒绝；消息包含路径以便定位。"""

    def __init__(self, code: str, message: str, *, path: str | None = None) -> None:
        detail = f"{code}: {message}"
        if path is not None:
            detail = f"{detail} [{path}]"
        super().__init__(detail)
        self.code = code
        self.path = path


@dataclass(frozen=True)
class ImportedResource:
    """已读取并校验的资源字节（真正按内容寻址发生在 ingest 时）。"""

    path: str
    sha256: str
    size_bytes: int
    media_type: str
    executable: bool
    data: bytes

    def manifest_entry(self) -> SkillResource:
        return SkillResource(
            path=self.path,
            sha256=self.sha256,
            size_bytes=self.size_bytes,
            media_type=self.media_type,
        )


@dataclass(frozen=True)
class SkillImportReport:
    """导入结果：来源与许可证 + 已校验资源 + 尚未发布的草稿。"""

    skill_id: str
    version: str
    kind: str
    licence: str
    provenance: Mapping[str, Any]
    resources: tuple[ImportedResource, ...]
    dependencies: tuple[SkillDependency, ...]
    draft: SkillDraft
    warnings: tuple[str, ...] = ()

    def manifest_entries(self) -> tuple[SkillResource, ...]:
        return tuple(resource.manifest_entry() for resource in self.resources)

    def resource_bytes(self) -> dict[str, bytes]:
        return {resource.sha256: resource.data for resource in self.resources}

    def to_draft(self) -> SkillDraft:
        return self.draft


@dataclass(frozen=True)
class _FoundFile:
    path: str
    data: bytes
    executable: bool


def import_skill(
    source: str | os.PathLike[str],
    *,
    licence: str | None = None,
    imported_at: str | None = None,
) -> SkillImportReport:
    """读取并校验一个 Skill 源（目录或 zip 归档），返回导入报告。

    只做读取与校验：不执行安装钩子、不下载依赖、不运行入口。
    """
    origin = Path(source)
    if not origin.exists():
        raise SkillImportError(SkillImportCode.SOURCE_MISSING, "skill source does not exist")
    if origin.is_dir():
        origin_type = "directory"
        files = _read_directory(origin)
    elif origin.is_file() and (
        origin.suffix.lower() == ".zip" or _magic(origin) == b"PK\x03\x04"
    ):
        origin_type = "archive"
        files = _read_archive(origin)
    else:
        raise SkillImportError(
            SkillImportCode.SOURCE_UNSUPPORTED, "skill source must be a directory or a zip archive"
        )
    resolved = origin.resolve()
    manifest = _read_manifest(files, licence=licence)
    install_hooks = _detect_install_hooks(files)
    skill_id = _require_text(manifest, "skill_id")
    version = _require_text(manifest, "version")
    kind = _require_text(manifest, "kind")
    if kind not in _DEFAULT_INJECTION_MODE:
        raise SkillImportError(
            SkillImportCode.MANIFEST_INVALID, f"unknown skill kind: {kind!r}"
        )
    licence_value = _licence_of(manifest, licence)
    dependencies = _read_dependencies(manifest)
    resources = _read_resources(files, manifest, kind=kind)
    if manifest.get("instruction_ref") is not None:
        declared = [resource.manifest_entry().path for resource in resources]
        if manifest["instruction_ref"] not in declared:
            raise SkillImportError(
                SkillImportCode.INSTRUCTION_REF_UNDECLARED,
                "instruction_ref must be one of the declared resources",
                path=str(manifest["instruction_ref"]),
            )
    draft = _build_draft(
        manifest,
        skill_id=skill_id,
        version=version,
        kind=kind,
        resources=resources,
        dependencies=dependencies,
    )
    provenance = {
        "origin_type": origin_type,
        "origin": str(resolved),
        "origin_name": resolved.name,
        "importer_version": IMPORTER_VERSION,
        "imported_at": _canonical_now(imported_at),
        "licence": licence_value,
        "install_hooks_detected": list(install_hooks),
    }
    warnings = tuple(
        f"install_hook_not_executed:{hook}" for hook in install_hooks
    )
    return SkillImportReport(
        skill_id=skill_id,
        version=version,
        kind=kind,
        licence=licence_value,
        provenance=provenance,
        resources=resources,
        dependencies=dependencies,
        draft=draft,
        warnings=warnings,
    )


def ingest_resources(store: Any, report: SkillImportReport) -> tuple[SkillResource, ...]:
    """把报告中的字节写入内容寻址 store；返回内容 hash 固定的清单条目。"""
    entries: list[SkillResource] = []
    for resource in report.resources:
        ref = store.put(resource.data)
        if ref != resource.sha256:
            raise SkillImportError(
                SkillImportCode.RESOURCE_HASH_MISMATCH,
                f"content store returned {ref} for {resource.sha256}",
                path=resource.path,
            )
        entries.append(resource.manifest_entry())
    return tuple(entries)


def _canonical_now(value: str | None) -> str:
    if value is None:
        return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return value


def _magic(path: Path, size: int = 4) -> bytes:
    with path.open("rb") as handle:
        return handle.read(size)


def _check_entry_name(name: str) -> str:
    if not isinstance(name, str) or not name or name != name.strip():
        raise SkillImportError(SkillImportCode.PATH_TRAVERSAL, "empty or padded resource path")
    if "\\" in name:
        raise SkillImportError(
            SkillImportCode.PATH_TRAVERSAL, "backslashes are not portable resource paths", path=name
        )
    if name.startswith("/") or _DRIVE_LETTER.match(name) is not None:
        raise SkillImportError(
            SkillImportCode.PATH_TRAVERSAL, "absolute resource paths are refused", path=name
        )
    segments = name.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise SkillImportError(
            SkillImportCode.PATH_TRAVERSAL,
            "resource path contains empty or traversal segments",
            path=name,
        )
    if len(segments) > MAX_PATH_DEPTH:
        raise SkillImportError(
            SkillImportCode.PATH_TRAVERSAL, "resource path is too deep", path=name
        )
    return name


def _check_forbidden_path(name: str) -> None:
    segments = [segment.lower() for segment in name.split("/")]
    for segment in segments:
        if segment in _VENDORED_DIRS:
            raise SkillImportError(
                SkillImportCode.VENDORED_DEPENDENCY,
                "vendored dependency directories are not skill resources",
                path=name,
            )
        if segment in _CREDENTIAL_DIRS:
            raise SkillImportError(
                SkillImportCode.CREDENTIALS_ARTIFACT,
                "host credential directories are not skill resources",
                path=name,
            )
    basename = segments[-1]
    if basename in _CREDENTIAL_NAMES or basename.startswith(".env"):
        raise SkillImportError(
            SkillImportCode.CREDENTIALS_ARTIFACT,
            "host credential files are not skill resources",
            path=name,
        )
    if basename.endswith(_CREDENTIAL_SUFFIXES):
        raise SkillImportError(
            SkillImportCode.CREDENTIALS_ARTIFACT,
            "key material is not a skill resource",
            path=name,
        )


def _check_limits(count: int, name: str, size: int) -> None:
    if count > MAX_RESOURCE_FILES:
        raise SkillImportError(
            SkillImportCode.TOO_MANY_FILES,
            f"skill source declares more than {MAX_RESOURCE_FILES} files",
        )
    if size > MAX_RESOURCE_BYTES:
        raise SkillImportError(
            SkillImportCode.OVERSIZED_RESOURCE,
            f"resource exceeds {MAX_RESOURCE_BYTES} bytes",
            path=name,
        )


def _read_directory(root: Path) -> dict[str, _FoundFile]:
    files: dict[str, _FoundFile] = {}
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in list(dirnames):
            if os.path.islink(Path(dirpath) / name):
                relative = (Path(dirpath) / name).relative_to(root).as_posix()
                raise SkillImportError(
                    SkillImportCode.SYMLINK_RESOURCE, "symlinked directories are refused", path=relative
                )
        for name in sorted(filenames):
            absolute = Path(dirpath) / name
            relative = absolute.relative_to(root).as_posix()
            if os.path.islink(absolute):
                raise SkillImportError(
                    SkillImportCode.SYMLINK_RESOURCE, "symlinked files are refused", path=relative
                )
            _check_entry_name(relative)
            data = absolute.read_bytes()
            _check_limits(len(files) + 1, relative, len(data))
            total += len(data)
            if total > MAX_TOTAL_RESOURCE_BYTES:
                raise SkillImportError(
                    SkillImportCode.OVERSIZED_EXTRACTION,
                    f"skill source extracts to more than {MAX_TOTAL_RESOURCE_BYTES} bytes",
                )
            executable = bool(absolute.stat().st_mode & 0o111)
            files[relative] = _FoundFile(relative, data, executable)
    return files


def _read_archive(path: Path) -> dict[str, _FoundFile]:
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise SkillImportError(
            SkillImportCode.OVERSIZED_EXTRACTION,
            f"archive exceeds {MAX_ARCHIVE_BYTES} bytes",
            path=path.name,
        )
    payload = path.read_bytes()
    try:
        with zipfile.ZipFile(path) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
    except zipfile.BadZipFile as error:
        raise SkillImportError(
            SkillImportCode.ARCHIVE_INVALID, "archive is not a readable zip file", path=path.name
        ) from error
    if len(infos) > MAX_RESOURCE_FILES:
        raise SkillImportError(
            SkillImportCode.TOO_MANY_FILES,
            f"archive declares more than {MAX_RESOURCE_FILES} files",
        )
    declared_total = sum(info.file_size for info in infos)
    if declared_total > MAX_TOTAL_RESOURCE_BYTES:
        raise SkillImportError(
            SkillImportCode.OVERSIZED_EXTRACTION,
            f"archive declares more than {MAX_TOTAL_RESOURCE_BYTES} uncompressed bytes",
        )
    files: dict[str, _FoundFile] = {}
    total = 0
    for info in infos:
        name = _check_entry_name(info.filename)
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise SkillImportError(
                SkillImportCode.SYMLINK_RESOURCE, "symlink entries are refused", path=name
            )
        data = _decompress_member(payload, info)
        _check_limits(len(files) + 1, name, len(data))
        total += len(data)
        if total > MAX_TOTAL_RESOURCE_BYTES:
            raise SkillImportError(
                SkillImportCode.OVERSIZED_EXTRACTION,
                f"archive extracts to more than {MAX_TOTAL_RESOURCE_BYTES} bytes",
            )
        files[name] = _FoundFile(name, data, bool(mode & 0o111))
    return files


def _decompress_member(payload: bytes, info: zipfile.ZipInfo) -> bytes:
    """不信任归档声明的尺寸：自己解压、边解边限流，随时可以放弃。"""
    declared = info.file_size
    if declared > MAX_RESOURCE_BYTES:
        raise SkillImportError(
            SkillImportCode.OVERSIZED_RESOURCE,
            f"resource exceeds {MAX_RESOURCE_BYTES} bytes",
            path=info.filename,
        )
    header = payload[info.header_offset : info.header_offset + 30]
    if len(header) < 30 or header[:4] != b"PK\x03\x04":
        raise SkillImportError(
            SkillImportCode.ARCHIVE_INVALID, "entry has no readable local header", path=info.filename
        )
    name_length = int.from_bytes(header[26:28], "little")
    extra_length = int.from_bytes(header[28:30], "little")
    start = info.header_offset + 30 + name_length + extra_length
    compressed_size = info.compress_size
    if compressed_size <= 0 and declared > 0:
        raise SkillImportError(
            SkillImportCode.ARCHIVE_INVALID,
            "streamed entries without a compressed size are refused",
            path=info.filename,
        )
    blob = payload[start : start + compressed_size]
    if info.compress_type == zipfile.ZIP_STORED:
        data = blob
    elif info.compress_type == zipfile.ZIP_DEFLATED:
        data = _inflate_bounded(blob, info.filename)
    else:
        raise SkillImportError(
            SkillImportCode.ARCHIVE_COMPRESSION_UNSUPPORTED,
            f"unsupported compression method {info.compress_type}",
            path=info.filename,
        )
    if len(data) != declared:
        raise SkillImportError(
            SkillImportCode.ZIP_BOMB_DECLARED_MISMATCH,
            f"entry declares {declared} bytes but expands to {len(data)}",
            path=info.filename,
        )
    if (
        declared >= MIN_RATIO_CHECK_BYTES
        and compressed_size > 0
        and declared / compressed_size > MAX_COMPRESSION_RATIO
    ):
        raise SkillImportError(
            SkillImportCode.ZIP_BOMB_RATIO,
            f"entry compression ratio exceeds {MAX_COMPRESSION_RATIO}",
            path=info.filename,
        )
    return data


def _inflate_bounded(blob: bytes, name: str) -> bytes:
    decompressor = zlib.decompressobj(-15)
    data = bytearray()
    try:
        data += decompressor.decompress(blob, MAX_RESOURCE_BYTES + 1)
        while decompressor.unconsumed_tail and len(data) <= MAX_RESOURCE_BYTES:
            data += decompressor.decompress(decompressor.unconsumed_tail, MAX_RESOURCE_BYTES + 1)
    except zlib.error as error:
        raise SkillImportError(
            SkillImportCode.ARCHIVE_INVALID, "entry payload is not valid deflate data", path=name
        ) from error
    if len(data) > MAX_RESOURCE_BYTES:
        raise SkillImportError(
            SkillImportCode.OVERSIZED_RESOURCE,
            f"resource exceeds {MAX_RESOURCE_BYTES} bytes",
            path=name,
        )
    return bytes(data)


def _read_manifest(
    files: dict[str, _FoundFile], *, licence: str | None
) -> dict[str, Any]:
    manifest_file = files.pop(SKILL_MANIFEST_NAME, None)
    if manifest_file is None:
        raise SkillImportError(
            SkillImportCode.MANIFEST_MISSING, f"skill source has no {SKILL_MANIFEST_NAME}"
        )
    try:
        payload = json.loads(manifest_file.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SkillImportError(
            SkillImportCode.MANIFEST_INVALID, f"{SKILL_MANIFEST_NAME} is not valid JSON: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise SkillImportError(
            SkillImportCode.MANIFEST_INVALID, f"{SKILL_MANIFEST_NAME} must be a JSON object"
        )
    unknown = sorted(set(payload) - _MANIFEST_FIELDS)
    if unknown:
        raise SkillImportError(
            SkillImportCode.MANIFEST_INVALID, f"unknown {SKILL_MANIFEST_NAME} fields: {unknown}"
        )
    if licence is not None:
        declared = payload.get("licence", payload.get("license"))
        if declared is not None and declared != licence:
            raise SkillImportError(
                SkillImportCode.MANIFEST_INVALID,
                "explicit licence argument contradicts the source manifest",
            )
    return payload


def _licence_of(manifest: Mapping[str, Any], licence: str | None) -> str:
    declared = manifest.get("licence")
    alias = manifest.get("license")
    if declared is not None and alias is not None and declared != alias:
        raise SkillImportError(
            SkillImportCode.MANIFEST_INVALID, "licence and license must not disagree"
        )
    value = declared or alias or licence
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise SkillImportError(
            SkillImportCode.LICENCE_MISSING,
            "skill sources must declare a licence (skill.json licence or licence=...)",
        )
    return value


def _read_dependencies(manifest: Mapping[str, Any]) -> tuple[SkillDependency, ...]:
    declared = manifest.get("dependencies", [])
    if not isinstance(declared, list):
        raise SkillImportError(
            SkillImportCode.MANIFEST_INVALID, "dependencies must be a list"
        )
    dependencies: list[SkillDependency] = []
    for item in declared:
        if not isinstance(item, dict):
            raise SkillImportError(
                SkillImportCode.MANIFEST_INVALID, "each dependency must be an object"
            )
        version = item.get("version")
        if not is_exact_pin(version):
            raise SkillImportError(
                SkillImportCode.UNPINNED_DEPENDENCY,
                f"dependency {item.get('name')!r} is not pinned to an exact version: {version!r}",
            )
        try:
            dependencies.append(SkillDependency.model_validate(item))
        except ValueError as error:  # pydantic ValidationError 也是 ValueError
            raise SkillImportError(
                SkillImportCode.MANIFEST_INVALID, f"invalid dependency: {error}"
            ) from error
    return tuple(dependencies)


def _read_resources(
    files: dict[str, _FoundFile], manifest: Mapping[str, Any], *, kind: str
) -> tuple[ImportedResource, ...]:
    declarations = _resource_declarations(manifest)
    missing = sorted(set(declarations) - set(files))
    if missing:
        raise SkillImportError(
            SkillImportCode.DECLARED_RESOURCE_MISSING,
            f"declared resources are absent from the source: {missing}",
        )
    resources: list[ImportedResource] = []
    for name in sorted(files):
        found = files[name]
        _check_forbidden_path(name)
        declaration = declarations.get(name, {})
        if declarations and name not in declarations:
            raise SkillImportError(
                SkillImportCode.UNDECLARED_RESOURCE,
                "source file is not listed in the declared resources",
                path=name,
            )
        declared_executable = bool(declaration.get("executable", False))
        media_type = _media_type_for(name, found.data)
        if media_type in _ARCHIVE_MEDIA_TYPES:
            raise SkillImportError(
                SkillImportCode.NESTED_ARCHIVE,
                "nested archives are not skill resources",
                path=name,
            )
        if media_type in _NATIVE_BINARY_MEDIA_TYPES:
            raise SkillImportError(
                SkillImportCode.UNKNOWN_BINARY,
                "host binaries are not skill resources",
                path=name,
            )
        if media_type == "application/octet-stream":
            raise SkillImportError(
                SkillImportCode.UNKNOWN_BINARY,
                "unrecognised binary is not a skill resource",
                path=name,
            )
        executable = (
            declared_executable
            or found.executable
            or name.endswith(_EXECUTABLE_SUFFIXES)
            or found.data[:2] == b"#!"
        )
        if executable and not declared_executable:
            raise SkillImportError(
                SkillImportCode.UNDECLARED_EXECUTABLE,
                "executable files must be declared explicitly in resources",
                path=name,
            )
        if declared_executable and kind != "executable":
            raise SkillImportError(
                SkillImportCode.EXECUTABLE_NOT_PERMITTED,
                "only executable skills may declare executable resources",
                path=name,
            )
        digest = "sha256:" + hashlib.sha256(found.data).hexdigest()
        expected = declaration.get("sha256")
        if expected is not None and expected != digest:
            raise SkillImportError(
                SkillImportCode.RESOURCE_HASH_MISMATCH,
                "resource bytes do not match the declared sha256",
                path=name,
            )
        resources.append(
            ImportedResource(
                path=name,
                sha256=digest,
                size_bytes=len(found.data),
                media_type=media_type,
                executable=executable,
                data=found.data,
            )
        )
    return tuple(resources)


def _resource_declarations(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    declared = manifest.get("resources")
    if declared is None:
        return {}
    if not isinstance(declared, list):
        raise SkillImportError(SkillImportCode.MANIFEST_INVALID, "resources must be a list")
    declarations: dict[str, dict[str, Any]] = {}
    for item in declared:
        if not isinstance(item, dict):
            raise SkillImportError(
                SkillImportCode.MANIFEST_INVALID, "each resource declaration must be an object"
            )
        unknown = sorted(set(item) - _RESOURCE_FIELDS)
        if unknown:
            raise SkillImportError(
                SkillImportCode.MANIFEST_INVALID, f"unknown resource declaration fields: {unknown}"
            )
        name = _check_entry_name(item.get("path"))
        if name == SKILL_MANIFEST_NAME:
            raise SkillImportError(
                SkillImportCode.MANIFEST_INVALID, f"{SKILL_MANIFEST_NAME} is not a resource"
            )
        if name in declarations:
            raise SkillImportError(
                SkillImportCode.MANIFEST_INVALID, f"duplicate resource declaration: {name}"
            )
        declarations[name] = item
    return declarations


def _build_draft(
    manifest: Mapping[str, Any],
    *,
    skill_id: str,
    version: str,
    kind: str,
    resources: tuple[ImportedResource, ...],
    dependencies: tuple[SkillDependency, ...],
) -> SkillDraft:
    content: dict[str, Any] = {
        "skill_id": skill_id,
        "version": version,
        "kind": kind,
        "description": manifest.get("description"),
        "instruction": manifest.get("instruction"),
        "instruction_ref": manifest.get("instruction_ref"),
        "resource_manifest": [resource.manifest_entry() for resource in resources],
        "dependency_refs": list(dependencies),
        "requested_permissions": manifest.get("requested_permissions", {}),
        "input_schema": manifest.get("input_schema", {}),
        "output_schema": manifest.get("output_schema", {}),
        "fixture_refs": manifest.get("fixture_refs", []),
        "injection_mode": manifest.get("injection_mode", _DEFAULT_INJECTION_MODE[kind]),
        "entrypoint": manifest.get("entrypoint"),
    }
    try:
        return SkillDraft.model_validate(content)
    except ValueError as error:  # pydantic ValidationError 也是 ValueError
        raise SkillImportError(
            SkillImportCode.MANIFEST_INVALID, f"skill manifest is not a valid draft: {error}"
        ) from error


def _require_text(manifest: Mapping[str, Any], field_name: str) -> str:
    value = manifest.get(field_name)
    if not isinstance(value, str) or not value or value != value.strip():
        raise SkillImportError(
            SkillImportCode.MANIFEST_INVALID, f"{field_name} must be a non-empty string"
        )
    return value


def _detect_install_hooks(files: Mapping[str, _FoundFile]) -> tuple[str, ...]:
    """只发现、只告警：安装钩子永不执行（导入器没有任何执行路径）。"""
    hooks: list[str] = []
    for name in sorted(files):
        basename = name.rsplit("/", 1)[-1].lower()
        if "/" not in name and basename in _INSTALL_HOOK_NAMES:
            hooks.append(name)
        if basename == "package.json":
            try:
                payload = json.loads(files[name].data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            scripts = payload.get("scripts") if isinstance(payload, dict) else None
            if isinstance(scripts, dict) and any(
                key in scripts for key in ("preinstall", "install", "postinstall")
            ):
                hooks.append(name + "#scripts")
    return tuple(hooks)


def _media_type_for(name: str, data: bytes) -> str:
    basename = name.rsplit("/", 1)[-1]
    suffix = basename.rsplit(".", 1)[-1].lower() if "." in basename else ""
    declared = _TEXT_MEDIA_TYPES.get("." + suffix) if suffix else None
    if declared is not None:
        return declared
    head = data[:8]
    for prefix, media in _ARCHIVE_MAGIC:
        if head.startswith(prefix):
            return media
    for prefix, media in _NATIVE_BINARY_MAGIC:
        if head.startswith(prefix):
            return media
    for prefix, media in _KNOWN_BINARY_MAGIC:
        if head.startswith(prefix):
            return media
    if b"\x00" not in data[:4096] and _decodes_utf8(data):
        return "text/plain"
    return "application/octet-stream"


def _decodes_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True
