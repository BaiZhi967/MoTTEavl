"""Harbor（Terminal-Bench）任务来源与稳定身份（M3-T01，需求 4.1）。

准备过程**只读**：遍历受控根目录、读受控的文件字节、算 hash，绝不执行
任务包里的脚本、不 docker build、不发模型请求。固定来源的下载与解包在
``motte_benchmark.harbor.source``（同样不执行仓库脚本），本模块只负责
"读已落盘的受控字节并产生稳定身份"。

身份规则（M3-G01/G02/G03）：

- 一个任务的 ``task_content_hash`` 是**该任务目录下全部常规文件**的
  ``{相对 POSIX 路径: sha256}`` 规范 JSON 摘要，因此内容变化 = 新身份，
  单纯的时间戳/权限位变化不改变身份；
- ``task_key`` 由 source + dataset revision + 规范化相对路径 + 内容 hash
  派生：**同名不同目录的两个任务永远不会合并**，展示名不参与唯一键；
- 路径安全：每个组件必须是"单一安全组件"（不含分隔符/冒号/NUL，且不是
  当前或父目录组件），随后再对拼接结果做一次"仍在受控根内"的断言
  （``Path.is_relative_to``）。目录外的 symlink 一律拒绝
  （``O_NOFOLLOW`` 逐组件打开，见 ``motte_benchmark.trusted``）。
- 候选目录里不合任务结构的项显式登记为 ``invalid_tasks``，不静默跳过。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from motte_benchmark.protocol import BenchmarkRuntimeError
from motte_benchmark.trusted import TrustedDir, TrustedPathError

#: Harbor 判定一个目录是任务所必需的结构（与固定版本 ``Task.is_valid_dir`` 一致）。
TASK_CONFIG_NAME = "task.toml"
TASK_ENVIRONMENT_DIR = "environment"
TASK_INSTRUCTION_NAME = "instruction.md"
TASK_TESTS_DIR = "tests"
TASK_SOLUTION_DIR = "solution"

#: 只读准备的限额：任务数、单文件、单任务总量、路径长度与扫描条目数。
DEFAULT_TASK_LIMITS: dict[str, int] = {
    "max_tasks": 512,
    "max_files_per_task": 2048,
    "max_file_bytes": 8 * 1024 * 1024,
    "max_total_bytes": 256 * 1024 * 1024,
    "max_path_length": 512,
    "max_scan_entries": 20000,
}

_PLACEHOLDER_REVISIONS = {
    "", "latest", "tbd", "todo", "placeholder", "unpinned", "unknown", "n/a", "main", "master",
}

#: 单一安全路径组件：非空、不含分隔符/冒号/NUL。当前与父目录组件在
#: ``_validated_parts`` 里用 ``os.curdir`` / ``os.pardir`` 显式排除。
_SAFE_PATH_PART = re.compile(r"^[^/\\:\x00]+$")


#: 这些失败不能降级为"某个候选任务不合法"：它们说明受控边界或限额本身
#: 出了问题，必须让整次准备失败并把原因暴露给操作员。
_FATAL_SCAN_CODES = frozenset({
    "TASK_LIMIT_EXCEEDED",
    "TASK_PATH_TOO_LONG",
    "TASK_PATH_ESCAPE",
    "TASK_LIMIT_UNKNOWN",
})


class HarborTaskError(BenchmarkRuntimeError):
    """任务准备失败；``code`` 前缀进入证据（不执行任何任务内容）。"""


def _read_error(message: str) -> HarborTaskError:
    """把受控读取错误映射回语义正确的错误码（超限 ≠ 路径逃逸）。"""
    code = "TASK_LIMIT_EXCEEDED" if message.startswith("file-size:") else "TASK_PATH_ESCAPE"
    return HarborTaskError(code, message)


def _validated_parts(value: str) -> list[str]:
    """把外部路径写法拆成安全的相对组件（需求 4.1 的归一化入口）。"""
    if not isinstance(value, str) or not value.strip():
        raise HarborTaskError("TASK_PATH_INVALID", "relative path must be a nonempty string")
    candidate = value.replace("\\", "/").strip()
    if candidate.startswith("/"):
        raise HarborTaskError(
            "TASK_PATH_INVALID", f"relative path must not be absolute: {value!r}",
        )
    if len(candidate) >= 2 and candidate[1] == ":":
        raise HarborTaskError(
            "TASK_PATH_INVALID",
            f"relative path must not carry a drive letter: {value!r}",
        )
    parts: list[str] = []
    for part in candidate.split("/"):
        if part in ("", os.curdir):
            continue
        if part == os.pardir or not _SAFE_PATH_PART.match(part):
            raise HarborTaskError(
                "TASK_PATH_INVALID",
                "relative path must stay inside the controlled root; rejected component "
                f"{part!r} in {value!r}",
            )
        parts.append(part)
    if not parts:
        raise HarborTaskError("TASK_PATH_INVALID", f"relative path is empty: {value!r}")
    return parts


def normalize_relative_path(value: str) -> str:
    """外部路径写法 → 存储用的 POSIX 相对路径（Windows 分隔符在此转换一次）。"""
    return "/".join(_validated_parts(value))


def resolve_within(root: Path | str, relative_path: str) -> Path:
    """拼接并断言目标仍在受控根内；越界立即失败，绝不返回可疑路径。"""
    base = Path(root).resolve()
    target = base.joinpath(*_validated_parts(relative_path)).resolve()
    if target != base and not target.is_relative_to(base):
        raise HarborTaskError(
            "TASK_PATH_ESCAPE",
            f"resolved path escapes the controlled root: {relative_path!r}",
        )
    return target


def _require_pinned_revision(revision: str) -> str:
    if not isinstance(revision, str) or revision.strip().lower() in _PLACEHOLDER_REVISIONS:
        raise HarborTaskError(
            "TASK_REVISION_UNPINNED",
            "dataset revision must be a real pinned revision, not "
            f"{revision!r} (M3-G01: no latest/placeholder)",
        )
    return revision.strip()


def _limits(overrides: dict[str, Any] | None) -> dict[str, int]:
    limits = dict(DEFAULT_TASK_LIMITS)
    for key, value in (overrides or {}).items():
        if key not in DEFAULT_TASK_LIMITS:
            raise HarborTaskError(
                "TASK_LIMIT_UNKNOWN",
                f"unknown task preparation limit: {key!r} "
                f"(allowed: {sorted(DEFAULT_TASK_LIMITS)})",
            )
        limits[key] = int(value)
    return limits


def _trusted_error(message: str) -> Exception:
    return HarborTaskError("TASK_PATH_ESCAPE", message)


def open_controlled_root(root: Path | str) -> TrustedDir:
    """打开受控根目录；symlink 根、缺目录、非目录都显式失败。"""
    path = Path(root)
    try:
        if path.is_symlink():
            raise HarborTaskError(
                "TASK_ROOT_SYMLINK", f"controlled task root must not be a symlink: {path}",
            )
        if not path.is_dir():
            raise HarborTaskError(
                "TASK_ROOT_MISSING", f"controlled task root is not a directory: {path}",
            )
    except OSError as error:  # pragma: no cover - 权限/竞态
        raise HarborTaskError(
            "TASK_ROOT_MISSING", f"cannot stat task root {path}: {error}",
        ) from error
    try:
        return TrustedDir(path, error_factory=_trusted_error)
    except TrustedPathError as error:
        raise HarborTaskError("TASK_PATH_ESCAPE", str(error)) from error


def _hash_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def task_content_hash(file_hashes: dict[str, str]) -> str:
    """任务内容 hash：排序后的 ``{相对路径: 文件 sha256}`` 规范 JSON 摘要。"""
    if not file_hashes:
        raise HarborTaskError("TASK_EMPTY", "task directory has no regular files")
    encoded = json.dumps(
        dict(sorted(file_hashes.items())), ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _list_root_files(root: Path | str, limits: dict[str, int]) -> list[str]:
    with open_controlled_root(root) as trusted:
        files, _symlinks = trusted.list_files()
    if len(files) > limits["max_scan_entries"]:
        raise HarborTaskError(
            "TASK_LIMIT_EXCEEDED",
            f"task root has more than {limits['max_scan_entries']} entries",
        )
    return files


def _ancestors(relative_dir: str) -> list[str]:
    parts = relative_dir.split("/")
    return ["/".join(parts[:index]) for index in range(1, len(parts))]


def _candidate_dirs(files: list[str]) -> list[str]:
    """直接包含 ``task.toml`` 的目录（外层任务拥有其整棵子树）。"""
    candidates = sorted({
        rel.rsplit("/", 1)[0] for rel in files if rel.endswith("/" + TASK_CONFIG_NAME)
    })
    kept: list[str] = []
    for candidate in candidates:
        if any(candidate.startswith(owner + "/") for owner in kept):
            continue
        kept.append(candidate)
    return kept


def discover_task_dirs(
    root: Path | str, *, limits: dict[str, Any] | None = None,
) -> list[str]:
    """受控根目录下的候选任务目录（posix 相对路径，按路径排序）。

    判定标准与 Harbor 的本地任务结构一致：一个目录直接包含 ``task.toml``
    即为候选。允许嵌套（固定来源归档常见 ``sample/<name>/`` 布局），但
    **不会**把一个任务的子目录当成另一个任务——外层任务拥有其整棵子树。
    候选是否合任务结构由 ``prepare_task_manifest`` 逐项判定并登记，不在
    发现阶段静默丢弃。
    """
    resolved_limits = _limits(limits)
    files = _list_root_files(root, resolved_limits)
    kept = _candidate_dirs(files)
    if len(kept) > resolved_limits["max_tasks"]:
        raise HarborTaskError(
            "TASK_LIMIT_EXCEEDED",
            f"task root has {len(kept)} candidate tasks > limit "
            f"{resolved_limits['max_tasks']}",
        )
    return kept


def discover_stray_dirs(
    root: Path | str, candidates: list[str], *, limits: dict[str, Any] | None = None,
) -> list[str]:
    """含文件但既不是任务、也不是任务祖先的目录（缺 ``task.toml`` 的候选）。

    这些目录显式登记为 ``invalid_tasks`` 而不是被静默忽略：一次上传漏掉
    ``task.toml`` 时操作员必须看得见，而不是发现任务数"少了一个"。
    """
    resolved_limits = _limits(limits)
    files = _list_root_files(root, resolved_limits)
    owned = {
        ancestor for candidate in candidates for ancestor in _ancestors(candidate)
    }
    strays: list[str] = []
    for directory in sorted({rel.rsplit("/", 1)[0] for rel in files if "/" in rel}):
        if any(
            directory == candidate
            or directory.startswith(candidate + "/")
            or candidate.startswith(directory + "/")
            for candidate in candidates
        ):
            continue
        if directory in owned:
            continue
        strays.append(directory)
    return strays


def scan_task(
    root: Path | str, relative_dir: str, *, limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """读取一个任务目录下的全部常规文件：``{file_hashes, total_bytes, file_count}``。

    只读、不执行；单文件与单任务总量超限立即失败（不截断、不跳过文件，
    否则身份会在"静默丢文件"下漂移）。
    """
    resolved_limits = _limits(limits)
    prefix = normalize_relative_path(relative_dir)
    with open_controlled_root(root) as trusted:
        files, _symlinks = trusted.list_files()
        owned = [rel for rel in files if rel.startswith(prefix + "/")]
        if not owned:
            raise HarborTaskError("TASK_EMPTY", f"task directory has no regular files: {prefix}")
        if len(owned) > resolved_limits["max_files_per_task"]:
            raise HarborTaskError(
                "TASK_LIMIT_EXCEEDED",
                f"task {prefix} has {len(owned)} files > limit "
                f"{resolved_limits['max_files_per_task']}",
            )
        hashes: dict[str, str] = {}
        total = 0
        for rel in owned:
            inner = rel[len(prefix) + 1:]
            if len(inner) > resolved_limits["max_path_length"]:
                raise HarborTaskError(
                    "TASK_PATH_TOO_LONG",
                    f"task file path exceeds {resolved_limits['max_path_length']} chars: {inner}",
                )
            try:
                data = trusted.read_bytes(rel, max_bytes=resolved_limits["max_file_bytes"])
            except TrustedPathError as error:
                raise _read_error(str(error)) from error
            total += len(data)
            if total > resolved_limits["max_total_bytes"]:
                raise HarborTaskError(
                    "TASK_LIMIT_EXCEEDED",
                    f"task {prefix} exceeds {resolved_limits['max_total_bytes']} total bytes",
                )
            hashes[inner] = _hash_bytes(data)
    return {"file_hashes": hashes, "total_bytes": total, "file_count": len(hashes)}


def hash_task_files(
    root: Path | str, relative_dir: str, *, limits: dict[str, Any] | None = None,
) -> dict[str, str]:
    """``scan_task`` 的文件 hash 视图（身份与复验只需要 hash）。"""
    return scan_task(root, relative_dir, limits=limits)["file_hashes"]


def _read_task_toml(root: Path | str, relative_dir: str) -> dict[str, Any] | None:
    """只读解析 ``task.toml``；解析失败返回 None（结构错误由调用方判定）。"""
    try:
        with open_controlled_root(root) as trusted:
            raw = trusted.read_bytes(
                f"{relative_dir}/{TASK_CONFIG_NAME}", max_bytes=1024 * 1024,
            )
        payload = tomllib.loads(raw.decode("utf-8"))
    except (HarborTaskError, TrustedPathError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def declared_task_license(root: Path | str, relative_dir: str) -> str | None:
    """``task.toml`` 的 ``metadata.license``（任务自带许可声明，可空）。"""
    payload = _read_task_toml(root, relative_dir) or {}
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        license_id = metadata.get("license")
        if isinstance(license_id, str) and license_id.strip():
            return license_id.strip()
    return None


def declared_upstream_task_id(root: Path | str, relative_dir: str) -> str | None:
    """``task.toml`` 里声明的上游包身份（``task.name`` + ``task.version``）。"""
    payload = _read_task_toml(root, relative_dir) or {}
    section = payload.get("task")
    if not isinstance(section, dict):
        return None
    name = section.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    version = section.get("version")
    if isinstance(version, str) and version.strip():
        return f"{name.strip()}@{version.strip()}"
    return name.strip()


def prepare_task_manifest(
    root: Path | str,
    *,
    source_id: str,
    dataset_revision: str,
    license_id: str | None = None,
    license_evidence: str | None = None,
    source_kind: str = "local",
    limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """只读准备受控任务根目录，返回 ``TaskManifest`` 的 JSON 载荷。

    同一份字节重复准备得到同一 ``manifest_hash``；内容变化必须表现为新
    ``task_key``（调用方据此换新 revision，不覆盖旧身份）。
    """
    revision = _require_pinned_revision(dataset_revision)
    if not isinstance(source_id, str) or not source_id.strip():
        raise HarborTaskError("TASK_SOURCE_INVALID", "source_id must be a nonempty string")
    if source_kind not in ("local", "pinned-source"):
        raise HarborTaskError(
            "TASK_SOURCE_INVALID",
            f"source_kind must be 'local' or 'pinned-source', got {source_kind!r}",
        )
    resolved_limits = _limits(limits)
    from motte_contracts.trial import (  # noqa: PLC0415 - 契约依赖
        TaskIdentity,
        TaskManifest,
        compute_task_key,
    )

    candidates = discover_task_dirs(root, limits=resolved_limits)
    if not candidates:
        raise HarborTaskError("TASK_ROOT_EMPTY", f"no candidate task directories under {root}")

    tasks: list[TaskIdentity] = []
    file_hashes: dict[str, dict[str, str]] = {}
    display_names: dict[str, str] = {}
    task_facts: dict[str, dict[str, Any]] = {}
    invalid: list[dict[str, Any]] = [
        {
            "normalized_relative_path": stray,
            "code": "TASK_STRUCTURE_INVALID",
            "message": f"directory contains files but no {TASK_CONFIG_NAME}",
        }
        for stray in discover_stray_dirs(root, candidates, limits=resolved_limits)
    ]

    for relative_dir in candidates:
        try:
            scanned = scan_task(root, relative_dir, limits=resolved_limits)
        except HarborTaskError as error:
            if error.code in _FATAL_SCAN_CODES:
                # 限额/越界是环境或受控边界问题，不是"这个任务形状不对"：
                # 直接失败，绝不降级成 invalid 后继续，让缺项被看见。
                raise
            invalid.append({
                "normalized_relative_path": relative_dir,
                "code": error.code,
                "message": str(error),
            })
            continue
        hashes = scanned["file_hashes"]
        missing = [
            name for name in (TASK_CONFIG_NAME, TASK_INSTRUCTION_NAME)
            if name not in hashes
        ]
        has_environment = any(rel.startswith(TASK_ENVIRONMENT_DIR + "/") for rel in hashes)
        if missing or not has_environment:
            absent = sorted(missing + ([] if has_environment else [TASK_ENVIRONMENT_DIR]))
            invalid.append({
                "normalized_relative_path": relative_dir,
                "code": "TASK_STRUCTURE_INVALID",
                "message": (
                    "task directory must contain task.toml, instruction.md and an "
                    f"environment/ directory; missing: {absent}"
                ),
            })
            continue
        content_hash = task_content_hash(hashes)
        upstream_task_id = declared_upstream_task_id(root, relative_dir)
        identity = TaskIdentity(
            source_id=source_id,
            dataset_revision=revision,
            normalized_relative_path=relative_dir,
            task_content_hash=content_hash,
            task_key=compute_task_key(
                source_id=source_id,
                dataset_revision=revision,
                normalized_relative_path=relative_dir,
                task_content_hash=content_hash,
                upstream_task_id=upstream_task_id,
            ),
            upstream_task_id=upstream_task_id,
        )
        tasks.append(identity)
        file_hashes[content_hash] = hashes
        display_names[identity.task_key] = relative_dir
        task_facts[identity.task_key] = {
            "file_count": scanned["file_count"],
            "total_bytes": scanned["total_bytes"],
            "has_tests": any(rel.startswith(TASK_TESTS_DIR + "/") for rel in hashes),
            "has_solution": any(rel.startswith(TASK_SOLUTION_DIR + "/") for rel in hashes),
            "declared_license": declared_task_license(root, relative_dir),
            "normalized_relative_path": relative_dir,
        }

    if not tasks:
        raise HarborTaskError(
            "TASK_ROOT_EMPTY",
            "no valid task directories under the controlled root; invalid candidates: "
            f"{[item['normalized_relative_path'] for item in invalid]}",
        )

    manifest = TaskManifest(
        source_id=source_id,
        dataset_revision=revision,
        tasks=tasks,
        display_names=display_names,
        file_hashes=file_hashes,
        task_facts=task_facts,
        invalid_tasks=invalid,
        license_id=license_id,
        license_evidence=license_evidence,
        source_kind=source_kind,  # type: ignore[arg-type]
        prepared_at=datetime.now(timezone.utc).isoformat(),
        limits=resolved_limits,
    )
    payload = manifest.model_dump(mode="json")
    payload["manifest_hash"] = manifest.manifest_hash
    return payload


def verify_task_manifest(root: Path | str, manifest: dict[str, Any]) -> dict[str, Any]:
    """重新读取受控根目录，核对已准备身份仍然成立（只读复验）。

    内容变化、文件增删、目录消失都列出具体 task_key 与差异；调用方据此换
    新 revision，绝不覆盖旧证据。
    """
    from motte_contracts.trial import TaskManifest  # noqa: PLC0415 - 契约依赖

    parsed = TaskManifest.model_validate({
        key: value for key, value in manifest.items() if key != "manifest_hash"
    })
    limits = {**DEFAULT_TASK_LIMITS, **(parsed.limits or {})}
    mismatches: list[dict[str, Any]] = []
    for task in parsed.tasks:
        try:
            hashes = hash_task_files(root, task.normalized_relative_path, limits=limits)
        except HarborTaskError as error:
            mismatches.append({
                "task_key": task.task_key,
                "normalized_relative_path": task.normalized_relative_path,
                "code": error.code,
                "message": str(error),
            })
            continue
        expected = parsed.file_hashes.get(task.task_content_hash, {})
        if hashes != expected:
            changed = sorted(
                rel for rel in set(hashes) | set(expected)
                if hashes.get(rel) != expected.get(rel)
            )
            mismatches.append({
                "task_key": task.task_key,
                "normalized_relative_path": task.normalized_relative_path,
                "code": "TASK_CONTENT_CHANGED",
                "message": "task bytes differ from the prepared identity",
                "changed_files": changed,
                "recomputed_content_hash": task_content_hash(hashes),
            })
    return {"ok": not mismatches, "mismatches": mismatches, "manifest_hash": parsed.manifest_hash}


def select_tasks(
    manifest: dict[str, Any], *, task_keys: list[str] | None = None,
) -> list[dict[str, Any]]:
    """按显式 task_key 选择任务（省略=全部）；未知 key 显式失败。"""
    tasks = manifest.get("tasks") or []
    if not task_keys:
        return list(tasks)
    from motte_contracts.trial import TaskIdentity  # noqa: PLC0415 - 契约依赖

    by_key = {TaskIdentity.model_validate(task).task_key: task for task in tasks}
    unknown = [key for key in task_keys if key not in by_key]
    if unknown:
        raise HarborTaskError(
            "TASK_KEY_UNKNOWN",
            f"selected task keys are not in the prepared manifest: {unknown}",
        )
    return [by_key[key] for key in task_keys]


def task_key_of(manifest: dict[str, Any], normalized_relative_path: str) -> str:
    """相对路径 → task_key（精确匹配，不做 basename 近似）。"""
    from motte_contracts.trial import TaskIdentity  # noqa: PLC0415 - 契约依赖

    wanted = normalize_relative_path(normalized_relative_path)
    for task in manifest.get("tasks") or []:
        identity = TaskIdentity.model_validate(task)
        if identity.normalized_relative_path == wanted:
            return identity.task_key
    raise HarborTaskError(
        "TASK_KEY_UNKNOWN", f"no prepared task at relative path {wanted!r}",
    )


def basename_counts(manifest: dict[str, Any]) -> dict[str, int]:
    """basename → 出现次数；>1 表示 Harbor 的 ``task_name`` 不足以定位任务。"""
    counts: dict[str, int] = {}
    for task in manifest.get("tasks") or []:
        name = Path(normalize_relative_path(task["normalized_relative_path"])).name
        counts[name] = counts.get(name, 0) + 1
    return counts


def directory_hashes(path: Path | str) -> dict[str, str]:
    """受控目录下所有常规文件的 ``{相对路径: sha256}``（Runner 侧证据核对用）。"""
    with open_controlled_root(path) as trusted:
        files, _symlinks = trusted.list_files()
        return {
            rel: _hash_bytes(trusted.read_bytes(rel, max_bytes=64 * 1024 * 1024))
            for rel in files
        }


def file_sha256(path: Path) -> str:
    """单个文件的 sha256（下载校验用；不做目录遍历）。"""
    try:
        with open(path, "rb") as handle:
            digest = hashlib.sha256()
            while True:
                chunk = handle.read(1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as error:
        raise HarborTaskError(
            "TASK_SOURCE_UNREADABLE", f"cannot read {path}: {error}",
        ) from error
    return "sha256:" + digest.hexdigest()


def environment_digest(
    *,
    harbor_version: str,
    terminal_bench_revision: str,
    docker_platform: str | None,
    image_digest: str | None,
    agent_id: str,
    agent_version: str,
    extra: dict[str, Any] | None = None,
) -> str:
    """环境 fingerprint：Harbor 版本、数据 revision、镜像与 Agent 身份。

    安全/可见性配置与上游不同时必须传入不同的 ``extra``，从而产生不同
    digest，不能继续冒充官方同口径（需求第 6 节末）。
    """
    from motte_contracts.trial import canonical_hash  # noqa: PLC0415 - 契约依赖

    for label, value in (
        ("harbor_version", harbor_version),
        ("terminal_bench_revision", terminal_bench_revision),
        ("agent_id", agent_id),
        ("agent_version", agent_version),
    ):
        if not isinstance(value, str) or not value.strip():
            raise HarborTaskError(
                "TASK_ENVIRONMENT_UNPINNED", f"{label} must be pinned to a real value",
            )
    return canonical_hash({
        "schema": "motte-harbor-environment@1",
        "harbor_version": harbor_version,
        "terminal_bench_revision": terminal_bench_revision,
        "docker_platform": docker_platform,
        "image_digest": image_digest,
        "agent_id": agent_id,
        "agent_version": agent_version,
        "extra": extra or {},
    })
