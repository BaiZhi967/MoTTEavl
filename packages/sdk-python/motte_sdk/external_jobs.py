"""外部 Job 应用层监督（启动边界、观察、采集与结局映射）。

应用层持有调度主权：先持久化启动意图（含 launch_token）再调用
adapter.start；恢复路径只观察/采集，绝不重启。结果导入的幂等检查点与
同键冲突由 external job 存储（M2-T03）接管；本模块交付 T02 的单次启动
边界与结局语义。
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from motte_contracts.external_job import (
    ExternalJobHandle,
    ExternalJobSpec,
    ExternalJobStatus,
    new_launch_token,
)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class ExternalJobSupervisor:
    """单 Job 生命周期监督：launch / run / recover。

    ``intent_journal`` 是启动意图的持久化通道（T03 替换为 Job 存储事务）；
    写入顺序必须是 launch_intent → adapter.start → launch_started。
    """

    def __init__(
        self,
        adapter: Any,
        *,
        intent_journal: Callable[[dict[str, Any]], None] | None = None,
        poll_interval_seconds: float = 0.1,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.adapter = adapter
        self._journal = intent_journal
        self.poll_interval_seconds = poll_interval_seconds
        self._sleep = sleep
        self._monotonic = monotonic

    def _record(self, entry: dict[str, Any]) -> None:
        if self._journal is not None:
            self._journal(entry)

    def launch(self, spec: ExternalJobSpec) -> ExternalJobHandle:
        """prepare → 持久化启动意图（新 token）→ start → 持久化句柄。"""
        prepared = self.adapter.prepare(spec)
        token = new_launch_token()
        launching = prepared.model_copy(update={
            # 每次启动换发不可复用的新 token，先于 start 持久化。
            "launch_token": token,
            "status": ExternalJobStatus.launching,
            "launch_identity": {
                **(prepared.launch_identity or {}),
                "intent_recorded_at": _now_iso(),
                "launch_token": token,
            },
        })
        self._record({
            "event": "launch_intent",
            "spec": spec.model_dump(mode="json"),
            "handle": launching.model_dump(mode="json"),
        })
        started = self.adapter.start(spec, launching)
        self._record({
            "event": "launch_started",
            "handle": started.model_dump(mode="json"),
        })
        return started

    def run(self, spec: ExternalJobSpec) -> dict[str, Any]:
        """一次完整执行：launch 后观察至终态并采集。"""
        handle = self.launch(spec)
        return self.observe(spec, handle)

    def recover(self, spec: ExternalJobSpec, persisted_handle: ExternalJobHandle) -> dict[str, Any]:
        """崩溃恢复：只观察/采集，不 start；token 不可核验时 indeterminate。"""
        return self.observe(spec, persisted_handle)

    def interrupt(self, spec: ExternalJobSpec, handle: ExternalJobHandle) -> dict[str, Any]:
        """操作员取消：先中断本 Job 拥有的进程，再尽力采集部分工件。"""
        cancelled = self.adapter.interrupt(handle)
        results, error = self._collect_quiet(cancelled)
        return {
            "job_status": "cancelled",
            "results": results,
            "error": error,
            "handle": cancelled.model_dump(mode="json"),
        }

    # ------------------------------------------------------------------ 观察

    def observe(
        self, spec: ExternalJobSpec, handle: ExternalJobHandle,
    ) -> dict[str, Any]:
        poll_interval = float(
            spec.limits.get("poll_interval_seconds", self.poll_interval_seconds),
        )
        max_wall = spec.limits.get("max_wall_seconds")
        deadline = (
            self._monotonic() + float(max_wall)
            if isinstance(max_wall, (int, float)) else None
        )
        while True:
            handle = self.adapter.poll(handle)
            if handle.status != ExternalJobStatus.active:
                break
            if deadline is not None and self._monotonic() > deadline:
                interrupted = self.adapter.interrupt(handle)
                results, error = self._collect_quiet(interrupted)
                return {
                    "job_status": "failed",
                    "results": results,
                    "error": {
                        "code": "JOB_TIMEOUT",
                        "message": "external job exceeded max_wall_seconds",
                        "details": {"max_wall_seconds": max_wall},
                    },
                    "handle": interrupted.model_dump(mode="json"),
                }
            self._sleep(max(poll_interval, 0.01))

        if handle.status == ExternalJobStatus.indeterminate:
            return {
                "job_status": "indeterminate",
                "results": [],
                "error": {
                    "code": "JOB_OUTCOME_INDETERMINATE",
                    "message": (
                        "job process cannot be verified by launch token and no "
                        "controlled results exist; never restarted automatically"
                    ),
                },
                "handle": handle.model_dump(mode="json"),
            }

        results, collect_error = self._collect_quiet(handle)
        exit_code = handle.owned_resources.get("exit_code")
        error = collect_error
        if handle.status == ExternalJobStatus.failed and error is None:
            error = {
                "code": "JOB_NONZERO_EXIT",
                "message": "external job process exited nonzero; partial results kept",
                "details": {"exit_code": exit_code},
            }
        return {
            "job_status": handle.status.value,
            "results": results,
            "error": error,
            "handle": handle.model_dump(mode="json"),
        }

    def _collect_quiet(
        self, handle: ExternalJobHandle,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """采集并转成 outcome 载荷；采集失败记录错误而不是抛出。"""
        try:
            results, _cursor = self.adapter.collect(
                handle, dict(handle.collection_cursor or {}),
            )
        except Exception as error:  # noqa: BLE001 - 采集失败进入证据
            return ([], {
                "code": getattr(error, "code", "JOB_COLLECT_FAILED"),
                "message": str(error),
            })
        payload = [item.model_dump(mode="json") for item in results]
        return (payload, None)
