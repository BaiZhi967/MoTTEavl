"""Harbor Runner 桥接（在固定 Harbor 环境的解释器里执行，M3-T06）。

职责（保持最小）：

1. 读取平台冻结的 ``harbor/config.json`` 与 ``harbor/plan.json``；
2. **启动前**复验不可变任务副本 ``frozen-tasks/``：逐文件 hash 与
   ``canonical_hash(plan["task_files"])`` 一致、无 symlink、无多余文件——
   漂移一律退出码 5，绝不启动 Job（review R03）；
3. 用锁定版本的真实 Harbor 模型构造原生 ``JobConfig``——构造即校验，未知
   字段/非法组合在启动前失败，不把"字符串看起来对"当成可执行；同时把平台
   自己的所有权 overlay（``motte.job``/``motte.run``/``motte.owner`` 标签）
   放进 ``EnvironmentConfig.extra_docker_compose``（review R04）；
4. **在 ``Job.run()`` 之前**就把 Job 目录位置与计划 Trial 名写进
   ``harbor/job-location.json``；注册 TERM/INT handler，收到信号时重写定位
   文件、写完成标记（143/130）后退出——不依赖 Python ``finally``，硬终止
   也不会丢掉已完成 Trial 的采集入口（review R05）；
5. 运行 Job，并把 **Harbor 自己的 Job 目录位置**写进定位文件；证据不在这里
   复制，由平台侧的受控读取器（``TrustedDir``）从该目录冻结；
6. 原子写完成标记（含退出码），让平台能区分"跑完了"与"进程消失了"。

本模块不 import 平台其他包（Runner 环境只装 harbor 与 ``motte_benchmark.harbor``
的桥接模块），不读取平台数据库，不使用宿主凭据。所有路径都经过
``safe_child`` / ``safe_join`` 校验，越界立即失败。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

PLAN_NAME = "harbor/plan.json"
CONFIG_NAME = "harbor/config.json"
LOCATION_NAME = "harbor/job-location.json"
#: Agent 名 → 固定 Harbor 版本的类路径（与平台 AGENT_SPECS 的 harbor_name 对应）。
#: 只列平台 allowlist 里的 Agent：不做动态发现，避免"配置文件里写什么就加载什么"。
AGENT_IMPORT_PATHS: dict[str, tuple[str, str, str]] = {
    "oracle": ("harbor.agents.oracle", "OracleAgent", "OracleAgent"),
    "claude-code": (
        "harbor.agents.installed.claude_code", "ClaudeCode", "ClaudeCode",
    ),
}
FROZEN_NAME = "harbor/frozen-tasks.json"
OVERLAY_NAME = "harbor/ownership-overlay.yaml"
#: 环境边界观察记录（review R2-05）：Runner 交给 compose 的变量名与来源。
ENV_BOUNDARY_NAME = "harbor/env-boundary.json"
BOUNDARY_SCHEMA = "motte-runner-env-boundary@1"
#: 平台注入的桥接变量名（身份与受控路径）：这些名字由平台本次启动生成。
BRIDGE_ENV_NAMES: tuple[str, ...] = (
    "MOTTE_WORK_DIR", "MOTTE_JOB_ID", "MOTTE_RUN_ID", "MOTTE_LAUNCH_TOKEN",
    "MOTTE_TASK_ROOT", "MOTTE_RUNNER_PYTHON", "MOTTE_RUNNER_ROOT",
)
COMPLETION_MARKER = ".motte-job-complete"
#: 冻结任务副本的目录名与清单（平台侧 ``frozen.py`` 用同一份 schema）。
FROZEN_TASKS_DIR = "frozen-tasks"
FROZEN_MANIFEST_NAME = "manifest.json"
FROZEN_SCHEMA = "motte-frozen-tasks@1"
#: Harbor 容器所有权标签前缀（平台侧按标签定向停止/残留检查）。
OWNER_LABEL_JOB = "motte.job"
OWNER_LABEL_RUN = "motte.run"
OWNER_LABEL_OWNER = "motte.owner"


class RunnerBridgeError(RuntimeError):
    """Runner 侧配置/布局错误（退出码 4/5，绝不静默继续）。"""


def safe_child(base: Path, name: str) -> Path:
    """受控子路径：``name`` 必须是单一安全组件，且结果仍在 ``base`` 内。"""
    if not isinstance(name, str) or not name:
        raise RunnerBridgeError("path component must be a nonempty string")
    if name in (os.curdir, os.pardir) or "/" in name or "\\" in name or "\x00" in name:
        raise RunnerBridgeError(f"unsafe path component: {name!r}")
    root = Path(base).resolve()
    target = root.joinpath(name).resolve()
    if target != root and not target.is_relative_to(root):
        raise RunnerBridgeError(f"path escapes {root}: {name!r}")
    return target


def safe_join(base: Path, relative: str) -> Path:
    """受控相对路径（可含多级）：逐段校验后拼接，保证不越界。"""
    current = Path(base)
    for part in str(relative).replace("\\", "/").split("/"):
        if part:
            current = safe_child(current, part)
    return current


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RunnerBridgeError(f"{path} must contain a JSON object")
    return payload


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def write_completion_marker(work_dir: Path, exit_code: int, detail: str = "") -> None:
    """原子完成标记：平台凭它区分正常结束与进程消失。"""
    marker = safe_join(work_dir, COMPLETION_MARKER)
    temporary = safe_join(work_dir, COMPLETION_MARKER + ".partial")
    temporary.write_text(
        json.dumps(
            {"exit_code": exit_code, "completed": True, "detail": detail},
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    temporary.replace(marker)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------ 冻结副本


def canonical_hash(payload: Any) -> str:
    """规范 JSON（排序、紧凑、UTF-8）的 sha256——与平台 ``canonical_hash`` 一致。

    这里重写一份而不是 import：Runner 环境只拷贝本包，不含平台契约包。
    """
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _walk_frozen_task(task_dir: Path) -> tuple[dict[str, str], list[str]]:
    """冻结任务目录下的 ``{相对路径: sha256}`` 与遇到的 symlink（拒绝清单）。"""
    hashes: dict[str, str] = {}
    symlinks: list[str] = []
    for base, dirnames, filenames in os.walk(task_dir, followlinks=False):
        for name in list(dirnames):
            target = Path(base) / name
            if target.is_symlink():
                symlinks.append(target.relative_to(task_dir).as_posix())
        # symlink 目录不下钻：冻结副本里出现链接就是漂移，不是"另一个任务目录"。
        dirnames[:] = [name for name in dirnames if not (Path(base) / name).is_symlink()]
        for name in filenames:
            target = Path(base) / name
            relative = target.relative_to(task_dir).as_posix()
            if target.is_symlink() or not target.is_file():
                symlinks.append(relative)
                continue
            hashes[relative] = _file_sha256(target)
    return (hashes, sorted(symlinks))


def verify_frozen_tasks(
    work_dir: Path, data_root: Path, plan: Mapping[str, Any],
) -> dict[str, Any]:
    """启动前复验不可变任务副本；返回可审计的核验记录（不一致即抛错）。"""
    manifest_path = safe_join(data_root, FROZEN_MANIFEST_NAME)
    try:
        manifest = load_json(manifest_path)
    except (OSError, ValueError, RunnerBridgeError) as error:
        raise RunnerBridgeError(f"frozen task manifest unreadable: {error}") from error
    if manifest.get("schema") != FROZEN_SCHEMA:
        raise RunnerBridgeError(
            f"frozen task manifest schema must be {FROZEN_SCHEMA}, "
            f"got {manifest.get('schema')!r}",
        )
    task_files = plan.get("task_files")
    if not isinstance(task_files, Mapping) or not task_files:
        raise RunnerBridgeError("plan.task_files is required to verify frozen tasks")
    expected_manifest_hash = canonical_hash(task_files)
    if manifest.get("task_files_hash") != expected_manifest_hash:
        raise RunnerBridgeError(
            "frozen task set does not match the frozen plan: "
            f"manifest={manifest.get('task_files_hash')} plan={expected_manifest_hash}",
        )
    frozen_tasks = manifest.get("tasks")
    if not isinstance(frozen_tasks, Mapping):
        raise RunnerBridgeError("frozen task manifest must contain a tasks mapping")
    differences: list[dict[str, Any]] = []
    files_verified = 0
    for task_key, entry in sorted(frozen_tasks.items(), key=lambda item: str(item[0])):
        if not isinstance(entry, Mapping):
            raise RunnerBridgeError(f"frozen task entry must be an object: {task_key}")
        directory = entry.get("directory")
        if not isinstance(directory, str) or not directory:
            raise RunnerBridgeError(f"frozen task entry has no directory: {task_key}")
        task_dir = safe_child(data_root, directory)
        if not task_dir.is_dir():
            differences.append({"task_key": str(task_key), "code": "TASK_DIR_MISSING"})
            continue
        observed, symlinks = _walk_frozen_task(task_dir)
        expected = {
            str(rel): str(digest)
            for rel, digest in dict(entry.get("files") or {}).items()
        }
        if symlinks:
            differences.append({
                "task_key": str(task_key), "code": "TASK_FILE_SYMLINK",
                "paths": symlinks[:5],
            })
        for relative in sorted(set(expected) | set(observed)):
            if observed.get(relative) != expected.get(relative):
                differences.append({
                    "task_key": str(task_key), "code": "TASK_FILE_DRIFT",
                    "relative_path": relative,
                    "expected": expected.get(relative),
                    "observed": observed.get(relative),
                })
        files_verified += len(expected)
    record = {
        "schema": FROZEN_SCHEMA,
        "verified_at": now_iso(),
        "files_verified": files_verified,
        "task_count": len(frozen_tasks),
        "task_files_hash": expected_manifest_hash,
        "manifest_hash": manifest.get("manifest_hash"),
        "ok": not differences,
        "differences": differences[:50],
    }
    write_json(safe_join(work_dir, FROZEN_NAME), record)
    if differences:
        raise RunnerBridgeError(
            f"frozen task copy drifted before start: {differences[:5]}",
        )
    return record


# ------------------------------------------------------------------ 配置投影


def frozen_task_directories(data_root: Path) -> dict[str, str]:
    """冻结清单里的 ``{normalized_relative_path: 目录名}``（执行只读这些目录）。"""
    try:
        manifest = load_json(safe_join(data_root, FROZEN_MANIFEST_NAME))
    except (OSError, ValueError, RunnerBridgeError) as error:
        raise RunnerBridgeError(f"frozen task manifest unreadable: {error}") from error
    tasks = manifest.get("tasks")
    if not isinstance(tasks, Mapping):
        raise RunnerBridgeError("frozen task manifest must contain a tasks mapping")
    out: dict[str, str] = {}
    for entry in tasks.values():
        if not isinstance(entry, Mapping):
            continue
        relative = entry.get("relative_path")
        directory = entry.get("directory")
        if isinstance(relative, str) and isinstance(directory, str) and directory:
            out[relative] = directory
    return out


def resolved_task_paths(
    config: Mapping[str, Any], data_root: Path, plan: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """把冻结配置里的任务路径投影成**冻结副本**的绝对路径。

    ``config["job"]["tasks"][].path`` 是任务在源根下的相对路径；执行只允许
    读冻结副本（``frozen-tasks/<目录>``，目录名由清单给出，因为 ``task_key``
    里的冒号会破坏 docker compose 的挂载规格）。因此这里按冻结清单把相对路径
    换成副本目录——不对冻结配置做任何改写（配置本身仍是可移植的相对路径）。
    """
    root = Path(data_root).resolve()
    directories = frozen_task_directories(root)
    entries: list[dict[str, Any]] = []
    for entry in config["job"]["tasks"]:
        relative = str(entry["path"])
        directory = directories.get(relative)
        if directory is None:
            raise RunnerBridgeError(
                f"task path {relative!r} is not part of the frozen plan; "
                "refusing to execute an unplanned task",
            )
        candidate = safe_child(root, directory)
        if not candidate.is_dir():
            raise RunnerBridgeError(f"frozen task directory is missing: {directory}")
        entries.append({"path": str(candidate), "source": entry.get("source")})
    return entries


def job_paths(config: Mapping[str, Any], work_dir: Path) -> tuple[Path, str]:
    """Job 目录与 Job 名；两者都必须落在受控工作目录内。"""
    work = Path(work_dir).resolve()
    raw_jobs_dir = str(config["job"]["jobs_dir"])
    jobs_dir = safe_join(work, raw_jobs_dir).resolve()
    if jobs_dir != work and work not in jobs_dir.parents:
        raise RunnerBridgeError(f"jobs dir must stay inside the work dir: {jobs_dir}")
    job_name = str(config["job"]["job_name"])
    safe_child(jobs_dir, job_name)
    return jobs_dir, job_name


def write_ownership_overlay(
    work_dir: Path, *, job_id: str, run_id: str, owner_token: str,
) -> Path:
    """写平台自己的 compose overlay：给 Harbor 的 main 服务打所有权标签。

    Harbor 的 docker 环境一律用 ``docker compose --project-name <sanitized
    session_id>``，且 ``EnvironmentConfig.extra_docker_compose`` 会被追加到任务
    compose 之后（``docker.py::_docker_compose_paths``），所以这份 overlay 对
    "只有 Dockerfile 的任务"同样适用，不改变执行路径。

    ``motte.owner`` 写的是 launch token 的**摘要**而不是原始 token：标签会留在
    容器元数据里，原始 token 只留在平台受控记录中（可重算摘要核验身份）。
    """
    if not job_id or not owner_token:
        raise RunnerBridgeError("ownership labels require job_id and owner token")
    owner_label = "sha256:" + hashlib.sha256(owner_token.encode("utf-8")).hexdigest()[:32]
    document = {
        "services": {
            "main": {
                "labels": {
                    OWNER_LABEL_JOB: job_id,
                    OWNER_LABEL_RUN: run_id,
                    OWNER_LABEL_OWNER: owner_label,
                },
            },
        },
    }
    path = safe_join(work_dir, OVERLAY_NAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = safe_join(work_dir, OVERLAY_NAME + ".partial")
    temporary.write_text(_dump_simple_yaml(document), encoding="utf-8")
    temporary.replace(path)
    return path


def _dump_simple_yaml(document: Mapping[str, Any]) -> str:
    """写一份极简 YAML（不依赖 Runner 环境的 yaml 版本与序列化细节）。"""
    lines = [
        "# MoTTEavl 平台所有权 overlay（M3 review R04）：由 Runner 生成，平台侧核验。",
        "services:",
    ]
    for service, body in document["services"].items():
        lines.append(f"  {service}:")
        for key, value in body.items():
            if isinstance(value, Mapping):
                lines.append(f"    {key}:")
                for inner_key, inner_value in value.items():
                    lines.append(f"      {inner_key}: {json.dumps(str(inner_value))}")
            else:
                lines.append(f"    {key}: {json.dumps(value)}")
    return "\n".join(lines) + "\n"


def build_job_config(
    config: Mapping[str, Any], data_root: Path, work_dir: Path,
    plan: Mapping[str, Any], overlay: Path,
) -> Any:
    """用真实 Harbor 模型构造并校验原生 JobConfig（锁定版本）。"""
    from harbor.models.job.config import JobConfig  # noqa: PLC0415 - Runner 侧依赖

    jobs_dir, _job_name = job_paths(config, work_dir)
    job = dict(config["job"])
    job["tasks"] = resolved_task_paths(config, data_root, plan)
    job["jobs_dir"] = str(jobs_dir)
    environment = dict(job.get("environment") or {})
    extra = [str(item) for item in (environment.get("extra_docker_compose") or [])]
    if str(overlay) not in extra:
        extra.append(str(overlay))
    environment["extra_docker_compose"] = extra
    job["environment"] = environment
    return JobConfig.model_validate(job)


def compose_project_name(trial_name: str) -> str:
    """Harbor 的 compose project 名：``sanitize(f"{trial_name}__env")``。

    规则取自固定版本 ``docker.py::_sanitize_docker_compose_project_name``：
    小写、首字符非字母数字补 ``0``、非 ``[a-z0-9_-]`` 换 ``-``。
    """
    name = f"{trial_name}__env".lower()
    if not name or not name[0].isalnum() or not name[0].isascii():
        name = "0" + name
    return "".join(
        character if (character.isascii() and (character.isalnum() or character in "_-"))
        else "-"
        for character in name
    )


def trial_names_of(job: Any, job_dir: Path) -> list[str]:
    """计划 Trial 名（Harbor 在 ``Job.create`` 时生成，平台事先无法预测）。"""
    names: list[str] = []
    for config in getattr(job, "_trial_configs", None) or []:
        name = getattr(config, "trial_name", None)
        if isinstance(name, str) and name:
            names.append(name)
    if names:
        return sorted(set(names))
    # 兜底：从 Job 目录已有 Trial 子目录读取（恢复场景）。
    try:
        return sorted(
            path.name for path in job_dir.iterdir()
            if path.is_dir() and (path / "result.json").is_file()
        )
    except OSError:
        return []


def record_job_location(
    work_dir: Path, jobs_dir: Path, job_name: str, *, reused: bool,
    started: bool = False, trial_names: Iterable[str] = (),
    planned_tasks: Iterable[str] = (), started_at: str | None = None,
) -> dict[str, Any]:
    """记录 Job 目录位置（证据由平台侧从这个目录冻结）。

    ``started=True`` 的版本在 ``Job.run()`` **之前**就写：即使进程随后被
    TERM/KILL 硬终止，平台也能定位 Job 目录并冻结已完成 Trial 的证据。
    """
    job_dir = safe_child(jobs_dir, job_name)
    names = sorted({str(name) for name in trial_names})
    tasks = sorted({str(name) for name in planned_tasks})
    trials = sorted(
        path.name for path in job_dir.iterdir()
        if path.is_dir() and safe_join(path, "result.json").is_file()
    ) if job_dir.is_dir() else []
    payload = {
        "job_dir": str(job_dir),
        "jobs_dir": str(jobs_dir),
        "job_name": job_name,
        "started": started,
        "reused_existing_job_dir": reused,
        "trials": trials,
        # Harbor 生成的 Trial 名（含随机后缀）+ 对应 compose project 名：
        # 平台据此定向停止本 Job 的容器（review R04）。
        "trial_names": names,
        "compose_projects": [compose_project_name(name) for name in names],
        "planned_task_names": tasks,
        "written_at": now_iso(),
    }
    if started_at:
        payload["started_at"] = started_at
    write_json(safe_join(work_dir, LOCATION_NAME), payload)
    return payload


async def run_job(job_config: Any, work_dir: Path, jobs_dir: Path, job_name: str) -> Any:
    from harbor.job import Job  # noqa: PLC0415 - Runner 侧依赖

    job = await Job.create(job_config)
    # Job.create 已确定 Job 目录与 Trial 名：先落盘，再开始执行（review R05）。
    known = read_location(work_dir)
    location = record_job_location(
        work_dir, jobs_dir, job_name, reused=False, started=True,
        trial_names=trial_names_of(job, safe_child(jobs_dir, job_name)),
        planned_tasks=known.get("planned_task_names") or (),
    )
    _install_signal_handlers(work_dir, jobs_dir, job_name)
    _STATE["location"] = location
    return await job.run()


#: 信号 handler 需要的跨调用状态（模块级，因为 handler 不带上下文）。
_STATE: dict[str, Any] = {"location": {}, "signalled": False}


def _known_names(work_dir: Path, key: str) -> list[str]:
    """已记录的 Trial 名/任务名（_STATE 优先，其次已落盘的定位文件）。"""
    state = _STATE.get("location") or {}
    values = state.get(key)
    if not values:
        values = read_location(work_dir).get(key)
    return [str(item) for item in (values or [])]


def read_location(work_dir: Path) -> dict[str, Any]:
    """读回已落盘的定位文件（信号路径用它保留已知 Trial 名与 project 集合）。"""
    try:
        return load_json(safe_join(work_dir, LOCATION_NAME))
    except (OSError, ValueError, RunnerBridgeError):
        return {}


def _install_signal_handlers(work_dir: Path, jobs_dir: Path, job_name: str) -> None:
    """TERM/INT：重写定位文件 + 写完成标记后退出，绝不依赖 ``finally``。"""
    def _handler(signum: int, _frame: Any) -> None:
        if _STATE.get("signalled"):
            return
        _STATE["signalled"] = True
        exit_code = 143 if signum == getattr(signal, "SIGTERM", 15) else 130
        # 已落盘的信息优先（handler 可能在任何 _STATE 赋值之前触发）：重写时
        # 不能把已经知道的 Trial 名/compose project 丢掉（review R04/R05）。
        known = dict(_STATE.get("location") or {}) or read_location(work_dir)
        try:
            located = record_job_location(
                work_dir, jobs_dir, job_name, reused=False, started=True,
                trial_names=known.get("trial_names") or (),
                planned_tasks=known.get("planned_task_names") or (),
            )
        except Exception:  # noqa: BLE001 - 信号路径尽力记录，绝不吞掉退出
            located = {"trials": []}
        try:
            write_json(safe_join(work_dir, "harbor/signal.json"), {
                "signal": int(signum),
                "exit_code": exit_code,
                "trials": located.get("trials") or [],
                "at": now_iso(),
            })
            write_completion_marker(
                work_dir, exit_code,
                f"runner received signal {signum}; partial trials kept",
            )
        finally:
            os._exit(exit_code)

    for name in ("SIGTERM", "SIGINT"):
        signum = getattr(signal, name, None)
        if signum is None:  # pragma: no cover - 非 POSIX
            continue
        try:
            signal.signal(signum, _handler)
        except (ValueError, OSError):  # pragma: no cover - 非主线程
            continue


VALIDATION_NAME = "harbor/config-validation.json"


def clear_stale_validation(work_dir: Path) -> None:
    """校验失败时删掉上一次的校验结论，避免旧的"已验证"冒充本次结果。"""
    try:
        safe_join(work_dir, VALIDATION_NAME).unlink(missing_ok=True)
    except (OSError, RunnerBridgeError):  # pragma: no cover - 清理失败不掩盖原错误
        pass


def validate_agent_config(job_config: Any, work_dir: Path) -> dict[str, Any]:
    """用**真实 Harbor 的 Agent 选项模型**校验 Agent 段（review R10，零执行）。

    ``JobConfig`` 只校验字段形状；"``kwargs.version`` 真的会被这个 Agent 接受"
    只有在 Agent 自己的 options 模型上才成立（``AgentOptions`` 是
    ``extra='forbid'``）。这里刻意**不构造 Agent**：``OracleAgent`` 这类构造需要
    task_dir/trial_paths，构造失败会把一个本来合法的校准 Run 判成配置错误
    （真实链路回归）。校验只做两件事：Agent 名可解析 + kwargs 通过 options 模型。
    """
    from harbor.agents.options import (  # noqa: PLC0415 - Runner 侧依赖
        compile_cli_from_options,
        compile_env_from_options,
    )
    from harbor.models.agent.name import AgentName  # noqa: PLC0415 - Runner 侧依赖

    summary: dict[str, Any] = {"agents": [], "options": {}}
    for agent_config in job_config.agents:
        name = str(agent_config.name or "")
        try:
            agent_name = AgentName(name)
        except ValueError as error:
            raise RunnerBridgeError(f"harbor does not know agent {name!r}") from error
        module_path, _, class_name = AGENT_IMPORT_PATHS.get(agent_name.value, ("", "", ""))
        if not module_path:
            raise RunnerBridgeError(f"agent {name!r} has no pinned import path")
        module = importlib.import_module(module_path)
        agent_class = getattr(module, class_name)
        kwargs = dict(agent_config.kwargs or {})
        # ``parse_options`` 就是 Harbor 解析 AgentConfig.kwargs 的同一入口
        # （第二个参数是 env 映射）：未知字段/类型错误在这里失败，而不是在
        # 容器里安装到一半才失败。
        options = agent_class.parse_options(kwargs, os.environ)
        summary["agents"].append({
            "name": name,
            "model_name": agent_config.model_name,
            "agent_class": class_name,
            "cli_version": kwargs.get("version"),
            "options_validated": True,
            # 实际生效的 Agent 执行参数（review R2-11）：逐字段核对"配置 → 下发参数"，
            # 而不是只断言冻结 Profile 里有这些字段。这些选项都是模型/工具/预算旋钮，
            # 不含任何秘密。
            "applied_options": options.model_dump(exclude_none=True) if options else None,
            "applied_env": compile_env_from_options(options) if options else None,
            "applied_cli": compile_cli_from_options(options) if options else None,
            "agent_env": dict(agent_config.env or {}),
        })
        if options is not None:
            summary["options"].update(options.model_dump(exclude_none=True))
    if not summary["agents"]:
        raise RunnerBridgeError("job config has no agent to validate")
    return summary


def credential_requirement(plan: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    """凭据引用解析：只检查引用指向的环境变量**存在**，绝不记录值（R10）。

    平台只冻结 ``{"ref": "env:NAME"}``；Harbor 的 Agent 从 Runner 进程环境
    读取真实值。若变量缺失，这个 Job 一定会在调用模型时失败——所以在启动前
    具名拒绝，而不是让用户看到一个含糊的 provider 错误。
    """
    refs = config.get("credentials")
    refs = refs if isinstance(refs, Mapping) else {}
    envs: dict[str, str] = {}
    for name, ref in refs.items():
        if isinstance(ref, str) and ref.startswith("env:"):
            envs[str(name)] = ref.removeprefix("env:")
    missing = sorted(env for env in envs.values() if not os.environ.get(env))
    return {
        "referenced": dict(envs),
        "missing": missing,
        "resolved": not missing,
    }


def record_env_boundary(work_dir: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    """记录"compose 能看到的变量名"（review R2-05，只记名字不记值）。

    Harbor 0.23.0 的 ``_run_docker_compose_command`` 用
    ``_compose_env_vars(include_os_env=True)``：``docker compose`` 子进程拿到的
    就是本进程的 ``os.environ``，因此任务 compose 的插值/透传能看到的变量名
    就是这个集合。平台在**下发前**已经裁剪过一次（``env_boundary`` 的
    platform-to-runner 边界：白名单 + 声明的 ``env:NAME`` 引用 + 平台注入项），
    这里把实际可见的名字与冻结配置里声明的凭据名如实落盘，供平台当证据冻结并
    核验（判定留给平台：Runner 侧刻意不复制凭据名判定逻辑）。
    """
    refs = config.get("credentials")
    refs = refs if isinstance(refs, Mapping) else {}
    declared = sorted(
        str(ref).removeprefix("env:") for ref in refs.values()
        if isinstance(ref, str) and str(ref).startswith("env:")
    )
    payload = {
        "schema": BOUNDARY_SCHEMA,
        "boundary": "runner-to-compose",
        "recorded_at": now_iso(),
        # compose 子进程环境 = 本进程 os.environ（含 Harbor 自己注入的 infra 变量，
        # 那些由 Harbor 写入 compose 环境，不是宿主秘密）。
        "compose_env_inherits_os_env": True,
        "runner_env_names": sorted(os.environ),
        "declared_credentials": declared,
        "missing_declared_credentials": sorted(
            name for name in declared if not os.environ.get(name)
        ),
        "bridge_env_names": sorted(
            name for name in BRIDGE_ENV_NAMES if os.environ.get(name) is not None
        ),
    }
    write_json(safe_join(work_dir, ENV_BOUNDARY_NAME), payload)
    return payload


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MoTTEavl Harbor runner bridge")
    parser.add_argument("--launch-token", default="")
    parser.add_argument("--job-id", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument(
        "--validate-config", action="store_true",
        help="只做原生配置校验（零执行、零模型调用），结果写 harbor/config-validation.json",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    work_dir = Path(os.environ.get("MOTTE_WORK_DIR", "")).resolve()
    if not work_dir.is_dir():
        print("MOTTE_WORK_DIR is required and must exist", file=sys.stderr)
        return 4
    # 启动身份留在 argv/env 里，跨实例可核验（与 M2 R3-03 同款）。
    token = args.launch_token or os.environ.get("MOTTE_LAUNCH_TOKEN", "")
    if not token:
        write_completion_marker(work_dir, 4, "launch token missing")
        print("launch token missing", file=sys.stderr)
        return 4

    try:
        config = load_json(safe_join(work_dir, CONFIG_NAME))
        plan = load_json(safe_join(work_dir, PLAN_NAME))
    except (OSError, ValueError, RunnerBridgeError) as error:
        write_completion_marker(work_dir, 4, f"frozen config unreadable: {error}")
        print(f"frozen config unreadable: {error}", file=sys.stderr)
        return 4

    job_id = args.job_id or os.environ.get("MOTTE_JOB_ID", "")
    run_id = args.run_id or os.environ.get("MOTTE_RUN_ID", "")
    write_json(safe_join(work_dir, "harbor/launch-identity.json"), {
        "job_id": job_id,
        "run_id": run_id,
        "launch_token": token,
        "work_dir": str(work_dir),
        "harbor_version": config.get("harbor_version"),
        "config_hash": config.get("config_hash"),
        "started_at": now_iso(),
    })

    data_root = Path(
        os.environ.get("MOTTE_TASK_ROOT") or safe_join(work_dir, FROZEN_TASKS_DIR),
    ).resolve()
    try:
        jobs_dir, job_name = job_paths(config, work_dir)
        # 启动前复验冻结副本；漂移/缺副本一律退出码 5，不启动 Job（review R03）。
        frozen = verify_frozen_tasks(work_dir, data_root, plan)
        overlay = write_ownership_overlay(
            work_dir, job_id=job_id, run_id=run_id, owner_token=token,
        )
        job_config = build_job_config(config, data_root, work_dir, plan, overlay)
        credentials = credential_requirement(plan, config)
        # 启动 Harbor 之前先落盘环境边界观察（review R2-05）：只校验不执行时也写，
        # 让平台能核验"compose 究竟能看到哪些变量名"。
        env_boundary = record_env_boundary(work_dir, config)
        agent_summary = validate_agent_config(job_config, work_dir)
    except Exception as error:  # noqa: BLE001 - 原生校验失败必须显式落库
        clear_stale_validation(work_dir)
        write_completion_marker(work_dir, 5, f"native config rejected: {error}")
        print(f"native config rejected: {error}", file=sys.stderr)
        return 5

    if credentials["missing"]:
        # 凭据引用只指向环境变量名；缺失就是"跑起来必然失败"，启动前拒绝。
        clear_stale_validation(work_dir)
        write_completion_marker(
            work_dir, 5, f"credential refs unresolved: {credentials['missing']}",
        )
        print(
            "credential refs unresolved: " + ", ".join(credentials["missing"]),
            file=sys.stderr,
        )
        return 5

    if args.validate_config:
        # 只校验不执行：真实 Harbor 模型 + Agent 的 options 模型都接受这份冻结配置。
        summary = {
            "schema": "motte-harbor-config-validation@1",
            "validated_at": now_iso(),
            "harbor_version": config.get("harbor_version"),
            "config_hash": config.get("config_hash"),
            "frozen_files_verified": frozen.get("files_verified"),
            "agents": agent_summary["agents"],
            "credentials": credentials,
            # 实际下发的 Agent 执行参数（来自真实 options 模型，零模型调用）。
            "agent_options": agent_summary.get("options"),
            "env_boundary": {
                "runner_env_names": env_boundary["runner_env_names"],
                "declared_credentials": env_boundary["declared_credentials"],
                "bridge_env_names": env_boundary["bridge_env_names"],
            },
            "job_name": job_name,
            "jobs_dir": str(jobs_dir),
            "planned_trials": plan.get("planned_trial_count"),
        }
        write_json(safe_join(work_dir, VALIDATION_NAME), summary)
        write_completion_marker(work_dir, 0, "native config validated (no execution)")
        return 0

    if safe_child(jobs_dir, job_name).is_dir():
        # 同一 Job 名已存在：只登记位置，绝不重复执行（与平台恢复语义一致）。
        located = record_job_location(
            work_dir, jobs_dir, job_name, reused=True, started=False,
            trial_names=(), planned_tasks=planned_task_names(plan),
        )
        write_completion_marker(
            work_dir, 0, f"reused existing job dir; trials={len(located['trials'])}",
        )
        return 0

    location = record_job_location(
        work_dir, jobs_dir, job_name, reused=False, started=True,
        trial_names=(), planned_tasks=planned_task_names(plan), started_at=now_iso(),
    )
    _STATE["location"] = location
    try:
        result = asyncio.run(run_job(job_config, work_dir, jobs_dir, job_name))
    except Exception as error:  # noqa: BLE001 - 失败仍要保留已产出的证据
        try:
            located = record_job_location(
                work_dir, jobs_dir, job_name, reused=False, started=False,
                trial_names=_known_names(work_dir, "trial_names"),
                planned_tasks=_known_names(work_dir, "planned_task_names"),
            )
        except Exception:  # noqa: BLE001 - 连目录都没有时如实记录
            located = {"trials": []}
        write_json(safe_join(work_dir, "harbor/run-error.json"), {
            "error_type": type(error).__name__,
            "message": str(error),
            "observed_trials": len(located["trials"]),
            "at": now_iso(),
        })
        write_completion_marker(work_dir, 1, f"job failed: {type(error).__name__}")
        print(f"harbor job failed: {error}", file=sys.stderr)
        return 1

    located = record_job_location(
        work_dir, jobs_dir, job_name, reused=False, started=False,
        trial_names=_known_names(work_dir, "trial_names"),
        planned_tasks=_known_names(work_dir, "planned_task_names"),
    )
    stats = getattr(result, "stats", None)
    write_json(safe_join(work_dir, "harbor/run-summary.json"), {
        "n_trials": len(located["trials"]),
        "n_completed_trials": getattr(stats, "n_completed_trials", None),
        "n_errored_trials": getattr(stats, "n_errored_trials", None),
        "planned_trial_count": plan.get("planned_trial_count"),
        "frozen_files_verified": frozen.get("files_verified"),
    })
    write_completion_marker(work_dir, 0, f"trials={len(located['trials'])}")
    return 0


def planned_task_names(plan: Mapping[str, Any]) -> list[str]:
    """计划里选中的任务 display 名（Harbor 的 trial 名由它派生，仅作线索）。

    真正的 ``trial_names`` 只有 ``Job.create`` 之后才知道（含随机后缀），
    因此启动前的定位文件只能给出这层线索；平台按 ``job_dir`` 采集，不依赖它。
    """
    names: list[str] = []
    for entry in plan.get("tasks") or []:
        if isinstance(entry, Mapping) and entry.get("normalized_relative_path"):
            names.append(str(entry["normalized_relative_path"]).rstrip("/").split("/")[-1])
    return sorted(set(names))


if __name__ == "__main__":  # pragma: no cover - Runner 入口
    raise SystemExit(main())
