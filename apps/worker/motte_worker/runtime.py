"""Worker 调度循环：从持久化存储抢占 queued Run 并执行。

默认 loop 模式只依赖 SQLite（本地开发零外部服务）；celery 模式见 celery_app.py。
"""
from __future__ import annotations

import time
from typing import Any

from motte_sdk.replay_run import ReplayProvider
from motte_sdk.service import RunService


def provider_for_run(run: dict[str, Any]):
    """按 Run manifest 选择 provider；replay 是当前唯一内建类型。"""
    provider_config = (run.get("manifest") or {}).get("provider") or {}
    if provider_config.get("kind") == "replay":
        return ReplayProvider(provider_config.get("fixture") or {}).invoke
    return None


class WorkerLoop:
    def __init__(self, service: RunService) -> None:
        self.service = service

    def recover_interrupted(self) -> list[str]:
        """启动时回收上次崩溃遗留的中间态 Run。"""
        return self.service.store.runs.requeue_interrupted()

    def claim_and_execute(self) -> dict | None:
        """抢占并执行一个 queued Run；无待处理任务时返回 None。"""
        claimed = self.service.store.runs.claim_next_queued()
        if claimed is None:
            return None
        return self.service.execute(claimed["id"], provider=provider_for_run(claimed))

    def run_forever(self, poll_interval: float = 1.0) -> None:
        self.recover_interrupted()
        while True:
            if self.claim_and_execute() is None:
                time.sleep(poll_interval)
