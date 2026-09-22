"""冻结任务副本：准备不可变执行副本并在 Runner 边界复验（M3 review R03）。

复现反例：``prepare → build_run_inputs`` 之后修改 ``instruction.md``，平台侧
的清单复验already 会返回不一致，但执行侧只看"目录存在"，于是 Harbor 照旧
执行改动后的任务包（tests/solution/Compose 同样可能漂移），执行内容与
task_key、revision、预检和评分快照不再对应。

本模块把选中任务的每一个文件按冻结清单
``plan["task_files"][task_key] = {相对路径: sha256}`` 逐字节复验后复制到
受控工作目录的 ``frozen-tasks/<task_key>/``，并写
``frozen-tasks/manifest.json``。执行只允许读取这份副本（``MOTTE_TASK_ROOT``
指向它），源目录之后的任何改动都无法影响已经冻结的 Run：

- 缺失/多余/内容不符/符号链接/非普通文件都算漂移 → ``HARBOR_TASK_CONTENT_DRIFT``；
- 源目录缺失 → ``HARBOR_TASK_SOURCE_MISSING``；
- 漂移在 ``start`` 之前拒绝：不启动 Job、不"重新计算身份后沿用旧 Run"。

写侧同样受控：目标路径逐段校验仍在冻结根内（``is_relative_to``），不跟随
符号链接，不把宿主上的任意路径当任务内容。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from motte_benchmark.harbor.tasks import (
    HarborTaskError,
    normalize_relative_path,
    task_content_hash,
)
from motte_benchmark.protocol import BenchmarkRuntimeError
from motte_benchmark.trusted import TrustedDir, TrustedPathError
from motte_contracts.trial import canonical_hash

#: 冻结副本的 schema 与目录名（Runner 只从这个目录读任务）。
FROZEN_SCHEMA = "motte-frozen-tasks@1"
FROZEN_DIR_NAME = "frozen-tasks"
FROZEN_MANIFEST_NAME = "manifest.json"
#: 单个任务文件与单任务总量的复制限额（与准备阶段限额一致）。
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
#: 任务目录名允许的字符：Harbor 把任务目录 basename 带进 Trial 目录名与 docker
#: compose 的挂载规格（``<host>:<container>``），冒号会让挂载规格失效——因此
#: ``sha256:...`` 这类 task_key 必须换成文件系统/Compose 都安全的目录名。
_SAFE_DIR_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def frozen_dir_name(task_key: str) -> str:
    """task_key → 冻结副本的目录名（确定性、docker/compose 安全）。"""
    cleaned = "".join(
        character if character in _SAFE_DIR_CHARS else "-" for character in str(task_key)
    )
    if not cleaned or cleaned in (".", ".."):
        raise BenchmarkRuntimeError(
            "HARBOR_TASK_FILES_MISSING", f"task_key cannot form a safe directory: {task_key!r}",
        )
    return cleaned


def _hash_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def task_files_hash(task_files: Mapping[str, Any]) -> str:
    """``task_files``（``{task_key: {相对路径: sha256}}``）的规范哈希。"""
    normalized = {
        str(task_key): {
            str(rel): str(digest) for rel, digest in dict(files or {}).items()
        }
        for task_key, files in task_files.items()
    }
    return canonical_hash(normalized)


def _plan_tasks(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """计划里声明的任务（``task_key`` + ``normalized_relative_path``）。"""
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise BenchmarkRuntimeError(
            "HARBOR_PLAN_MISSING", "runner_config.plan.tasks is required",
        )
    out: list[dict[str, Any]] = []
    for entry in tasks:
        if not isinstance(entry, Mapping) or not entry.get("task_key"):
            raise BenchmarkRuntimeError(
                "HARBOR_PLAN_INVALID",
                f"plan.tasks entries require task_key and path: {entry!r}",
            )
        out.append({
            "task_key": str(entry["task_key"]),
            "normalized_relative_path": normalize_relative_path(
                str(entry.get("normalized_relative_path") or entry.get("path") or ""),
            ),
        })
    return out


def _frozen_task_files(plan: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    raw = plan.get("task_files")
    if not isinstance(raw, Mapping) or not raw:
        raise BenchmarkRuntimeError(
            "HARBOR_TASK_FILES_MISSING",
            "runner_config.plan.task_files is required: without per-file hashes the "
            "runtime cannot prove the executed bytes are the frozen ones",
        )
    out: dict[str, dict[str, str]] = {}
    for task_key, files in raw.items():
        if not isinstance(files, Mapping) or not files:
            raise BenchmarkRuntimeError(
                "HARBOR_TASK_FILES_MISSING",
                f"plan.task_files[{task_key}] must be a non-empty path→sha256 mapping",
            )
        out[str(task_key)] = {str(rel): str(digest) for rel, digest in files.items()}
    return out


def _safe_rel_parts(relative: str) -> list[str]:
    """冻结清单里的相对路径拆成安全组件（越界/绝对路径立即失败）。"""
    candidate = str(relative).replace("\\", "/")
    if candidate.startswith("/") or ":" in candidate:
        raise BenchmarkRuntimeError(
            "HARBOR_TASK_CONTENT_DRIFT",
            f"frozen task file path must stay relative: {relative!r}",
        )
    parts = [part for part in PurePosixPath(candidate).parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise BenchmarkRuntimeError(
            "HARBOR_TASK_CONTENT_DRIFT",
            f"frozen task file path escapes the task dir: {relative!r}",
        )
    return parts


def _write_file(root: Path, relative_parts: list[str], data: bytes) -> None:
    """受控写入：逐段校验仍在冻结根内，拒绝 symlink 目标。"""
    base = Path(root).resolve()
    base.mkdir(parents=True, exist_ok=True)
    current = base
    for part in relative_parts[:-1]:
        current = current.joinpath(part)
        if current.is_symlink():
            raise BenchmarkRuntimeError(
                "HARBOR_FROZEN_WRITE_ESCAPE",
                f"frozen task path component is a symlink: {current}",
            )
        current.mkdir(exist_ok=True)
    target = current.joinpath(relative_parts[-1])
    if not target.resolve().is_relative_to(base):
        raise BenchmarkRuntimeError(
            "HARBOR_FROZEN_WRITE_ESCAPE", f"frozen task file escapes root: {target}",
        )
    if target.is_symlink():
        raise BenchmarkRuntimeError(
            "HARBOR_FROZEN_WRITE_ESCAPE", f"frozen task file is a symlink: {target}",
        )
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_TRUNC
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    fd = os.open(target, flags, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def materialize_frozen_tasks(
    *,
    plan: Mapping[str, Any],
    data_root: Path | str,
    work_dir: Path | str,
    limits: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """从受控任务根物化不可变副本，返回冻结清单载荷。

    每个选中任务的每个文件都逐字节与 ``plan["task_files"]`` 比对；任何差异
    都在这里失败（调用方在 ``start`` 之前调用，因此不会启动 Job）。
    """
    source_root = Path(data_root)
    if not source_root.is_dir() or source_root.is_symlink():
        raise BenchmarkRuntimeError(
            "HARBOR_TASK_SOURCE_MISSING",
            f"controlled task root is missing or a symlink: {source_root}",
        )
    frozen_root = Path(work_dir).resolve() / FROZEN_DIR_NAME
    frozen_root.mkdir(parents=True, exist_ok=True)
    max_file_bytes = int((limits or {}).get("max_file_bytes", MAX_FILE_BYTES))
    max_total_bytes = int((limits or {}).get("max_total_bytes", MAX_TOTAL_BYTES))
    task_files = _frozen_task_files(plan)
    tasks_payload: dict[str, Any] = {}
    content_hashes = plan.get("task_content_hashes") or {}
    for task in _plan_tasks(plan):
        task_key = task["task_key"]
        expected = task_files.get(task_key)
        if expected is None:
            raise BenchmarkRuntimeError(
                "HARBOR_TASK_FILES_MISSING",
                f"plan.task_files has no entry for task {task_key}",
            )
        source_dir = source_root.joinpath(*task["normalized_relative_path"].split("/"))
        if not source_dir.is_dir():
            raise BenchmarkRuntimeError(
                "HARBOR_TASK_SOURCE_MISSING",
                f"task source directory is missing for {task_key}: {source_dir}",
            )
        directory = frozen_dir_name(task_key)
        with TrustedDir(source_root, error_factory=_source_error) as trusted:
            present, symlinks = trusted.list_files()
            prefix = task["normalized_relative_path"] + "/"
            owned = sorted(rel[len(prefix):] for rel in present if rel.startswith(prefix))
            symlinked = sorted(rel[len(prefix):] for rel in symlinks if rel.startswith(prefix))
            if symlinked:
                raise BenchmarkRuntimeError(
                    "HARBOR_TASK_CONTENT_DRIFT",
                    f"task {task_key} contains symlinked files: {symlinked[:5]}",
                )
            missing = sorted(set(expected) - set(owned))
            extra = sorted(set(owned) - set(expected))
            if missing or extra:
                raise BenchmarkRuntimeError(
                    "HARBOR_TASK_CONTENT_DRIFT",
                    f"task {task_key} file set differs from the frozen manifest: "
                    f"missing={missing[:5]} extra={extra[:5]}",
                )
            written: dict[str, str] = {}
            total = 0
            for relative in sorted(expected):
                parts = _safe_rel_parts(relative)
                try:
                    data = trusted.read_bytes(f"{prefix}{relative}", max_bytes=max_file_bytes)
                except TrustedPathError as error:
                    raise BenchmarkRuntimeError(
                        "HARBOR_TASK_CONTENT_DRIFT",
                        f"task {task_key} file is not a readable regular file: "
                        f"{relative}: {error}",
                    ) from error
                total += len(data)
                if total > max_total_bytes:
                    raise BenchmarkRuntimeError(
                        "HARBOR_TASK_CONTENT_DRIFT",
                        f"task {task_key} exceeds {max_total_bytes} frozen bytes",
                    )
                digest = _hash_bytes(data)
                if digest != expected[relative]:
                    raise BenchmarkRuntimeError(
                        "HARBOR_TASK_CONTENT_DRIFT",
                        f"task {task_key} file {relative} changed after freezing: "
                        f"frozen={expected[relative]} observed={digest}",
                    )
                _write_file(frozen_root / directory, parts, data)
                written[relative] = digest
        content_hash = str(content_hashes.get(task_key) or "")
        observed_hash = task_content_hash(written)
        if content_hash and content_hash != observed_hash:
            raise BenchmarkRuntimeError(
                "HARBOR_TASK_CONTENT_DRIFT",
                f"task {task_key} content hash mismatch: frozen={content_hash} "
                f"observed={observed_hash}",
            )
        tasks_payload[task_key] = {
            "directory": directory,
            "relative_path": task["normalized_relative_path"],
            "content_hash": content_hash or observed_hash,
            "files": written,
        }
    manifest = {
        "schema": FROZEN_SCHEMA,
        "tasks": tasks_payload,
        "task_files_hash": task_files_hash(task_files),
        "file_count": sum(len(item["files"]) for item in tasks_payload.values()),
    }
    manifest["manifest_hash"] = canonical_hash(manifest)
    target = frozen_root / FROZEN_MANIFEST_NAME
    _write_file(frozen_root, [FROZEN_MANIFEST_NAME], _encode(manifest))
    return {"root": frozen_root, "manifest": manifest, "manifest_path": target}


def _encode(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _source_error(message: str) -> Exception:
    """受控读取失败按"源不可用/漂移"分类，而不是笼统的路径错误。"""
    code = "HARBOR_TASK_CONTENT_DRIFT" if message.startswith("file-size:") else "HARBOR_TASK_SOURCE_MISSING"
    return BenchmarkRuntimeError(code, message)


def clean_frozen_tasks(work_dir: Path | str) -> None:
    """移除上一次准备的冻结副本（重试同一工作目录时必须重建，不复用旧字节）。"""
    root = Path(work_dir).resolve() / FROZEN_DIR_NAME
    if root.is_symlink():
        raise BenchmarkRuntimeError(
            "HARBOR_FROZEN_WRITE_ESCAPE", f"frozen task root is a symlink: {root}",
        )
    if root.is_dir():
        shutil.rmtree(root)
