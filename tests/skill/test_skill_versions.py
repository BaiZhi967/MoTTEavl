"""M5-T06：Skill 三种形态、不可变版本与安全导入。

覆盖 roadmap 第 6 节的三层要求：

* 契约与发布：仅 executable 要求 entrypoint；同版同内容幂等、同版异内容冲突；
  草稿编辑不改变已发布 hash；deprecated 停止新选择但保留历史读取。
* 三存储资源契约：`InMemorySkillRepository` 是 `resources.skills` 的内存等价物，
  显式记录 lead 要实现的方法与不可变语义（同键同 payload 幂等 / 仅允许
  lifecycle+deprecated_* 的弃用转换 / 其余冲突）。
* 安全导入：路径穿越、符号链接、文件数、单文件与总解压尺寸、压缩炸弹（声明
  尺寸不一致与压缩比）、未声明执行文件、未固定依赖、凭据/虚拟环境/未知二进制
  各自有独立错误码，且导入不执行安装钩子、不联网、不跑入口。
"""
from __future__ import annotations

import copy
import hashlib
import json
import stat
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError

import motte_skill.importer as importer
from motte_skill.importer import (
    ContentAddressedMemoryStore,
    SkillImportCode,
    SkillImportError,
    import_skill,
    ingest_resources,
)
from motte_skill.manifest import SkillManifest
from motte_skill.registry import SkillRegistry
from motte_skill.versions import (
    SkillContentHashMismatch,
    SkillDependencyNotPinned,
    SkillDeprecated,
    SkillDraft,
    SkillEntrypoint,
    SkillNotFound,
    SkillResource,
    SkillResourceMissing,
    SkillResourceMismatch,
    SkillSetRef,
    SkillVersion,
    SkillVersionConflict,
    deprecate_skill,
    is_deprecation_transition,
    publish_skill,
    select_skills,
    skill_content_hash,
    verify_dependency_refs,
    verify_resources,
)

PUBLISHED_AT = "2026-09-21T00:00:00Z"
DEPRECATED_AT = "2026-09-22T00:00:00Z"


# ------------------------------------------------------------------ 内存仓库


class InMemorySkillRepository:
    """`resources.skills` 协议的内存等价物（三种存储后端的语义基准）。

    协议（lead 的 storage 接线需要逐项实现）：

    * `get(skill_id, version) -> dict | None`
    * `put(record, *, expected_generation=None) -> dict`
    * `list() -> list[dict]`

    语义：同键同 payload 幂等（返回既有记录）；同键仅 lifecycle/deprecated_*
    变化（published -> deprecated）允许写入；其它任何变化都是
    "different content" 冲突。记录按 JSON payload 往返，和真实列一致。
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], dict] = {}

    @staticmethod
    def _payload(record: dict) -> dict:
        return json.loads(json.dumps(record))

    def get(self, skill_id: str, version: str) -> dict | None:
        row = self._rows.get((str(skill_id), str(version)))
        return copy.deepcopy(row) if row is not None else None

    def put(self, record: dict, *, expected_generation: int | None = None) -> dict:
        payload = self._payload(record)
        key = (payload["skill_id"], payload["version"])
        existing = self._rows.get(key)
        if existing is None:
            self._rows[key] = payload
            return copy.deepcopy(payload)
        if existing == payload:
            return copy.deepcopy(existing)
        if is_deprecation_transition(existing, payload):
            self._rows[key] = payload
            return copy.deepcopy(payload)
        raise SkillVersionConflict(
            "resource version already exists with different content; use a new version"
        )

    def list(self) -> list[dict]:
        return [copy.deepcopy(row) for _, row in sorted(self._rows.items())]


class OverwritingSkillRepository(InMemorySkillRepository):
    """故意违反不可变约束的 KV：证明 publish_skill 自己就挡住了冲突。"""

    def put(self, record: dict, *, expected_generation: int | None = None) -> dict:
        payload = self._payload(record)
        self._rows[(payload["skill_id"], payload["version"])] = payload
        return copy.deepcopy(payload)


class TamperingStore(ContentAddressedMemoryStore):
    """同 ref 返回不同字节：证明发布期会重算内容 hash。"""

    def get(self, ref: str) -> bytes | None:
        return b"tampered"


# --------------------------------------------------------------------- 夹具


def _entry(path: str, data: bytes, media_type: str = "text/plain") -> dict:
    return {
        "path": path,
        "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "media_type": media_type,
    }


RUN_PY = b"print('order cancelled')\n"
INSTRUCTION_MD = "# order helper\n\n先确认，再取消。\n".encode("utf-8")


def instruction_draft(**overrides) -> SkillDraft:
    payload = {
        "skill_id": "order-helper",
        "version": "1",
        "kind": "instruction",
        "description": "确认后再取消订单",
        "instruction": "先向用户确认，再调用取消工具。",
        "injection_mode": "system-prompt",
    }
    payload.update(overrides)
    return SkillDraft.model_validate(payload)


def executable_draft(**overrides) -> SkillDraft:
    payload = {
        "skill_id": "order-cancel-runner",
        "version": "1",
        "kind": "executable",
        "description": "受控入口示例",
        "instruction": "运行受控入口并核对输出 schema。",
        "injection_mode": "context-section",
        "resource_manifest": [_entry("run.py", RUN_PY, "text/x-python")],
        "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}},
        "output_schema": {"type": "object", "properties": {"status": {"type": "string"}}},
        "entrypoint": {
            "interpreter": "python3",
            "argv": ["run.py"],
            "cwd": ".",
            "env_allowlist": ["ORDER_API_BASE"],
        },
    }
    payload.update(overrides)
    return SkillDraft.model_validate(payload)


def _write_source(root: Path, manifest: dict, files: dict[str, object]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "skill.json").write_text(json.dumps(manifest), encoding="utf-8")
    for name, data in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return root


def _write_zip(
    path: Path,
    manifest: dict,
    files: dict[str, object],
    *,
    modes: dict[str, int] | None = None,
    compression: int = zipfile.ZIP_DEFLATED,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression) as archive:
        archive.writestr(zipfile.ZipInfo("skill.json"), json.dumps(manifest))
        for name, data in files.items():
            payload = data.encode("utf-8") if isinstance(data, str) else data
            info = zipfile.ZipInfo(name)
            info.compress_type = compression  # ZipInfo 默认 STORED，必须显式带上
            info.external_attr = (modes or {}).get(name, 0o644) << 16
            archive.writestr(info, payload)
    return path


def _patch_central_uncompressed_size(path: Path, name: str, value: int) -> None:
    """只改中央目录声明的解压尺寸，本地数据不变：制造"声明 != 实际"。"""
    raw = bytearray(path.read_bytes())
    offset = 0
    while True:
        index = raw.find(b"PK\x01\x02", offset)
        assert index >= 0, "central directory entry not found"
        name_length = int.from_bytes(raw[index + 28 : index + 30], "little")
        entry_name = bytes(raw[index + 46 : index + 46 + name_length]).decode("utf-8")
        if entry_name == name:
            raw[index + 24 : index + 28] = value.to_bytes(4, "little")
            path.write_bytes(bytes(raw))
            return
        offset = index + 46 + name_length


def _import_code(source: Path, **kwargs) -> str:
    with pytest.raises(SkillImportError) as info:
        import_skill(source, **kwargs)
    return info.value.code


MINIMAL_MANIFEST = {
    "skill_id": "order-helper", "version": "1", "kind": "instruction",
    "licence": "Apache-2.0",
}


# ======================================================== 契约与发布（规则 1-2）


def test_instruction_only_skill_publishes_without_entrypoint():
    repository = InMemorySkillRepository()
    published = publish_skill(repository, instruction_draft(), published_at=PUBLISHED_AT)
    assert published["kind"] == "instruction"
    assert published["entrypoint"] is None
    assert published["resource_manifest"] == []
    assert SkillVersion.model_validate(published).lifecycle == "published"


def test_executable_skill_without_declared_entrypoint_cannot_be_published():
    with pytest.raises(ValidationError, match="executable skills require a declared entrypoint"):
        SkillDraft.model_validate({
            "skill_id": "runner",
            "version": "1",
            "kind": "executable",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
        })
    # 仓库里也不存在这样的记录：不能靠"没有 entrypoint"发布执行 Skill。
    repository = InMemorySkillRepository()
    with pytest.raises(ValidationError):
        publish_skill(
            repository,
            {"skill_id": "runner", "version": "1", "kind": "executable",
             "input_schema": {"type": "object"}, "output_schema": {"type": "object"}},
            published_at=PUBLISHED_AT,
        )
    assert repository.list() == []


def test_skill_version_never_accepts_a_shell_string_entrypoint():
    shell_text = "python3 run.py && curl http://evil"
    for entrypoint in (shell_text, {"interpreter": "bash", "argv": ["-c", shell_text]}):
        with pytest.raises(ValidationError):
            executable_draft(entrypoint=entrypoint)
    with pytest.raises(ValidationError, match="shell fragment"):
        SkillEntrypoint(interpreter="python3", argv=["run.py; rm -rf /"])
    with pytest.raises(ValidationError, match="inline code string"):
        SkillEntrypoint(interpreter="python3", argv=["-c", "print(1)"])
    # argv 是列表：字符串会被拒绝，而不是被解释成命令。
    with pytest.raises(ValidationError):
        SkillEntrypoint(interpreter="python3", argv="run.py")


def test_executable_entry_requires_argv_interpreter_cwd_env_allowlist_and_schemas():
    with pytest.raises(ValidationError, match="input_schema and output_schema"):
        executable_draft(input_schema={})
    with pytest.raises(ValidationError, match="at least 1 item|argv"):
        executable_draft(entrypoint={"interpreter": "python3", "argv": []})
    with pytest.raises(ValidationError, match="traversal segments"):
        executable_draft(entrypoint={"interpreter": "python3", "argv": ["run.py"],
                                     "cwd": "../outside"})
    with pytest.raises(ValidationError, match="allowed_programs"):
        executable_draft(entrypoint={
            "interpreter": "python3",
            "argv": ["run.py"],
            "sandbox": {"commands": {"allowed_programs": ["node"]}},
        })
    with pytest.raises(ValidationError):
        executable_draft(entrypoint={
            "interpreter": "python3",
            "argv": ["run.py"],
            "sandbox": {"network": "allowed"},
        })
    with pytest.raises(ValidationError, match="env_allowlist"):
        SkillEntrypoint(interpreter="python3", argv=["run.py"], env_allowlist=["LD_PRELOAD"])
    with pytest.raises(ValidationError, match="env_allowlist"):
        SkillEntrypoint(
            interpreter="python3",
            argv=["run.py"],
            sandbox={"environment": ["SECRET_TOKEN"]},
        )

    entrypoint = executable_draft().entrypoint
    assert entrypoint is not None
    assert entrypoint.command() == ("python3", "run.py")
    assert entrypoint.validated_command() == ("python3", "run.py")
    assert entrypoint.sandbox.network == "none"


def test_entrypoint_script_must_be_a_declared_resource():
    with pytest.raises(ValidationError, match="must be a declared resource"):
        executable_draft(entrypoint={"interpreter": "python3", "argv": ["other.py"]})


def test_skill_kind_scope_is_enforced_in_both_directions():
    with pytest.raises(ValidationError, match="cannot declare an entrypoint"):
        instruction_draft(entrypoint={"interpreter": "python3", "argv": ["run.py"]})
    with pytest.raises(ValidationError, match="require a resource_manifest"):
        instruction_draft(kind="instruction_with_resources")
    with pytest.raises(ValidationError, match="carry no resources"):
        instruction_draft(resource_manifest=[_entry("x.md", b"x", "text/markdown")])
    with pytest.raises(ValidationError, match="instruction_ref must be a declared resource"):
        instruction_draft(instruction_ref="missing.md", instruction=None)


def test_multiple_skill_order_is_part_of_the_identity():
    forward = SkillSetRef(skills=[
        {"skill_id": "a", "version": "1"},
        {"skill_id": "b", "version": "1"},
    ])
    backward = SkillSetRef(skills=[
        {"skill_id": "b", "version": "1"},
        {"skill_id": "a", "version": "1"},
    ])
    assert forward.selection_hash() != backward.selection_hash()
    assert forward.selection_hash() == SkillSetRef(skills=list(forward.skills)).selection_hash()
    with pytest.raises(ValidationError, match="at most once"):
        SkillSetRef(skills=[
            {"skill_id": "a", "version": "1"},
            {"skill_id": "a", "version": "2"},
        ])


def test_legacy_skill_manifest_and_registry_stay_working():
    manifest = SkillManifest(name="legacy", version="1", entrypoint="./run.sh", permissions=("net",))
    registry = SkillRegistry()
    assert registry.register(manifest) is manifest
    assert registry.get("legacy") is manifest
    assert registry.get("legacy", "1") is manifest
    assert registry.get("legacy", "2") is None
    # entrypoint 是 shell 字符串 => 永远进不了可发布契约。
    with pytest.raises(ValidationError):
        SkillVersion(skill_id="legacy", version="1", kind="executable",
                     entrypoint=manifest.entrypoint, published_at=PUBLISHED_AT)


# ==================================================== 不可变发布（规则 4-5）


def test_same_version_same_content_is_idempotent(tmp_path):
    repository = InMemorySkillRepository()
    store = ContentAddressedMemoryStore()
    report = import_skill(_write_source(
        tmp_path / "src",
        {"skill_id": "doc-skill", "version": "1", "kind": "instruction",
         "licence": "Apache-2.0", "instruction": "写文档。"},
        {},
    ))
    ingested = ingest_resources(store, report)
    assert ingested == ()
    first = publish_skill(repository, report.draft, resource_store=store, published_at=PUBLISHED_AT)
    second = publish_skill(repository, report.draft, resource_store=store, published_at=PUBLISHED_AT)
    assert first == second
    assert repository.get("doc-skill", "1")["content_hash"] == first["content_hash"]


def test_same_version_different_content_is_a_conflict():
    repository = InMemorySkillRepository()
    first = publish_skill(repository, instruction_draft(), published_at=PUBLISHED_AT)
    with pytest.raises(SkillVersionConflict, match="different content"):
        publish_skill(repository, instruction_draft(instruction="完全不同的指令。"))
    assert repository.get("order-helper", "1") == first
    with pytest.raises(SkillVersionConflict, match="different content"):
        repository.put({**first, "instruction": "直接改仓库也不行"})


def test_publish_skill_enforces_conflict_without_relying_on_the_repository():
    repository = OverwritingSkillRepository()
    first = publish_skill(repository, instruction_draft(), published_at=PUBLISHED_AT)
    with pytest.raises(SkillVersionConflict, match="different content"):
        publish_skill(repository, instruction_draft(instruction="另一个内容。"))
    assert repository.get("order-helper", "1") == first


def test_historical_run_resource_hashes_survive_a_new_draft_edit(tmp_path):
    repository = InMemorySkillRepository()
    store = ContentAddressedMemoryStore()
    source = _write_source(
        tmp_path / "src",
        {"skill_id": "order-helper", "version": "1", "kind": "instruction_with_resources",
         "licence": "Apache-2.0", "instruction_ref": "instruction.md",
         "injection_mode": "context-section", "resources": [{"path": "instruction.md"}]},
        {"instruction.md": INSTRUCTION_MD},
    )
    report = import_skill(source)
    ingest_resources(store, report)
    published = publish_skill(repository, report.draft, resource_store=store, published_at=PUBLISHED_AT)
    run_snapshot = {
        "content_hash": published["content_hash"],
        "resources": {entry["path"]: entry["sha256"] for entry in published["resource_manifest"]},
    }
    assert report.licence == "Apache-2.0"

    edited = report.draft.edited(instruction="改过的草稿指令。", instruction_ref=None)
    assert skill_content_hash(edited) != run_snapshot["content_hash"]
    with pytest.raises(SkillVersionConflict):
        publish_skill(repository, edited, resource_store=store, published_at=PUBLISHED_AT)

    # 历史 Run 重读同一版本：内容 hash 与逐资源 hash 都不变。
    historical = SkillVersion.model_validate(repository.get("order-helper", "1"))
    assert historical.content_hash == run_snapshot["content_hash"]
    assert {entry.path: entry.sha256 for entry in historical.resource_manifest} == (
        run_snapshot["resources"]
    )
    verify_resources(store, historical.resource_manifest)

    # 新版本用新字节 => 新 hash；旧版本仍然不变。
    newer_source = _write_source(
        tmp_path / "src2",
        {"skill_id": "order-helper", "version": "2", "kind": "instruction_with_resources",
         "licence": "Apache-2.0", "instruction_ref": "instruction.md",
         "injection_mode": "context-section", "resources": [{"path": "instruction.md"}]},
        {"instruction.md": b"# v2\n\n" + INSTRUCTION_MD},
    )
    newer = import_skill(newer_source)
    ingest_resources(store, newer)
    publish_skill(repository, newer.draft, resource_store=store, published_at=PUBLISHED_AT)
    assert repository.get("order-helper", "1")["content_hash"] == run_snapshot["content_hash"]
    assert repository.get("order-helper", "2")["content_hash"] != run_snapshot["content_hash"]


def test_publish_reverifies_content_addressed_resource_bytes(tmp_path):
    repository = InMemorySkillRepository()
    store = ContentAddressedMemoryStore()
    source = _write_source(
        tmp_path / "src",
        {"skill_id": "order-helper", "version": "1", "kind": "instruction_with_resources",
         "licence": "Apache-2.0", "instruction_ref": "instruction.md",
         "injection_mode": "context-section", "resources": [{"path": "instruction.md"}]},
        {"instruction.md": INSTRUCTION_MD},
    )
    report = import_skill(source)
    ingest_resources(store, report)
    store.drop("sha256:" + hashlib.sha256(INSTRUCTION_MD).hexdigest())
    with pytest.raises(SkillResourceMissing):
        publish_skill(repository, report.draft, resource_store=store, published_at=PUBLISHED_AT)
    assert repository.get("order-helper", "1") is None

    with pytest.raises(SkillResourceMismatch):
        publish_skill(repository, report.draft, resource_store=TamperingStore(),
                      published_at=PUBLISHED_AT)
    assert repository.get("order-helper", "1") is None


def test_publish_reverifies_dependency_pins_and_content_hash():
    repository = InMemorySkillRepository()
    with pytest.raises(SkillDependencyNotPinned):
        verify_dependency_refs([{"name": "requests", "version": ">=2.0"}])
    with pytest.raises(ValidationError, match="exact pin"):
        instruction_draft(dependency_refs=[{"name": "requests", "version": "latest"}])
    with pytest.raises(SkillContentHashMismatch):
        publish_skill(
            repository,
            {**instruction_draft().model_dump(mode="json"),
             "content_hash": "sha256:" + "0" * 64},
            published_at=PUBLISHED_AT,
        )
    assert repository.list() == []


def test_skill_version_records_are_published_or_deprecated_only():
    with pytest.raises(ValidationError, match="published|deprecated|Input should be"):
        SkillVersion(skill_id="x", version="1", kind="instruction", instruction="i",
                     lifecycle="draft", published_at=PUBLISHED_AT)


# ====================================================== 弃用与选择（规则 4）


def test_deprecation_blocks_new_selection_but_keeps_historical_reads():
    repository = InMemorySkillRepository()
    published = publish_skill(repository, instruction_draft(), published_at=PUBLISHED_AT)
    publish_skill(repository, instruction_draft(version="2"), published_at=PUBLISHED_AT)
    original_hash = published["content_hash"]

    deprecated = deprecate_skill(repository, "order-helper", "1",
                                 deprecated_at=DEPRECATED_AT, reason="被 v2 取代")
    assert deprecated["lifecycle"] == "deprecated"
    assert deprecated["content_hash"] == original_hash
    assert deprecated["deprecated_at"] == DEPRECATED_AT
    # 重复弃用幂等。
    assert deprecate_skill(repository, "order-helper", "1",
                           deprecated_at=DEPRECATED_AT, reason="被 v2 取代") == deprecated
    assert is_deprecation_transition(published, deprecated)

    with pytest.raises(SkillDeprecated):
        select_skills(repository, [{"skill_id": "order-helper", "version": "1"}])
    historical = select_skills(repository, [{"skill_id": "order-helper", "version": "1"}],
                               allow_deprecated=True)
    assert historical[0].content_hash == original_hash
    assert historical[0].lifecycle == "deprecated"

    with pytest.raises(SkillVersionConflict):
        publish_skill(repository, instruction_draft(instruction="弃用后还想改内容"),
                      published_at=PUBLISHED_AT)
    assert repository.get("order-helper", "1")["content_hash"] == original_hash


def test_registry_is_version_aware_and_the_repository_is_the_authority():
    repository = InMemorySkillRepository()
    publish_skill(repository, instruction_draft(version="1"), published_at=PUBLISHED_AT)
    publish_skill(repository, instruction_draft(version="2"), published_at=PUBLISHED_AT)
    deprecate_skill(repository, "order-helper", "1", deprecated_at=DEPRECATED_AT)
    registry = SkillRegistry(repository)

    assert registry.latest("order-helper").version == "2"
    assert registry.versions("order-helper") == ("2", "1")
    with pytest.raises(SkillDeprecated):
        registry.resolve("order-helper", "1")
    assert registry.resolve("order-helper", "1", allow_deprecated=True).content_hash == (
        repository.get("order-helper", "1")["content_hash"]
    )
    with pytest.raises(SkillNotFound):
        registry.resolve("order-helper", "9")
    assert registry.get("order-helper", "1") is not None
    assert registry.get("order-helper", "9") is None
    assert len(registry.list(lifecycle="published")) == 1
    assert registry.cached("order-helper", "2") is not None

    # 缓存不能掩盖弃用：deprecate 后即使已缓存，resolve 也必须拒绝。
    assert registry.cached("order-helper", "2") is not None
    deprecate_skill(repository, "order-helper", "2", deprecated_at=DEPRECATED_AT)
    with pytest.raises(SkillDeprecated):
        registry.resolve("order-helper", "2")
    assert registry.latest("order-helper") is None


def test_selection_hash_mismatch_is_rejected():
    repository = InMemorySkillRepository()
    publish_skill(repository, instruction_draft(), published_at=PUBLISHED_AT)
    with pytest.raises(SkillContentHashMismatch):
        select_skills(repository, [{
            "skill_id": "order-helper", "version": "1", "content_hash": "sha256:" + "0" * 64,
        }])
    assert select_skills(repository, [{
        "skill_id": "order-helper",
        "version": "1",
        "content_hash": repository.get("order-helper", "1")["content_hash"],
    }])[0].skill_id == "order-helper"


# ====================================================== 安全导入（规则 3、6）


def test_import_instruction_skill_records_provenance_and_licence(tmp_path):
    source = _write_source(
        tmp_path / "src",
        {"skill_id": "doc-skill", "version": "1", "kind": "instruction",
         "licence": "Apache-2.0", "instruction": "写文档。",
         "description": "文档 Skill"},
        {},
    )
    report = import_skill(source)
    assert report.licence == "Apache-2.0"
    assert report.provenance["licence"] == "Apache-2.0"
    assert report.provenance["origin_type"] == "directory"
    assert report.provenance["origin"].endswith("src")
    assert report.provenance["importer_version"] == importer.IMPORTER_VERSION
    assert report.provenance["imported_at"].endswith("Z")
    assert report.draft.lifecycle == "draft"
    assert report.draft.kind == "instruction"
    assert report.manifest_entries() == ()

    with pytest.raises(SkillImportError) as info:
        import_skill(source, licence="MIT")
    assert info.value.code == SkillImportCode.MANIFEST_INVALID


def test_import_rejects_missing_licence(tmp_path):
    source = _write_source(tmp_path / "src", {"skill_id": "order-helper", "version": "1",
                                              "kind": "instruction", "instruction": "i"}, {})
    assert _import_code(source) == SkillImportCode.LICENCE_MISSING
    assert import_skill(source, licence="UNLICENSED").licence == "UNLICENSED"


def test_import_rejects_path_traversal(tmp_path):
    archive = _write_zip(tmp_path / "src.zip", MINIMAL_MANIFEST, {"../evil.md": "boom"})
    assert _import_code(archive) == SkillImportCode.PATH_TRAVERSAL
    absolute = _write_zip(tmp_path / "abs.zip", MINIMAL_MANIFEST, {"/etc/passwd": "boom"})
    assert _import_code(absolute) == SkillImportCode.PATH_TRAVERSAL
    # Windows 的 zipfile 会把反斜杠规范成 "/"，因此直接校验路径守卫本身。
    with pytest.raises(SkillImportError) as info:
        importer._check_entry_name("a\\b.md")
    assert info.value.code == SkillImportCode.PATH_TRAVERSAL


def test_import_rejects_symlink_entries(tmp_path):
    archive = tmp_path / "link.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(zipfile.ZipInfo("skill.json"), json.dumps(MINIMAL_MANIFEST))
        info = zipfile.ZipInfo("instruction.md")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        handle.writestr(info, "instruction.md")
    assert _import_code(archive) == SkillImportCode.SYMLINK_RESOURCE


def test_import_rejects_symlinked_files_on_disk(tmp_path):
    source = _write_source(tmp_path / "src", dict(MINIMAL_MANIFEST, instruction="i"), {})
    target = source / "skill.json"
    try:
        (source / "link.md").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("filesystem does not support symlinks")
    assert _import_code(source) == SkillImportCode.SYMLINK_RESOURCE


def test_import_rejects_too_many_files(tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "MAX_RESOURCE_FILES", 2)
    source = _write_source(tmp_path / "src", dict(MINIMAL_MANIFEST, instruction="i"),
                           {"a.md": "a", "b.md": "b", "c.md": "c"})
    assert _import_code(source) == SkillImportCode.TOO_MANY_FILES


def test_import_rejects_oversized_resource(tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "MAX_RESOURCE_BYTES", 8)
    source = _write_source(tmp_path / "src", dict(MINIMAL_MANIFEST, instruction="i"),
                           {"big.md": b"x" * 64})
    assert _import_code(source) == SkillImportCode.OVERSIZED_RESOURCE


def test_import_rejects_oversized_extraction(tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "MAX_TOTAL_RESOURCE_BYTES", 16)
    source = _write_source(tmp_path / "src", dict(MINIMAL_MANIFEST, instruction="i"),
                           {"a.md": b"x" * 16, "b.md": b"y" * 16})
    assert _import_code(source) == SkillImportCode.OVERSIZED_EXTRACTION

    archive = _write_zip(tmp_path / "big.zip", dict(MINIMAL_MANIFEST, instruction="i"),
                         {"a.md": b"x" * 64})
    assert _import_code(archive) == SkillImportCode.OVERSIZED_EXTRACTION


def test_import_rejects_unreadable_archives(tmp_path):
    corrupt = tmp_path / "corrupt.zip"
    corrupt.write_bytes(b"PK\x03\x04" + b"\x00" * 64)
    assert _import_code(corrupt) == SkillImportCode.ARCHIVE_INVALID


def test_import_rejects_zip_bomb_declared_size_mismatch(tmp_path):
    archive = _write_zip(tmp_path / "liar.zip", dict(MINIMAL_MANIFEST, instruction="i"),
                         {"a.md": b"x" * 4096})
    _patch_central_uncompressed_size(archive, "a.md", 16)
    assert _import_code(archive) == SkillImportCode.ZIP_BOMB_DECLARED_MISMATCH


def test_import_rejects_zip_bomb_compression_ratio(tmp_path):
    archive = _write_zip(tmp_path / "bomb.zip", dict(MINIMAL_MANIFEST, instruction="i"),
                         {"a.md": b"\x00" * (256 * 1024)})
    assert _import_code(archive) == SkillImportCode.ZIP_BOMB_RATIO


def test_import_rejects_unsupported_compression(tmp_path):
    archive = _write_zip(tmp_path / "bz2.zip", dict(MINIMAL_MANIFEST, instruction="i"),
                         {"a.md": "hello"}, compression=zipfile.ZIP_BZIP2)
    assert _import_code(archive) == SkillImportCode.ARCHIVE_COMPRESSION_UNSUPPORTED


def test_import_rejects_undeclared_executable(tmp_path):
    source = _write_source(tmp_path / "src", dict(MINIMAL_MANIFEST, instruction="i"),
                           {"run.sh": "#!/bin/sh\necho hi\n"})
    assert _import_code(source) == SkillImportCode.UNDECLARED_EXECUTABLE
    archive = _write_zip(tmp_path / "exec.zip", dict(MINIMAL_MANIFEST, instruction="i"),
                         {"tool.sh": "#!/bin/sh\necho hi\n"}, modes={"tool.sh": 0o755})
    assert _import_code(archive) == SkillImportCode.UNDECLARED_EXECUTABLE


def test_import_rejects_executable_resources_in_instruction_skills(tmp_path):
    manifest = dict(MINIMAL_MANIFEST, instruction="i",
                    resources=[{"path": "run.sh", "executable": True}])
    source = _write_source(tmp_path / "src", manifest, {"run.sh": "#!/bin/sh\necho hi\n"})
    assert _import_code(source) == SkillImportCode.EXECUTABLE_NOT_PERMITTED


def test_import_accepts_declared_executables_for_executable_skills(tmp_path):
    manifest = {
        "skill_id": "runner", "version": "1", "kind": "executable",
        "licence": "Apache-2.0", "instruction": "受控入口",
        "injection_mode": "none",
        "input_schema": {"type": "object"}, "output_schema": {"type": "object"},
        "resources": [
            {"path": "run.sh", "executable": True},
            {"path": "runner.py"},
        ],
        "entrypoint": {"interpreter": "python3", "argv": ["runner.py"]},
    }
    source = _write_source(tmp_path / "src", manifest, {
        "run.sh": "#!/bin/sh\necho hi\n",
        "runner.py": "print('run')\n",
    })
    report = import_skill(source)
    executables = {resource.path: resource.executable for resource in report.resources}
    assert executables == {"run.sh": True, "runner.py": False}
    # 未声明的入口脚本仍然拒绝：先删掉声明再看。
    undeclared = _write_source(tmp_path / "src2", manifest, {"run.sh": "#!/bin/sh\necho hi\n"})
    assert _import_code(undeclared) == SkillImportCode.DECLARED_RESOURCE_MISSING


def test_import_rejects_unpinned_dependencies(tmp_path):
    for version in (">=1.0", "*", "latest", "^1.2", "git+https://example.invalid/x#main"):
        manifest = dict(MINIMAL_MANIFEST, instruction="i",
                        dependencies=[{"name": "requests", "version": version}])
        source = _write_source(tmp_path / f"src-{abs(hash(version))}", manifest, {})
        assert _import_code(source) == SkillImportCode.UNPINNED_DEPENDENCY
    pinned = _write_source(
        tmp_path / "pinned",
        dict(MINIMAL_MANIFEST, instruction="i",
             dependencies=[{"name": "requests", "version": "2.32.3"}]),
        {},
    )
    assert import_skill(pinned).dependencies[0].version == "2.32.3"


def test_import_rejects_host_credentials(tmp_path):
    source = _write_source(tmp_path / "src", dict(MINIMAL_MANIFEST, instruction="i"),
                           {".env": "OPENAI_API_KEY=sk-not-a-real-key",
                            ".aws/credentials": "[default]\naws_access_key_id = x",
                            "keys/server.pem": "-----BEGIN PRIVATE KEY-----"})
    assert _import_code(source) == SkillImportCode.CREDENTIALS_ARTIFACT


def test_import_rejects_vendored_dependency_directories(tmp_path):
    source = _write_source(tmp_path / "src", dict(MINIMAL_MANIFEST, instruction="i"),
                           {"node_modules/left-pad/index.js": "module.exports = 1;"})
    assert _import_code(source) == SkillImportCode.VENDORED_DEPENDENCY
    venv = _write_source(tmp_path / "venv-src", dict(MINIMAL_MANIFEST, instruction="i"),
                         {".venv/Lib/site-packages/x.py": "x = 1"})
    assert _import_code(venv) == SkillImportCode.VENDORED_DEPENDENCY


def test_import_rejects_unknown_binaries(tmp_path):
    blob = _write_source(tmp_path / "blob", dict(MINIMAL_MANIFEST, instruction="i"),
                         {"payload.dat": bytes(range(64))})
    assert _import_code(blob) == SkillImportCode.UNKNOWN_BINARY
    elf = _write_source(tmp_path / "elf", dict(MINIMAL_MANIFEST, instruction="i"),
                        {"payload.dat": b"\x7fELF" + b"\x00" * 64})
    assert _import_code(elf) == SkillImportCode.UNKNOWN_BINARY


def test_import_rejects_nested_archives(tmp_path):
    archive = _write_zip(tmp_path / "src.zip", dict(MINIMAL_MANIFEST, instruction="i"),
                         {"inner.zip": b"PK\x03\x04" + b"\x00" * 32})
    assert _import_code(archive) == SkillImportCode.NESTED_ARCHIVE


def test_import_rejects_undeclared_and_missing_resources(tmp_path):
    manifest = dict(MINIMAL_MANIFEST, instruction="i",
                    resources=[{"path": "a.md"}])
    source = _write_source(tmp_path / "src", manifest, {"a.md": "a", "b.md": "b"})
    assert _import_code(source) == SkillImportCode.UNDECLARED_RESOURCE

    missing = _write_source(tmp_path / "missing",
                            dict(MINIMAL_MANIFEST, instruction="i",
                                 resources=[{"path": "a.md"}, {"path": "gone.md"}]),
                            {"a.md": "a"})
    assert _import_code(missing) == SkillImportCode.DECLARED_RESOURCE_MISSING


def test_import_rejects_resource_hash_mismatch(tmp_path):
    manifest = dict(MINIMAL_MANIFEST, instruction="i",
                    resources=[{"path": "a.md", "sha256": "sha256:" + "0" * 64}])
    source = _write_source(tmp_path / "src", manifest, {"a.md": "a"})
    assert _import_code(source) == SkillImportCode.RESOURCE_HASH_MISMATCH


def test_import_rejects_instruction_ref_outside_the_manifest(tmp_path):
    manifest = dict(MINIMAL_MANIFEST, instruction=None, instruction_ref="other.md",
                    resources=[{"path": "a.md"}])
    source = _write_source(tmp_path / "src", manifest, {"a.md": "a"})
    assert _import_code(source) == SkillImportCode.INSTRUCTION_REF_UNDECLARED


def test_import_rejects_missing_manifest_and_unsupported_source(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _import_code(empty) == SkillImportCode.MANIFEST_MISSING
    text = tmp_path / "skill.txt"
    text.write_text("not a skill", encoding="utf-8")
    assert _import_code(text) == SkillImportCode.SOURCE_UNSUPPORTED
    assert _import_code(tmp_path / "nope") == SkillImportCode.SOURCE_MISSING


def test_import_never_executes_install_hooks_or_entrypoint(tmp_path):
    marker = tmp_path / "M5T06_PWNED"
    hook = f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
    manifest = {
        "skill_id": "order-helper", "version": "1",
        "kind": "instruction_with_resources", "licence": "Apache-2.0",
        "instruction_ref": "instruction.md", "injection_mode": "context-section",
    }
    source = _write_source(tmp_path / "src", manifest, {
        "setup.py": hook,
        "package.json": json.dumps({"scripts": {"postinstall": "node postinstall.js"}}),
        "postinstall.js": f"require('fs').writeFileSync({json.dumps(str(marker))}, 'x')",
        "instruction.md": INSTRUCTION_MD,
    })
    report = import_skill(source)
    assert not marker.exists()
    assert any("not_executed" in warning for warning in report.warnings)
    assert "package.json#scripts" in report.provenance["install_hooks_detected"]
    assert {resource.path for resource in report.resources} == {
        "setup.py", "package.json", "postinstall.js", "instruction.md",
    }

    archive = _write_zip(tmp_path / "hooks.zip", manifest,
                         {"setup.py": hook, "instruction.md": INSTRUCTION_MD})
    archived = import_skill(archive)
    assert not marker.exists()
    assert archived.warnings


def test_importer_has_no_execution_or_network_paths():
    source = Path(importer.__file__).read_text(encoding="utf-8")
    for token in ("subprocess", "urllib", "requests", "httpx", "socket", "shutil.unpack",
                  "os.system", "eval(", "exec(", "__import__", "importlib"):
        assert token not in source, f"importer must stay read-only: {token}"


def test_imported_skill_publishes_end_to_end_through_a_zip(tmp_path):
    repository = InMemorySkillRepository()
    store = ContentAddressedMemoryStore()
    archive = _write_zip(
        tmp_path / "skill.zip",
        {
            "skill_id": "order-helper", "version": "1",
            "kind": "instruction_with_resources", "licence": "Apache-2.0",
            "instruction_ref": "instruction.md", "injection_mode": "context-section",
            "description": "订单助手", "resources": [{"path": "instruction.md"}],
        },
        {"instruction.md": INSTRUCTION_MD},
    )
    report = import_skill(archive)
    entries = ingest_resources(store, report)
    assert entries == report.manifest_entries()
    published = publish_skill(repository, report.draft, resource_store=store,
                              published_at=PUBLISHED_AT)
    assert published["instruction_ref"] == "instruction.md"
    assert report.provenance["origin_type"] == "archive"

    registry = SkillRegistry(repository)
    selected = registry.select(SkillSetRef(skills=[
        {"skill_id": "order-helper", "version": "1", "content_hash": published["content_hash"]},
    ]))
    assert selected[0].content_hash == published["content_hash"]
    assert selected[0].resource_manifest[0].sha256 == entries[0].sha256
    assert store.get(entries[0].sha256) == INSTRUCTION_MD


def test_content_addressed_store_is_deduplicating():
    store = ContentAddressedMemoryStore()
    first = store.put(b"same")
    second = store.put(b"same")
    assert first == second == "sha256:" + hashlib.sha256(b"same").hexdigest()
    assert store.list() == [first]
    assert store.get("sha256:" + "0" * 64) is None


def test_skill_resource_paths_reject_traversal_and_absolute_paths():
    for path in ("../x.md", "/etc/passwd", "a//b.md", "C:/x.md", "./a.md"):
        with pytest.raises(ValidationError):
            SkillResource(path=path, sha256="sha256:" + "0" * 64, size_bytes=0)
