"""Celery 装配：MOTTE_WORKER_MODE=celery 时经 Redis 调度，默认 loop 模式不需要 broker。

本地/CI 无 Redis 时可用 MOTTE_CELERY_EAGER=1 让任务同步执行（仅测试用途）。
"""
from __future__ import annotations

import os

from celery import Celery


def create_celery_app() -> Celery:
    app = Celery("motte", broker=os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    app.conf.task_always_eager = os.environ.get("MOTTE_CELERY_EAGER", "0") == "1"
    return app


celery_app = create_celery_app()


@celery_app.task(name="motte.execute_run")
def execute_run_task(run_id: str, cases: list[str] | None = None):
    from .tasks import execute_run

    return execute_run(run_id, cases or ())
