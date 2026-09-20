"""Harbor 容器所有权：标签注入、定向停止与残留核查（M3 review R04）。

复现反例（review 原文）：``motte.job=...`` 只写进 handle 的
``owned_resources``，没有注入真实容器；interrupt/cleanup 仅委托
``ProcessJobAdapter``，既没检查也没停止本 Job 的 Docker 容器。结论：结束
Runner 进程组**不等于**停止 Docker daemon 管理的容器，而且当前 cleanup 能在
没有核验任何容器的情况下报告 ``clean``。

平台侧的所有权证据有两条（二者都只覆盖本 Job，绝不做全局 prune）：

1. 容器标签 ``motte.job=<job_id>``（由 Runner 写的
   ``harbor/ownership-overlay.yaml`` 通过 ``extra_docker_compose`` 注入到
   Harbor 的 ``main`` 服务；``motte.owner`` 是 launch token 的摘要，可重算核验）；
2. 容器标签 ``com.docker.compose.project`` 属于本 Job 记录的 compose project
   集合（Harbor 用 ``--project-name sanitize(f"{trial_name}__env")``）。

状态语义严格按可观察证据：``clean`` 只在 daemon 可达且确认无本 Job 容器残留
时给出；``residual`` 列出残留；``unknown`` 表示无法核验（daemon 不可达、
权限不足、SDK 缺失），并附原因——**daemon 不可达时绝不报 clean**。
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

    def _matches(self, record: Mapping[str, Any]) -> bool:
        """本 Job 的容器：label motte.job 命中，或 compose project 属于本 Job。"""
        labels = record.get("labels") or {}
        if str(labels.get(OWNER_LABEL_JOB) or "") == self.job_id:
            return True
        project = str(labels.get(COMPOSE_PROJECT_LABEL) or "")
        return bool(project) and project in self.projects

    def list_owned(self) -> dict[str, Any]:
        """列出本 Job 的容器（含已停止的）；daemon 不可达时如实返回 unknown。"""
        try:
            client = self._connect()
        except BenchmarkRuntimeError as error:
            return {"state": "unknown", "reason": str(error), "code": error.code, "containers": []}
        records: dict[str, dict[str, Any]] = {}
        filters = [{"label": f"{OWNER_LABEL_JOB}={self.job_id}"}]
        filters.extend(
            {"label": f"{COMPOSE_PROJECT_LABEL}={project}"} for project in self.projects
        )
        try:
            for query in filters:
                for container in client.containers.list(all=True, filters=query):
                    record = _container_record(container)
                    if self._matches(record):
                        records[str(record["id"])] = record
        except Exception as error:  # noqa: BLE001 - 查询失败必须如实报告
            return {
                "state": "unknown",
                "reason": f"container query failed: {type(error).__name__}: {error}",
                "code": "HARBOR_DOCKER_UNAVAILABLE",
                "containers": sorted(records.values(), key=lambda item: str(item["id"])),
            }
        return {
            "state": "residual" if records else "clean",
            "containers": sorted(records.values(), key=lambda item: str(item["id"])),
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
        """停止（可选删除）本 Job 的容器；返回逐容器结果，不做全局清理。"""
        listed = self.list_owned()
        if listed["state"] == "unknown":
            return {
                "state": "unknown",
                "reason": listed.get("reason"),
                "stopped": [],
                "removed": [],
                "errors": [{"code": listed.get("code"), "message": listed.get("reason")}],
                "containers": [],
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
            }
        for container in containers:
            record = _container_record(container)
            if not self._matches(record):
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
            "reason": after.get("reason"),
            "owner_label": self.owner_label,
            "queried_projects": list(self.projects),
        }

    def state(self) -> dict[str, Any]:
        """残留核查：clean（有证据）/ residual（列出残留）/ unknown（附原因）。"""
        listed = self.list_owned()
        if listed["state"] == "unknown":
            return {
                "state": "unknown",
                "reason": listed.get("reason"),
                "code": listed.get("code"),
                "containers": listed.get("containers") or [],
            }
        if listed["containers"]:
            return {"state": "residual", "containers": listed["containers"], "reason": None}
        return {"state": "clean", "containers": [], "reason": None}
