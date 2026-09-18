"""Worker 调度循环：从持久化存储抢占 queued Run 并执行。

默认 loop 模式只依赖 SQLite（本地开发零外部服务）；celery 模式见 celery_app.py。
"""
from __future__ import annotations

import time
from typing import Any

from motte_provider.capabilities import UnsupportedParameterError
from motte_provider.config import build_provider
from motte_sdk.service import RunService

from .reporting import WorkerReporter


def provider_for_run(run: dict[str, Any]):
    """按 Run manifest 选择 provider；构造失败（strict 预检）会抛异常阻断付费调用。"""
    manifest = run.get("manifest") or {}
    provider_config = manifest.get("provider") or {}
    if not provider_config.get("kind"):
        raise ValueError("run manifest requires provider.kind")
    return build_provider(provider_config, manifest).invoke


class WorkerLoop:
    def __init__(self, service: RunService, reporter: WorkerReporter | None = None) -> None:
        self.service = service
        self.reporter = reporter or WorkerReporter()
        self.service.add_event_observer(self.reporter.observe_event)
        self.service.add_progress_observer(self.reporter.observe_progress)

    def recover_interrupted(self) -> list[str]:
        """启动时回收上次崩溃遗留的中间态 Run。"""
        requeued = self.service.store.runs.requeue_interrupted()
        for run_id in requeued:
            self.reporter.emit("run_requeued", run_id=run_id, reason="worker_restart")
        return requeued

    def claim_and_execute(self) -> dict | None:
        """抢占并执行一个 queued Run；无待处理任务时返回 None。

        provider 构造失败不产生任何网络调用，Run 落 unsupported 状态。
        """
        claimed = self.service.store.runs.claim_next_queued()
        if claimed is None:
            return None
        run_id = claimed["id"]
        self.reporter.emit(
            "run_claimed",
            run_id=run_id,
            status=claimed.get("status"),
            selected=len(claimed.get("case_ids") or []),
        )
        try:
            provider = provider_for_run(claimed)
        except UnsupportedParameterError as error:
            result = self.service.mark_unsupported(run_id, "UNSUPPORTED_PARAMETER", message=str(error))
        except ValueError as error:
            result = self.service.mark_unsupported(run_id, "PROVIDER_CONFIG_INVALID", message=str(error))
        else:
            result = self.service.execute(run_id, provider=provider)
        self._report_finished(result)
        return result

    def _report_finished(self, result: dict[str, Any]) -> None:
        cases = result.get("cases") or []
        scores = result.get("scores") or []
        failed = sum(
            isinstance(row.get("result"), dict) and isinstance(row["result"].get("error"), dict)
            for row in cases
        )
        not_attempted = sum(
            row.get("outcome") == "not_attempted" or row.get("result") is None for row in cases
        )
        self.reporter.emit(
            "run_finished",
            run_id=result.get("id"),
            status=result.get("status"),
            selected=len(result.get("case_ids") or []),
            persisted=len(cases),
            scored=len(scores),
            failed=failed,
            not_attempted=not_attempted,
        )

    def run_forever(self, poll_interval: float = 1.0, *, recover: bool = True) -> None:
        if recover:
            self.recover_interrupted()
        while True:
            if self.claim_and_execute() is None:
                time.sleep(poll_interval)
