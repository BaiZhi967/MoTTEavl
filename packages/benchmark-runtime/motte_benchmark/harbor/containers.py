"""Harbor 容器所有权：标签注入、定向停止与残留核查（M3 review R04/R2-08）。

复现反例（review R04 原文）：``motte.job=...`` 只写进 handle 的
``owned_resources``，没有注入真实容器；interrupt/cleanup 仅委托
``ProcessJobAdapter``，既没检查也没停止本 Job 的 Docker 容器。结论：结束
Runner 进程组**不等于**停止 Docker daemon 管理的容器，而且当前 cleanup 能在
没有核验任何容器的情况下报告 ``clean``。

R2-08 补的是**完整所有权核验**：假 Docker client 返回一个 ``motte.job`` 匹配、
但 ``motte.run``/``motte.owner`` 都不同的容器时，旧实现照样 stop/remove 并
报告 ``clean``。现在操作前必须核对全部持久化证据：

- 三条标签都一致（或该条证据缺失）→ ``match``，可以动作；
- ``motte.job`` 指向**别的** Job，或 ``motte.run``/``motte.owner`` 与记录明确
  冲突 → ``conflict``：**拒绝动作**，状态 ``unknown`` 并附冲突明细，
  project fallback 绝不覆盖明确的 owner 不一致；
- 只有 compose project 命中、没有任何冲突证据 → ``match``（Harbor 用
  ``--project-name`` 管理容器，这是 R04 允许的第二条证据）。

状态语义严格按可观察证据：``clean`` 只在 daemon 可达且确认无本 Job 容器残留
（也没有所有权冲突）时给出；``residual`` 列出残留；``unknown`` 表示无法核验
（daemon 不可达、权限不足、SDK 缺失、所有权冲突），并附原因——**绝不谎报**。
"""
from __future__ import annotations

import hashlib
from typing import Any, Callable, Iterable, Mapping, Sequence

from motte_benchmark.protocol import BenchmarkRuntimeError

#: 平台所有权标签（与 Runner 写入的 overlay 一致）。
OWNER_LABEL_JOB = "motte.job"
OWNER_LABEL_RUN = "motte.run"
OWNER_LABEL_OWNER = "motte.owner"
#: docker compose 自己的 project 标签（用于按 project 定位本 Job 的容器）。
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
#: 停止容器的默认宽限秒数（与进程中断宽限一致）。
DEFAULT_STOP_GRACE_SECONDS = 5.0
#: 所有权核验结论（``match`` 可动作 / ``conflict`` 拒绝动作 / ``unrelated`` 无关）。
OWNERSHIP_MATCH = "match"
OWNERSHIP_CONFLICT = "conflict"
OWNERSHIP_UNRELATED = "unrelated"

ClientFactory = Callable[[], Any]


def owner_label_value(owner_token: str) -> str:
    """launch token → ``motte.owner`` 标签值（摘要，不落原始 token）。"""
    return "sha256:" + hashlib.sha256(str(owner_token).encode("utf-8")).hexdigest()[:32]


def sanitize_compose_project_name(name: str) -> str:
    """Harbor 的 compose project 名规则（``docker.py`` 同款，供平台侧推导）。"""
    lowered = str(name).lower()
    if not lowered or not (lowered[0].isascii() and lowered[0].isalnum()):
        lowered = "0" + lowered
    return "".join(
        character if (character.isascii() and (character.isalnum() or character in "_-"))
        else "-"
        for character in lowered
    )


def _default_client_factory() -> Any:
    """懒加载 Docker SDK：API 进程不因缺少 docker 包而 import 失败。"""
    try:
        import docker  # noqa: PLC0415 - 只在真正需要时导入
    except ImportError as error:  # pragma: no cover - 依赖缺失路径
        raise BenchmarkRuntimeError(
            "HARBOR_DOCKER_SDK_MISSING", f"docker SDK is unavailable: {error}",
        ) from error
    try:
        return docker.from_env()
    except Exception as error:  # noqa: BLE001 - daemon 不可达/权限不足
        raise BenchmarkRuntimeError(
            "HARBOR_DOCKER_UNAVAILABLE", f"cannot reach the Docker daemon: {error}",
        ) from error


def _container_record(container: Any) -> dict[str, Any]:
    """容器的可审计快照（id/name/project/labels/status）。"""
    labels = dict(getattr(container, "labels", None) or {})
    status = getattr(container, "status", None)
    return {
        "kind": "container",
        "id": getattr(container, "id", None),
        "short_id": str(getattr(container, "short_id", "") or "") or None,
        "name": getattr(container, "name", None),
        "project": labels.get(COMPOSE_PROJECT_LABEL),
        "labels": labels,
        "status": status,
        "owner": labels.get(OWNER_LABEL_OWNER),
    }


class ContainerOwnership:
    """本 Job 的容器所有权视图（只认标签/project，绝不全局 prune）。"""

    def __init__(
        self,
        *,
        job_id: str,
        run_id: str | None = None,
        owner_token: str | None = None,
        projects: Iterable[str] = (),
        client_factory: ClientFactory | None = None,
        stop_grace_seconds: float = DEFAULT_STOP_GRACE_SECONDS,
    ) -> None:
        if not job_id:
            raise BenchmarkRuntimeError(
                "HARBOR_OWNERSHIP_UNKNOWN", "container ownership requires a job_id",
            )
        self.job_id = str(job_id)
        self.run_id = str(run_id) if run_id else None
        self.owner_label = owner_label_value(owner_token) if owner_token else None
        self.projects = sorted({str(project) for project in projects if project})
        self._client_factory = client_factory or _default_client_factory
        self.stop_grace_seconds = float(stop_grace_seconds)
        self._client: Any = None

    # -------------------------------------------------------------- 定位

    def _connect(self) -> Any:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def classify(self, record: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        """容器所有权核验：``(结论, 冲突明细)``（review R2-08）。

        证据是容器上的三条平台标签与 compose project 名。任何一条明确不一致
        都是冲突（拒绝动作）；缺失不算冲突（旧 Runner 只写部分标签时仍然可
        按已有证据定位），但绝不因此把冲突当作匹配。
        """
        labels = record.get("labels") or {}
        job = str(labels.get(OWNER_LABEL_JOB) or "")
        run = str(labels.get(OWNER_LABEL_RUN) or "")
        owner = str(labels.get(OWNER_LABEL_OWNER) or "")
        project = str(labels.get(COMPOSE_PROJECT_LABEL) or "")
        conflicts: list[dict[str, Any]] = []
        if job and job != self.job_id:
            # 明确写着别的 Job：project 名重合也不认（绝不覆盖 owner 不一致）。
            conflicts.append({
                "code": "HARBOR_CONTAINER_FOREIGN_JOB_LABEL",
                "detail": f"label {OWNER_LABEL_JOB}={job} 指向别的 Job（本 Job {self.job_id}）",
            })
        if self.run_id and run and run != self.run_id:
            conflicts.append({
                "code": "HARBOR_CONTAINER_RUN_CONFLICT",
                "detail": f"label {OWNER_LABEL_RUN}={run} 与记录的 {self.run_id} 不一致",
            })
        if self.owner_label and owner and owner != self.owner_label:
            conflicts.append({
                "code": "HARBOR_CONTAINER_OWNER_CONFLICT",
                "detail": (
                    f"label {OWNER_LABEL_OWNER}={owner} 与本次启动的 owner 摘要不一致"
                ),
            })
        if conflicts:
            return (OWNERSHIP_CONFLICT, conflicts)
        if job == self.job_id:
            return (OWNERSHIP_MATCH, [])
        if project and project in self.projects:
            return (OWNERSHIP_MATCH, [])
        return (OWNERSHIP_UNRELATED, [])

    def _matches(self, record: Mapping[str, Any]) -> bool:
        """本 Job 的容器（证据一致）。"""
        verdict, _conflicts = self.classify(record)
        return verdict == OWNERSHIP_MATCH

    def _conflict_record(
        self, record: Mapping[str, Any], conflicts: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        return {
            "code": "HARBOR_CONTAINER_OWNERSHIP_CONFLICT",
            "message": (
                "container ownership evidence contradicts this job's records: "
                + "; ".join(str(item.get("detail")) for item in conflicts)
            ),
            "conflicts": [dict(item) for item in conflicts],
            "container": dict(record),
        }

    def list_owned(self) -> dict[str, Any]:
        """列出本 Job 的容器（含已停止的）与所有权冲突项；daemon 不可达即 unknown。"""
        try:
            client = self._connect()
        except BenchmarkRuntimeError as error:
            return {
                "state": "unknown", "reason": str(error), "code": error.code,
                "containers": [], "conflicts": [],
            }
        records: dict[str, dict[str, Any]] = {}
        conflicts: dict[str, dict[str, Any]] = {}
        filters = [{"label": f"{OWNER_LABEL_JOB}={self.job_id}"}]
        filters.extend(
            {"label": f"{COMPOSE_PROJECT_LABEL}={project}"} for project in self.projects
        )
        try:
            for query in filters:
                for container in client.containers.list(all=True, filters=query):
                    record = _container_record(container)
                    verdict, conflicts_found = self.classify(record)
                    if verdict == OWNERSHIP_MATCH:
                        records[str(record["id"])] = record
                    elif verdict == OWNERSHIP_CONFLICT:
                        conflicts[str(record["id"])] = {
                            **record, "conflicts": [dict(item) for item in conflicts_found],
                        }
        except Exception as error:  # noqa: BLE001 - 查询失败必须如实报告
            return {
                "state": "unknown",
                "reason": f"container query failed: {type(error).__name__}: {error}",
                "code": "HARBOR_DOCKER_UNAVAILABLE",
                "containers": sorted(records.values(), key=lambda item: str(item["id"])),
                "conflicts": sorted(conflicts.values(), key=lambda item: str(item["id"])),
            }
        listed_conflicts = sorted(conflicts.values(), key=lambda item: str(item["id"]))
        # 有所有权冲突就无法给出 clean/residual 结论：冲突项不能被动作，也不能
        # 当成"没有残留"。状态记 unknown 并附冲突明细。
        if listed_conflicts:
            state = "unknown"
            reason = (
                "container ownership conflict: "
                + "; ".join(
                    str(item.get("conflicts")) for item in listed_conflicts
                )
            )
        else:
            state = "residual" if records else "clean"
            reason = None
        return {
            "state": state,
            "reason": reason,
            "containers": sorted(records.values(), key=lambda item: str(item["id"])),
            "conflicts": listed_conflicts,
            "queried_projects": list(self.projects),
        }

    def _containers_by_id(self, ids: Sequence[str]) -> list[Any]:
        client = self._connect()
        out: list[Any] = []
        for identifier in ids:
            try:
                out.append(client.containers.get(identifier))
            except Exception as error:  # noqa: BLE001 - 已消失/不可读都记录
                raise BenchmarkRuntimeError(
                    "HARBOR_CONTAINER_UNAVAILABLE",
                    f"cannot load owned container {identifier}: {error}",
                ) from error
        return out

    # -------------------------------------------------------------- 停止

    def stop_owned(self, *, remove: bool = False) -> dict[str, Any]:
        """停止（可选删除）本 Job 的容器；证据冲突项拒绝动作，不做全局清理。"""
        listed = self.list_owned()
        if listed["state"] == "unknown" and listed.get("reason") and not listed.get("containers"):
            conflict_errors = [
                self._conflict_record(
                    {key: value for key, value in item.items() if key != "conflicts"},
                    item.get("conflicts") or [],
                )
                for item in listed.get("conflicts") or []
            ]
            return {
                "state": "unknown",
                "reason": listed.get("reason"),
                "stopped": [],
                "removed": [],
                "errors": conflict_errors or [{
                    "code": listed.get("code") or "HARBOR_OWNERSHIP_UNKNOWN",
                    "message": listed.get("reason"),
                }],
                "containers": listed.get("containers") or [],
                "conflicts": listed.get("conflicts") or [],
            }
        stopped: list[dict[str, Any]] = []
        removed: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        ids = [str(item["id"]) for item in listed["containers"]]
        try:
            containers = self._containers_by_id(ids) if ids else []
        except BenchmarkRuntimeError as error:
            return {
                "state": "unknown",
                "reason": str(error),
                "stopped": [], "removed": [],
                "errors": [{"code": error.code, "message": str(error)}],
                "containers": listed["containers"],
                "conflicts": listed.get("conflicts") or [],
            }
        for container in containers:
            record = _container_record(container)
            verdict, conflicts = self.classify(record)
            if verdict == OWNERSHIP_CONFLICT:
                # 完整所有权证据明确冲突：拒绝清理，如实登记明细（R2-08）。
                errors.append(self._conflict_record(record, conflicts))
                continue
            if verdict != OWNERSHIP_MATCH:
                # 定位与核验不一致：绝不动作，如实记录。
                errors.append({
                    "code": "HARBOR_CONTAINER_NOT_OWNED",
                    "message": f"container {record['id']} does not carry this job's ownership",
                    "container": record,
                })
                continue
            try:
                container.stop(timeout=int(max(self.stop_grace_seconds, 1.0)))
                record["status"] = getattr(container, "status", None) or "exited"
                stopped.append(record)
                if remove:
                    container.remove(force=True)
                    removed.append(record)
            except Exception as error:  # noqa: BLE001 - 单个容器失败不影响其余
                errors.append({
                    "code": "HARBOR_CONTAINER_STOP_FAILED",
                    "message": f"{type(error).__name__}: {error}",
                    "container": record,
                })
        after = self.list_owned()
        for item in after.get("conflicts") or []:
            # 冲突项必须在结果里具名出现（拒绝动作的原因与明细），不能只留在列表里。
            errors.append(self._conflict_record(
                {key: value for key, value in item.items() if key != "conflicts"},
                item.get("conflicts") or [],
            ))
        if after["state"] == "unknown":
            state = "unknown"
        elif after["containers"] or errors:
            state = "residual"
        else:
            state = "clean"
        return {
            "state": state,
            "stopped": stopped,
            "removed": removed,
            "errors": errors,
            "containers": after["containers"],
            "conflicts": after.get("conflicts") or [],
            "reason": after.get("reason"),
            "owner_label": self.owner_label,
            "queried_projects": list(self.projects),
        }

    def state(self) -> dict[str, Any]:
        """残留核查：clean（有证据）/ residual（列出残留）/ unknown（附原因/冲突）。"""
        listed = self.list_owned()
        if listed["state"] == "unknown":
            return {
                "state": "unknown",
                "reason": listed.get("reason"),
                "code": listed.get("code"),
                "containers": listed.get("containers") or [],
                "conflicts": listed.get("conflicts") or [],
            }
        if listed["containers"]:
            return {
                "state": "residual", "containers": listed["containers"],
                "conflicts": [], "reason": None,
            }
        return {"state": "clean", "containers": [], "conflicts": [], "reason": None}
