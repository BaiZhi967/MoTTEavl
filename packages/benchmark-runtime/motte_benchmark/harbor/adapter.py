"""Harbor Job 适配器（M3-T06/T07，复用 M2 的 ExternalJobAdapter 协议）。

职责划分（需求第 5 节"资源所有权"）：

- ``ProcessJobAdapter`` 持有 Runner 进程生命周期（PID 归属、token 核验、
  进程组中断、残留清单）——这部分是 M2 已测代码，不重写；
- 本适配器持有 Harbor 语义：把冻结配置写进工作目录（``prepare``，零执行）、
  从 Harbor 自己的 Job 目录受控读取产物（``read_output_files``）、在边界把
  文本 UTF-8 encode 成 bytes 后交给纯 Parser（``collect_from_files``）。

平台主权：adapter 不修改 Run 表、不决定评分。任务容器由 Harbor 管理，平台在
``prepare`` 物化不可变任务副本、在启动时记录容器/进程/工作目录的 owner token，
中断与清理只操作**本 Job 拥有**的容器（标签 ``motte.job`` 或本 Job 记录的
compose project），绝不做全局 prune；无法核验时状态是 ``unknown`` 而不是
``clean``（review R04）。
"""
from __future__ import annotations

import hashlib
import json
import math
import mimetypes
from pathlib import Path
from typing import Any

from motte_benchmark.harbor.containers import (
    ContainerOwnership,
    owner_label_value,
    sanitize_compose_project_name,
)
from motte_benchmark.harbor.frozen import clean_frozen_tasks, materialize_frozen_tasks
from motte_benchmark.harbor.parser import PARSER_VERSION, parse_harbor_files
from motte_benchmark.harbor.tasks import HarborTaskError
from motte_benchmark.process import ProcessJobAdapter
from motte_benchmark.protocol import BenchmarkRuntimeError
from motte_benchmark.trusted import TrustedDir, TrustedPathError
from motte_contracts.external_job import ExternalJobHandle, ExternalJobStatus, ExternalJobSpec

ADAPTER_ID = "terminal-bench-harbor"
ADAPTER_VERSION = "1"
PROFILE_NAME = "job-profile.json"
CONFIG_NAME = "harbor/config.json"
PLAN_NAME = "harbor/plan.json"
LOCATION_NAME = "harbor/job-location.json"
#: Runner 侧核验记录（R03）与冻结任务副本目录名。
FROZEN_RECORD_NAME = "harbor/frozen-tasks.json"
FROZEN_DIR_NAME = "frozen-tasks"
#: 单次采集的原始证据预算（与 M2 一致：超预算只留 hash，解析前拒绝最终化）。
MAX_RESULT_BYTES = 10 * 1024 * 1024
#: 外层 Harbor 配置文件的读取上限（review R12：与内层 Trial 同等限额）。
MAX_CONFIG_BYTES = 4 * 1024 * 1024


def _encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")


class HarborJobAdapter:
    """Harbor（Terminal-Bench）外部 Job 适配器。"""

    #: 与锁定 Harbor 版本对应的 Runner 版本串（进入 profile/证据）。
    RUNNER_VERSION = "harbor-0.23.0"

    def __init__(
        self, *, argv: list[str] | None = None, module: str | None = None,
        extra_env: dict[str, str] | None = None, default_limits: dict[str, Any] | None = None,
        data_root: str | None = None, docker_client_factory: Any = None,
        stop_grace_seconds: float = 5.0,
    ) -> None:
        self.adapter_id = ADAPTER_ID
        self.adapter_version = ADAPTER_VERSION
        if argv is None and module is None:
            argv = ["/opt/motte-runner/bin/harbor-entry"]
        #: 任务数据的受控根目录：Runner 从它把冻结的相对任务路径解析成绝对路径，
        #: 因此必须随启动环境一起传下去（否则 Runner 找不到任务目录）。
        self.data_root = data_root
        env = {str(key): str(value) for key, value in (extra_env or {}).items()}
        if data_root:
            env.setdefault("MOTTE_TASK_ROOT", str(data_root))
        self._process = ProcessJobAdapter(
            argv=argv, module=module, extra_env=env or None, default_limits=default_limits,
        )
        #: Docker 所有权核验的客户端工厂（测试可注入；生产懒加载 Docker SDK）。
        self._docker_client_factory = docker_client_factory
        self.stop_grace_seconds = float(stop_grace_seconds)
        #: 二进制工件冻结通道：SDK 在装配时挂上（``binary_sink``），缺失即如实
        #: 记 ``artifact_id: null``（review R11：不能只有 hash 冒充完整冻结）。
        self.binary_sink: Any = None
        self.last_parsed: dict[str, Any] | None = None
        #: 最近一次采集的二进制清单（``collect_from_files`` 默认消费它）。
        self.last_binaries: list[dict[str, Any]] = []

    # ---------------------------------------------------------- 身份与限额

    def _require_finite_deadline(self, spec: ExternalJobSpec) -> float:
        """Job 总期限必须有限且为正（review R07）。

        ``spec.limits.max_wall_seconds`` 由 Supervisor 真实读取（超时 → 中断 +
        保留部分证据 + 清理）；缺失/非有限/非正都说明这个 Job 没有可执行的
        期限，必须在启动前拒绝，而不是无限占用资源与持续调用模型。
        """
        merged = {**self.default_limits, **(spec.limits or {})}
        value = merged.get("max_wall_seconds")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BenchmarkRuntimeError(
                "HARBOR_JOB_DEADLINE_MISSING",
                f"max_wall_seconds must be a finite positive number, got {value!r}",
            )
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise BenchmarkRuntimeError(
                "HARBOR_JOB_DEADLINE_MISSING",
                f"max_wall_seconds must be a finite positive number, got {value!r}",
            )
        return number

    @property
    def parser_version(self) -> str:
        return PARSER_VERSION

    @property
    def default_limits(self) -> dict[str, Any]:
        return self._process.default_limits

    @property
    def start_calls(self) -> list[dict[str, Any]]:
        return self._process.start_calls

    def spawned_processes(self) -> list[dict[str, Any]]:
        return self._process.spawned_processes()

    # ---------------------------------------------------------- 生命周期

    def prepare(self, spec: ExternalJobSpec) -> ExternalJobHandle:
        """写冻结配置与计划、物化不可变任务副本；零执行、零模型调用。"""
        deadline = self._require_finite_deadline(spec)
        handle = self._process.prepare(spec)
        work_dir = Path(handle.work_dir)
        (work_dir / "harbor").mkdir(parents=True, exist_ok=True)
        config = spec.runner_config.get("harbor")
        if not isinstance(config, dict):
            raise BenchmarkRuntimeError(
                "HARBOR_CONFIG_MISSING",
                "runner_config.harbor is required for the terminal-bench adapter",
            )
        plan = spec.runner_config.get("plan")
        if not isinstance(plan, dict):
            raise BenchmarkRuntimeError(
                "HARBOR_PLAN_MISSING", "runner_config.plan is required (frozen TrialPlan set)",
            )
        tasks = plan.get("tasks")
        if not isinstance(tasks, list):
            raise BenchmarkRuntimeError(
                "HARBOR_PLAN_MISSING", "runner_config.plan.tasks is required",
            )
        (work_dir / PLAN_NAME).write_bytes(_encode(plan))
        (work_dir / CONFIG_NAME).write_bytes(_encode(config))
        (work_dir / PROFILE_NAME).write_bytes(_encode(spec.profile))
        # 不可变执行副本（review R03）：源目录在排队期间可以被改动，因此把
        # 冻结清单里的每个字节复验后复制到受控工作目录，执行只读这份副本。
        if not self.data_root:
            raise BenchmarkRuntimeError(
                "HARBOR_TASK_SOURCE_MISSING",
                "the terminal-bench adapter requires data_root (controlled task root) "
                "to materialize the frozen task copy",
            )
        clean_frozen_tasks(work_dir)
        frozen = materialize_frozen_tasks(
            plan=plan, data_root=self.data_root, work_dir=work_dir,
        )
        # Runner 唯一的任务根：源目录之后的改动不再影响已冻结的 Run。
        self._process.set_extra_env("MOTTE_TASK_ROOT", str(frozen["root"]))
        return handle.model_copy(update={
            "launch_identity": {
                **(handle.launch_identity or {}),
                "adapter": ADAPTER_ID,
                "adapter_version": ADAPTER_VERSION,
                "harbor_version": config.get("harbor_version"),
                "planned_trials": len(plan.get("trials") or []),
                "config_hash": config.get("config_hash"),
                "data_root": self.data_root,
                "frozen_task_root": str(frozen["root"]),
                "frozen_tasks_hash": frozen["manifest"]["task_files_hash"],
                "frozen_file_count": frozen["manifest"]["file_count"],
                "max_wall_seconds": deadline,
            },
            "owned_resources": {
                **(handle.owned_resources or {}),
                "frozen_task_root": str(frozen["root"]),
                "frozen_tasks_hash": frozen["manifest"]["task_files_hash"],
                "max_wall_seconds": deadline,
            },
        })

    def start(self, spec: ExternalJobSpec, handle: ExternalJobHandle) -> ExternalJobHandle:
        """启动 Runner；launch_token 已在持久层落库后由 supervisor 注入。"""
        started = self._process.start(spec, handle)
        owned = dict(started.owned_resources or {})
        # 资源账本：本 Job 拥有的容器/工作目录前缀（清理前据此核验所有权）。
        owned.update({
            "owner_token": started.launch_token,
            "job_id": started.job_id,
            "run_id": started.run_id,
            "container_label": f"motte.job={started.job_id}",
            # 平台自己注入的标签值：容器元数据里只有摘要，原始 token 留在受控记录。
            "container_owner_label": owner_label_value(started.launch_token),
        })
        resources = list(owned.get("resources") or [])
        resources.append({
            "kind": "job_dir",
            "path": str(started.work_dir),
            "owner_token": started.launch_token,
        })
        owned["resources"] = resources
        return started.model_copy(update={"owned_resources": owned})

    def poll(self, handle: ExternalJobHandle) -> ExternalJobHandle:
        return self._process.poll(handle)

    # ------------------------------------------------------------ 容器所有权

    def _location_payload(self, handle: ExternalJobHandle) -> dict[str, Any]:
        """Runner 写的定位文件（受控读取；缺失/畸形即空）。"""
        try:
            with TrustedDir(Path(handle.work_dir), error_factory=_read_error) as trusted:
                raw = trusted.read_bytes(LOCATION_NAME, max_bytes=MAX_CONFIG_BYTES)
        except (TrustedPathError, BenchmarkRuntimeError):
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def ownership(self, handle: ExternalJobHandle) -> ContainerOwnership:
        """本 Job 的容器所有权视图（job 标签 + Runner 记录的 compose project）。"""
        owned = handle.owned_resources or {}
        location = self._location_payload(handle)
        projects = [
            str(item) for item in (location.get("compose_projects") or []) if item
        ]
        for name in location.get("trial_names") or []:
            projects.append(sanitize_compose_project_name(f"{name}__env"))
        projects.extend(str(item) for item in (owned.get("compose_projects") or []))
        return ContainerOwnership(
            job_id=str(owned.get("job_id") or handle.job_id),
            run_id=str(owned.get("run_id") or handle.run_id or ""),
            owner_token=handle.launch_token,
            projects=projects,
            client_factory=self._docker_client_factory,
            stop_grace_seconds=self.stop_grace_seconds,
        )

    def interrupt(self, handle: ExternalJobHandle) -> ExternalJobHandle:
        """中断本 Job 拥有的进程树**与容器**；只凭 owner token/标签核验。

        结束 Runner 进程组不保证 Docker daemon 管理的容器停止，因此取消必须
        定向停止本 Job 的容器；核验不到身份时绝不动作（也不假称已停止）。
        """
        interrupted = self._process.interrupt(handle)
        try:
            containers = self.ownership(interrupted).stop_owned(remove=False)
        except BenchmarkRuntimeError as error:  # pragma: no cover - 工厂异常
            containers = {
                "state": "unknown", "reason": str(error), "stopped": [], "removed": [],
                "errors": [{"code": error.code, "message": str(error)}], "containers": [],
            }
        return interrupted.model_copy(update={
            "owned_resources": {
                **(interrupted.owned_resources or {}),
                "container_state": containers["state"],
                "containers": containers.get("containers") or [],
            },
            "launch_identity": {
                **(interrupted.launch_identity or {}),
                "container_stop": {
                    "state": containers["state"],
                    "stopped": [item.get("id") for item in containers.get("stopped") or []],
                    "errors": containers.get("errors") or [],
                },
            },
        })

    def cleanup(self, handle: ExternalJobHandle) -> dict[str, Any]:
        """清理本 Job 资源并列出残留（不做全局 prune，也不删未知目录）。"""
        outcome = self._process.cleanup(handle)
        leftovers = list(outcome.get("leftovers") or [])
        ledger = list((handle.owned_resources or {}).get("resources") or [])
        job_dir = next(
            (item["path"] for item in ledger if item.get("kind") == "job_dir"), None,
        )
        # 容器必须真正核验后停止/删除；daemon 不可达时状态是 unknown，不是 clean。
        containers = self.ownership(handle).stop_owned(remove=True)
        observed: dict[str, dict[str, Any]] = {}
        for record in containers.get("removed") or []:
            observed[str(record.get("id"))] = {
                **record, "kind": "container", "state": "removed",
            }
        for record in containers.get("stopped") or []:
            observed.setdefault(str(record.get("id")), {
                **record, "kind": "container", "state": "stopped",
            })
        for record in containers.get("containers") or []:
            observed.setdefault(str(record.get("id")), {
                **record, "kind": "container", "state": "residual",
            })
        owned_resources = {
            **(handle.owned_resources or {}),
            "resources": ledger + [observed[key] for key in sorted(observed)],
            "container_state": containers["state"],
            "container_stop": {
                "stopped": [item.get("id") for item in containers.get("stopped") or []],
                "removed": [item.get("id") for item in containers.get("removed") or []],
                "errors": containers.get("errors") or [],
            },
        }
        container_leftovers = [
            {"kind": "container", **record} for record in containers.get("containers") or []
        ]
        leftovers.extend(container_leftovers)
        for error in containers.get("errors") or []:
            leftovers.append({"kind": "container_error", **error})
        if containers["state"] == "unknown":
            state = "unknown"
        elif leftovers:
            state = "residual"
        else:
            state = "clean"
        outcome = dict(outcome)
        outcome.update({
            "owner_token": handle.launch_token,
            "job_id": handle.job_id,
            "known_resources": ledger,
            "job_dir": job_dir,
            "job_dir_present": bool(job_dir) and Path(job_dir).is_dir(),
            "leftovers": leftovers,
            "state": state,
            "owned_resources": owned_resources,
            "container_state": containers["state"],
            "containers": containers.get("containers") or [],
            "container_errors": containers.get("errors") or [],
            "container_reason": containers.get("reason"),
            "queried_projects": containers.get("queried_projects") or [],
            "owner_label": containers.get("owner_label"),
        })
        return outcome

    def collect(
        self, handle: ExternalJobHandle, cursor: dict[str, Any],
    ) -> tuple[list[Any], dict[str, Any]]:
        """兼容路径：先读受控字节，再走共享的纯 Parser。"""
        files, binaries = self.read_output_bytes(handle)
        return self.collect_from_files(handle, cursor, files, binaries)

    # ---------------------------------------------------------- 证据读取

    def _location_payload(self, handle: ExternalJobHandle) -> dict[str, Any]:
        """Runner 写的定位文件（受控读取；缺失/畸形即空）。

        review R12：外层 harness 文件与内层 Trial 用同一套受控读取——fd 锚定、
        拒 symlink、大小限额；定位文件被替换成链接时不得跟随到宿主。
        """
        try:
            with TrustedDir(Path(handle.work_dir), error_factory=_outer_read_error) as trusted:
                raw = trusted.read_bytes(LOCATION_NAME, max_bytes=MAX_CONFIG_BYTES)
        except (TrustedPathError, BenchmarkRuntimeError):
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _job_dir(self, handle: ExternalJobHandle) -> Path | None:
        """Harbor Job 目录（来自 Runner 写的定位文件；越界或缺失即拒绝）。"""
        candidate = self._location_payload(handle).get("job_dir")
        if not isinstance(candidate, str) or not candidate:
            return None
        job_dir = Path(candidate)
        work_root = Path(handle.work_dir).resolve()
        resolved = job_dir.resolve()
        # Job 目录必须就落在本 Job 的工作目录内（Runner 侧 jobs_dir 受控）。
        if resolved != work_root and work_root not in resolved.parents:
            return None
        # 定位文件存在但目录不在（Runner 刚写完就被中断/被删）：按"证据不可用"
        # 处理，让其余已产出的证据仍然可采集，而不是整次采集失败。
        if not resolved.is_dir() or resolved.is_symlink():
            return None
        return resolved

    def _read_outer_files(
        self, handle: ExternalJobHandle, index: dict[str, Any],
        binaries: list[dict[str, Any]],
    ) -> dict[str, str]:
        """受控读取外层 harness 文件（config/lock/result/plan/定位/审计记录）。

        review R12：这些文件过去用普通 ``is_file``/``read_text``，会跟随 symlink、
        没有大小限额；现在统一走 ``TrustedDir``，并把 sha256/size/encoding 登记
        进 ``evidence-index.json``。symlink/超限是篡改信号 → 显式失败，不静默跳过。
        """
        work_dir = Path(handle.work_dir)
        names = (
            CONFIG_NAME, "harbor/lock.json", "harbor/result.json", PLAN_NAME,
            LOCATION_NAME, FROZEN_RECORD_NAME, "harbor/launch-identity.json",
            "harbor/run-summary.json", "harbor/run-error.json", "harbor/signal.json",
            # 冻结副本清单：task_key ↔ 执行目录名（Harbor 的 trial 名与 task_id.path
            # 都以副本目录为准，Parser 据此归属；review R03/R13）。
            f"{FROZEN_DIR_NAME}/manifest.json",
        )
        files: dict[str, str] = {}
        with TrustedDir(work_dir, error_factory=_outer_read_error) as trusted:
            present, symlinks = trusted.list_files()
            for name in names:
                if name in symlinks:
                    raise BenchmarkRuntimeError(
                        "HARBOR_OUTPUT_UNREADABLE",
                        f"outer harbor file is a symlink and must not be followed: {name}",
                    )
                if name not in present:
                    continue
                data = trusted.read_bytes(name, max_bytes=MAX_CONFIG_BYTES)
                entry: dict[str, Any] = {
                    "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                    "size_bytes": len(data),
                    "scope": "outer",
                }
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    # 外层文件同样不允许"只有 hash"冒充完整冻结（review R11）。
                    entry["encoding"] = "binary"
                    entry.update(self._freeze_binary(handle, name, data))
                    index["files"][name] = entry
                    binaries.append({"path": name, **entry})
                    continue
                entry["encoding"] = "utf-8"
                index["files"][name] = entry
                files[name] = text
        return files

    def read_output_files(self, handle: ExternalJobHandle) -> dict[str, str]:
        """受控读取 Harbor 产物文本：``{相对路径: 文本}``，解析前先冻结。

        只读取 Parser 需要的文件（结果/配置/锁/奖励/终端/轨迹/工件清单），
        非 UTF-8 或不存在的文件不进入文本 bundle（其内容由 ``binary_sink``
        冻结为 Artifact，hash/引用登记在 ``evidence-index.json``），绝不有损解码。
        """
        files, _binaries = self.read_output_bytes(handle)
        return files

    def read_output_bytes(
        self, handle: ExternalJobHandle,
    ) -> tuple[dict[str, str], list[dict[str, Any]]]:
        """完整采集：``(文本文件, 二进制清单)``。

        review R11：非 UTF-8 工件过去只留 size/hash，字节留在可被清理的工作
        目录里，"冻结完成"因此是假的。现在每个二进制文件都通过
        ``binary_sink`` 写入独立 Artifact，并把 ``artifact_id`` 写进
        ``evidence-index.json``；sink 缺失或抛错时如实记 ``artifact_id: null``
        与 ``note``，不谎报完整。
        """
        index: dict[str, Any] = {"schema_version": 2, "files": {}}
        binaries: list[dict[str, Any]] = []
        self.last_binaries = binaries
        files = self._read_outer_files(handle, index, binaries)
        job_dir = self._job_dir(handle)
        if job_dir is None:
            # Job 目录缺失/越界/被中断在创建之前：仍然返回冻结的计划与 harness
            # 文件，让 Parser 把全部计划单元标成 not_attempted（有 disposition），
            # 而不是空结果。
            index["job_dir_unavailable"] = True
            index["job_dir_missing"] = True
            location = self._location_payload(handle)
            index["job_location"] = {
                "started": bool(location.get("started")),
                "trials": list(location.get("trials") or []),
                "trial_names": list(location.get("trial_names") or []),
                "compose_projects": list(location.get("compose_projects") or []),
                "reused_existing_job_dir": bool(location.get("reused_existing_job_dir")),
            }
            files["harbor/evidence-index.json"] = json.dumps(
                index, ensure_ascii=False, sort_keys=True,
            )
            return files, binaries
        with TrustedDir(job_dir, error_factory=_read_error) as trusted:
            rels, symlinks = trusted.list_files()
            if symlinks:
                # 输出边界内的 symlink 显式拒绝：不静默丢结果、不跟随到宿主。
                raise BenchmarkRuntimeError(
                    "HARBOR_OUTPUT_SYMLINK",
                    f"harbor job dir contains symlinks: {symlinks[:5]}",
                )
            for rel in rels:
                if rel.endswith("instruction.md"):
                    continue
                if not self._wanted(rel):
                    continue
                try:
                    data = trusted.read_bytes(rel, max_bytes=MAX_RESULT_BYTES)
                except TrustedPathError:
                    continue
                key = f"harbor/job/{rel}" if "/" not in rel else f"harbor/trials/{rel}"
                entry: dict[str, Any] = {
                    "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                    "size_bytes": len(data),
                }
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    # 二进制/非 UTF-8：不做有损解码，改为冻结成独立 Artifact。
                    entry["encoding"] = "binary"
                    entry.update(self._freeze_binary(handle, rel, data))
                    index["files"][key] = entry
                    binaries.append({"path": key, **entry})
                    continue
                entry["encoding"] = "utf-8"
                index["files"][key] = entry
                files[key] = text
        location = self._location_payload(handle)
        index["job_location"] = {
            "started": bool(location.get("started")),
            "trials": list(location.get("trials") or []),
            "trial_names": list(location.get("trial_names") or []),
            "compose_projects": list(location.get("compose_projects") or []),
            "reused_existing_job_dir": bool(location.get("reused_existing_job_dir")),
        }
        index["binaries"] = sorted(binaries, key=lambda item: str(item.get("path")))
        files["harbor/evidence-index.json"] = json.dumps(
            index, ensure_ascii=False, sort_keys=True,
        )
        return files, binaries

    def _freeze_binary(
        self, handle: ExternalJobHandle, rel: str, data: bytes,
    ) -> dict[str, Any]:
        """把非 UTF-8 工件的完整字节写进独立 Artifact（review R11）。"""
        digest = hashlib.sha256(data).hexdigest()
        suffix = Path(rel).suffix
        media_type = mimetypes.guess_type(f"x{suffix}")[0] or "application/octet-stream"
        entry: dict[str, Any] = {
            "artifact_id": None,
            "media_type": media_type,
            "note": None,
        }
        sink = getattr(self, "binary_sink", None)
        if not callable(sink):
            entry["note"] = (
                "binary sink is not wired; the bytes are only readable from the "
                "work dir and are NOT frozen as an artifact"
            )
            return entry
        path = f"external-jobs/{handle.run_id}/{handle.job_id}/evidence/bin-{digest[:16]}{suffix}"
        try:
            artifact = sink(path, data)
        except Exception as error:  # noqa: BLE001 - sink 失败必须如实记录
            entry["note"] = f"binary sink failed: {type(error).__name__}: {error}"
            return entry
        artifact_id = getattr(artifact, "id", None) or (
            artifact if isinstance(artifact, str) else None
        )
        entry["artifact_id"] = artifact_id
        if artifact_id is None:
            entry["note"] = "binary sink returned no artifact id"
        return entry

    @staticmethod
    def _wanted(rel: str) -> bool:
        """Parser 需要的产物（大文件仍只登记 hash，二进制只登记编码与 hash）。

        路径形状是 ``<trial 目录>/...`` 或 job 层的 ``result.json`` 等文件，
        因此判定要跳过第一段 Trial 目录名看真正的种类。
        """
        parts = rel.split("/")
        if len(parts) == 1:
            # job 根层的文件（Harbor 的 job result.json/config.json/lock.json）。
            return parts[0] in ("result.json", "config.json", "lock.json", "trial.log",
                                "exception.txt")
        kinds = parts[1:]
        if len(kinds) == 1:
            # Trial 根层的文件（result.json / config.json / lock.json / trial.log …）。
            return True
        if kinds[0] in ("agent", "verifier", "artifacts", "user-agent"):
            return True
        if kinds[0] == "steps":
            return bool(kinds) and kinds[-1] in (
                "result.json", "trial.log", "exception.txt", "reward.json", "reward.txt",
                "test-stdout.txt", "test-stderr.txt", "manifest.json",
            )
        return False

    def collect_from_files(
        self, handle: ExternalJobHandle, cursor: dict[str, Any], files: dict[str, str],
        binaries: list[dict[str, Any]] | None = None,
    ) -> tuple[list[Any], dict[str, Any]]:
        """把冻结文本 encode 成 bytes 后调用纯 Parser（恢复路径同样走这里）。

        ``binaries`` 是二进制清单（``read_output_bytes`` 的第二个返回值）；
        省略时用最近一次读取的清单，保证 cursor 与 evidence-index 覆盖同一批
        二进制条目（review R11）。
        """
        from motte_contracts.external_job import NormalizedCaseResult

        plan_payload = files.get(PLAN_NAME)
        if plan_payload is None:
            raise BenchmarkRuntimeError(
                "HARBOR_FROZEN_PLAN_MISSING",
                "frozen plan is unavailable; case mapping cannot be rebuilt",
            )
        try:
            plan = json.loads(plan_payload)
        except json.JSONDecodeError as error:
            raise BenchmarkRuntimeError(
                "HARBOR_FROZEN_PLAN_INVALID", f"frozen plan is not valid JSON: {error}",
            ) from error
        plans = plan.get("trials") if isinstance(plan, dict) else None
        if not isinstance(plans, list):
            raise BenchmarkRuntimeError(
                "HARBOR_FROZEN_PLAN_INVALID", "frozen plan must contain the trial list",
            )
        encoded = {rel: text.encode("utf-8") for rel, text in files.items()}
        parsed = parse_harbor_files(encoded, plans, parser_version=PARSER_VERSION)
        self.last_parsed = parsed
        observed_binaries = list(
            binaries if binaries is not None else self.last_binaries or [],
        )

        results: list[NormalizedCaseResult] = []
        for trial in parsed["results"]:
            observation = trial["verifier_observation"]
            case_key = f"{trial['task_key']}#{trial['repeat_index']}"
            results.append(NormalizedCaseResult(
                # 幂等导入键必须是**计划 Trial**身份：一个 Task 有多个 Trial，
                # 用 task_key 当键会让第二个 Trial 与第一个"同键异内容"冲突。
                # 逻辑 Case 身份由 payload 的 task_key 表达（一 Task 一条 CaseRun）。
                stable_case_key=case_key,
                source_case_id=str(trial.get("source_trial_id") or case_key),
                output=trial,
                error=(
                    observation.get("error") if observation.get("error") else None
                ),
                status=_case_status(trial),
                error_category=(
                    str(observation["status"]) if observation.get("error") else None
                ),
                usage=trial.get("usage") or {},
                evidence_coverage=trial.get("coverage") or {},
            ))
        cursor = dict(cursor or {})
        cursor.update({
            "parser_version": parsed["parser_version"],
            "harbor_version": parsed["harbor_version"],
            "planned_trial_count": parsed["planned_trial_count"],
            "trial_count": parsed["trial_count"],
            "unmapped": parsed["unmapped"][:20],
            "coverage": _aggregate_coverage(parsed["results"]),
            # 二进制工件清单（含 artifact 引用）：cursor 与 evidence-index 覆盖
            # 同一批条目，恢复路径也能核对内容是否真的冻结过（review R11）。
            "binaries": observed_binaries,
            "binary_count": len(observed_binaries),
            "binaries_frozen": all(
                item.get("artifact_id") for item in observed_binaries
            ),
        })
        return results, cursor


def _read_error(message: str) -> Exception:
    return BenchmarkRuntimeError("HARBOR_OUTPUT_UNREADABLE", message)


def _outer_read_error(message: str) -> Exception:
    """外层 harness 文件的受控读取错误（超限与不可读分开表达）。"""
    code = (
        "HARBOR_OUTPUT_TOO_LARGE" if message.startswith("file-size:")
        else "HARBOR_OUTPUT_UNREADABLE"
    )
    return BenchmarkRuntimeError(code, message)


def _case_status(trial: dict[str, Any]) -> Any:
    """``NormalizedCaseResult`` 的处置状态：与 Trial disposition 对齐。"""
    from motte_contracts.external_job import CaseResultStatus

    disposition = str(trial["disposition"])
    if disposition in ("succeeded", "failed"):
        return CaseResultStatus.succeeded
    if disposition == "not_attempted":
        return CaseResultStatus.not_attempted
    return CaseResultStatus.failed


def _aggregate_coverage(results: list[dict[str, Any]]) -> dict[str, Any]:
    """采集覆盖范围汇总（供报告显示，不参与评分分母）。"""
    counted: dict[str, dict[str, int]] = {}
    for trial in results:
        items = (trial.get("coverage") or {}).get("items") or {}
        for key, value in items.items():
            bucket = counted.setdefault(key, {})
            bucket[str(value)] = bucket.get(str(value), 0) + 1
    return counted
