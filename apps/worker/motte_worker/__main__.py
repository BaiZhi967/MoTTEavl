"""`python -m motte_worker` 入口。"""
from __future__ import annotations

import argparse

from motte_sdk.service import RunService
from motte_storage.run_store import SQLiteRunStore

from .reporting import WorkerReporter
from .runtime import WorkerLoop
from .tasks import get_service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="motte-worker", description="MoTTEavl durable run worker")
    parser.add_argument("--db", default=None, help="SQLite 路径，默认 MOTTE_DB_PATH（var/runs.db）")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--once", action="store_true", help="处理完当前待处理 Run 后退出（不做长轮询）")
    parser.add_argument("--quiet", action="store_true", help="关闭 Worker stderr 进度日志")
    args = parser.parse_args(argv)

    service = RunService(SQLiteRunStore(args.db)) if args.db is not None else get_service()
    loop = WorkerLoop(service, reporter=WorkerReporter(enabled=not args.quiet))
    loop.recover_interrupted()
    if args.once:
        while loop.claim_and_execute() is not None:
            pass
        return 0
    loop.run_forever(poll_interval=args.poll_interval, recover=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
