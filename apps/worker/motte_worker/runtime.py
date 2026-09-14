"""Worker 调度循环：从持久化存储抢占 queued Run 并执行。

默认 loop 模式只依赖 SQLite（本地开发零外部服务）；celery 模式见 celery_app.py。
"""
from __future__ import annotations

import time
from typing import Any

from motte_provider.capabilities import UnsupportedParameterError
from motte_provider.config import build_case_provider
from motte_sdk.replay_run import ReplayProvider
from motte_sdk.service import RunService


def provider_for_run(run: dict[str, Any]):
    """按 Run manifest 选择 provider；构造失败（strict 预检）会抛异常阻断付费调用。"""
    manifest = run.get("manifest") or {}
    provider_config = manifest.get("provider") or {}
    kind = provider_config.get("kind")
    if not kind:
        return None
    if kind == "replay":
        return ReplayProvider(provider_config.get("fixture") or {}).invoke
    if kind == "openai_compatible":
        return build_case_provider(provider_config, manifest.get("cases") or {}).invoke
    raise ValueError(f"unsupported provider kind: {kind!r}")


class WorkerLoop:
    def __init__(self, service: RunService) -> None:
        self.service = service

    def recover_interrupted(self) -> list[str]:
        """启动时回收上次崩溃遗留的中间态 Run。"""
        return self.service.store.runs.requeue_interrupted()

    def claim_and_execute(self) -> dict | None:
        """抢占并执行一个 queued Run；无待处理任务时返回 None。

        provider 构造失败不产生任何网络调用，Run 落 unsupported 状态。
        """
        claimed = self.service.store.runs.claim_next_queued()
        if claimed is None:
            return None
        run_id = claimed["id"]
        try:
            provider = provider_for_run(claimed)
        except UnsupportedParameterError as error:
            return self.service.mark_unsupported(run_id, "UNSUPPORTED_PARAMETER", message=str(error))
        except ValueError as error:
            return self.service.mark_unsupported(run_id, "PROVIDER_CONFIG_INVALID", message=str(error))
        return self.service.execute(run_id, provider=provider)

    def run_forever(self, poll_interval: float = 1.0) -> None:
        self.recover_interrupted()
        while True:
            if self.claim_and_execute() is None:
                time.sleep(poll_interval)
