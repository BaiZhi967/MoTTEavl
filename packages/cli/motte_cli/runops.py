"""M7 run 生命周期子命令：run-list/get/cancel/retry/events/wait/report。

双模式（协议 sdk-and-migration.md frozen@1 §3）：

- local：直连 store / RunService（与既有 CLI 同一装配，不回归）；
- server：只经 ``MotteClient`` HTTP，失败映射为 §3 的 stderr JSON + 退出码，
  绝不回退本地。

stdout 纪律：成功输出机器可读 JSON（``--pretty`` 缩进；run-events 默认
JSONL 每行一条事件）；诊断只走 stderr。``run-wait`` 超时只停止等待
（TIMEOUT / 退出 2），**绝不取消远端 Run**；Ctrl-C → CANCELLED_BY_USER / 4。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from motte_cli import remote


def add_run_lifecycle_parsers(sub: argparse._SubParsersAction) -> None:
    """注册 run-list/get/cancel/retry/events/wait/report 子命令。"""
    run_list = sub.add_parser(
        "run-list", help="列出 Run（--status 过滤；server 模式走 GET /runs）",
    )
    run_list.add_argument("--status", help="按状态过滤（queued/completed/…）")
    _add_common(run_list)

    run_get = sub.add_parser("run-get", help="读取一个 Run 的当前视图")
    run_get.add_argument("run_id")
    _add_common(run_get)

    run_cancel = sub.add_parser("run-cancel", help="取消一个 Run（幂等）")
    run_cancel.add_argument("run_id")
    run_cancel.add_argument("--reason", help="取消原因（审计）")
    _add_common(run_cancel)

    run_retry = sub.add_parser("run-retry", help="重试终态 Run（superseding 子 Run）")
    run_retry.add_argument("run_id")
    _add_common(run_retry)

    run_events = sub.add_parser(
        "run-events",
        help="读取 Run 的持久事件流：默认 JSONL（每行一条），--snapshot 输出 JSON 数组",
    )
    run_events.add_argument("run_id")
    run_events.add_argument("--after", type=int, default=0, help="只取 seq 严格大于该值的事件")
    run_events.add_argument(
        "--snapshot", action="store_true",
        help="输出 JSON 数组（server 模式走 events/snapshot 端点）",
    )
    _add_common(run_events)

    run_wait = sub.add_parser(
        "run-wait",
        help="轮询等待 Run 终态（needs_review 也是终态）；超时不取消 Run",
    )
    run_wait.add_argument("run_id")
    run_wait.add_argument("--timeout", type=float, default=None, help="等待期限（秒）；超时退出 2 TIMEOUT")
    run_wait.add_argument("--poll", type=float, default=1.0, help="轮询间隔（秒，默认 1）")
    _add_common(run_wait)

    run_report = sub.add_parser(
        "run-report", help="读取 Run 报告（默认 current scoring pass）",
    )
    run_report.add_argument("run_id")
    run_report.add_argument("--scoring-pass-id", dest="scoring_pass_id", help="固定 scoring pass id")
    _add_common(run_report)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pretty", action="store_true", help="缩进 JSON 输出")
    parser.add_argument("--db", help="local 模式 SQLite 路径（server 模式禁止）")
    remote.add_mode_arguments(parser)


def _emit(args, payload: Any) -> int:
    indent = 2 if getattr(args, "pretty", False) else None
    print(json.dumps(payload, ensure_ascii=False, indent=indent))
    return 0


def _service(args):
    from motte_sdk.service import RunService, build_run_service
    from motte_storage.run_store import SQLiteRunStore

    return RunService(SQLiteRunStore(args.db)) if args.db else build_run_service()


# ---------------------------------------------------------------------- handlers


def run_list_command(args) -> int:
    status = getattr(args, "status", None)
    if remote.is_server(args):
        def invoke(client):
            listing = client.list_runs(status)
            return {
                "items": [view.raw for view in listing.items],
                "total": listing.total,
                **listing.extra,
            }

        outcome = remote.call_remote(args, invoke, default_code="RUN_NOT_FOUND")
        if isinstance(outcome, remote.RemoteOk):
            return _emit(args, outcome.payload)
        return outcome
    runs = _service(args).store.runs.list()
    if status is not None:
        runs = [run for run in runs if run.get("status") == status]
    return _emit(args, {"items": runs, "total": len(runs)})


def run_get_command(args) -> int:
    if remote.is_server(args):
        outcome = remote.call_remote(
            args, lambda client: client.get_run(args.run_id).raw,
            default_code="RUN_NOT_FOUND",
        )
        if isinstance(outcome, remote.RemoteOk):
            return _emit(args, outcome.payload)
        return outcome
    service = _service(args)
    try:
        run = service.get_run(args.run_id)
    except KeyError:
        return remote.cli_error("RUN_NOT_FOUND", f"run not found: {args.run_id}")
    return _emit(args, run)


def run_cancel_command(args) -> int:
    reason = getattr(args, "reason", None)
    if remote.is_server(args):
        outcome = remote.call_remote(
            args, lambda client: client.cancel_run(args.run_id, reason).raw,
            default_code="RUN_NOT_FOUND",
        )
        if isinstance(outcome, remote.RemoteOk):
            return _emit(args, outcome.payload)
        return outcome
    service = _service(args)
    try:
        run = service.cancel(args.run_id, reason=reason)
    except KeyError:
        return remote.cli_error("RUN_NOT_FOUND", f"run not found: {args.run_id}")
    except ValueError as error:
        return remote.cli_error("RUN_CONFLICT", str(error))
    return _emit(args, run)


def run_retry_command(args) -> int:
    if remote.is_server(args):
        outcome = remote.call_remote(
            args, lambda client: client.retry_run(args.run_id).raw,
            default_code="RUN_NOT_FOUND",
        )
        if isinstance(outcome, remote.RemoteOk):
            return _emit(args, outcome.payload)
        return outcome
    service = _service(args)
    try:
        run = service.retry(args.run_id)
    except KeyError:
        return remote.cli_error("RUN_NOT_FOUND", f"run not found: {args.run_id}")
    except ValueError as error:
        return remote.cli_error("RUN_CONFLICT", str(error))
    return _emit(args, run)


def run_events_command(args) -> int:
    after = max(0, int(getattr(args, "after", 0) or 0))
    if remote.is_server(args):
        outcome = remote.call_remote(
            args,
            lambda client: (
                _server_snapshot_events(client, args.run_id, after)
                if args.snapshot
                else _server_event_lines(client, args.run_id, after)
            ),
            default_code="RUN_NOT_FOUND",
        )
        if isinstance(outcome, remote.RemoteOk):
            if isinstance(outcome.payload, str):  # JSONL 模式已逐行打印
                return 0
            return _emit(args, outcome.payload)
        return outcome
    service = _service(args)
    try:
        service.get_run(args.run_id)
    except KeyError:
        return remote.cli_error("RUN_NOT_FOUND", f"run not found: {args.run_id}")
    events = service.events_after(args.run_id, after)
    if args.snapshot:
        return _emit(args, events)
    for event in events:
        print(json.dumps(event, ensure_ascii=False))
    return 0


def _server_snapshot_events(client, run_id: str, after: int) -> list[dict]:
    """Read every snapshot page while rejecting a non-advancing server cursor."""
    events: list[dict] = []
    cursor = after
    while True:
        snapshot = client.run_events_snapshot(run_id, after=cursor)
        events.extend(snapshot.events)
        if not snapshot.has_more:
            return events
        next_after = snapshot.next_after if snapshot.next_after is not None else snapshot.last_seq
        if next_after is None or next_after <= cursor:
            raise RuntimeError("events snapshot pagination cursor did not advance")
        cursor = next_after


def _server_event_lines(client, run_id: str, after: int) -> str:
    """server JSONL 模式：逐页取全量事件并逐行打印（stdout 机器数据）。"""
    for event in _server_snapshot_events(client, run_id, after):
        print(json.dumps(event, ensure_ascii=False))
    return ""


def run_wait_command(args) -> int:
    timeout = getattr(args, "timeout", None)
    poll = max(0.05, float(getattr(args, "poll", 1.0) or 1.0))
    if remote.is_server(args):
        def invoke(client):
            return client.wait_for_run(
                args.run_id, timeout=timeout, poll_interval=poll,
            )

        try:
            outcome = remote.call_remote(args, invoke, default_code="RUN_NOT_FOUND")
        except KeyboardInterrupt:
            return remote.cli_error(
                "CANCELLED_BY_USER", f"wait for run {args.run_id} cancelled by user",
                exit_code=4,
            )
        if isinstance(outcome, remote.RemoteOk):
            return _emit(args, outcome.payload)
        return outcome
    from motte_sdk.client_types import is_terminal_status

    service = _service(args)
    try:
        run = service.get_run(args.run_id)
    except KeyError:
        return remote.cli_error("RUN_NOT_FOUND", f"run not found: {args.run_id}")
    deadline = time.monotonic() + timeout if timeout is not None else None
    try:
        while not is_terminal_status(run.get("status")):
            if deadline is not None and time.monotonic() >= deadline:
                return remote.cli_error(
                    "TIMEOUT",
                    f"timed out waiting for run {args.run_id} "
                    f"(last status: {run.get('status')!r}); the run was NOT cancelled",
                    run={"id": run.get("id"), "status": run.get("status")},
                )
            time.sleep(poll)
            run = service.get_run(args.run_id)
    except KeyboardInterrupt:
        return remote.cli_error(
            "CANCELLED_BY_USER", f"wait for run {args.run_id} cancelled by user",
            exit_code=4,
        )
    return _emit(args, run)


def run_report_command(args) -> int:
    scoring_pass_id = getattr(args, "scoring_pass_id", None)
    if remote.is_server(args):
        outcome = remote.call_remote(
            args,
            lambda client: client.run_report(args.run_id, scoring_pass_id).raw,
            default_code="RUN_NOT_FOUND",
        )
        if isinstance(outcome, remote.RemoteOk):
            return _emit(args, outcome.payload)
        return outcome
    service = _service(args)
    try:
        report = _local_run_report(service, args.run_id, scoring_pass_id)
    except KeyError:
        return remote.cli_error("RUN_NOT_FOUND", f"run not found: {args.run_id}")
    if report is None:
        return remote.cli_error(
            "SCORING_PASS_NOT_FOUND",
            f"scoring pass not found for run: {scoring_pass_id}",
        )
    return _emit(args, report)


def _local_run_report(service, run_id: str, scoring_pass_id: str | None):
    """local 报告：与 API run_report 同一数据装配（view + 固定 pass）。

    返回 None 表示指定的 scoring pass 不属于该 Run（SCORING_PASS_NOT_FOUND）。
    """
    from motte_sdk.reporting import build_public_run_report

    run = service.get_run(run_id)
    if scoring_pass_id is not None:
        selected = service.store.scoring_passes.get(scoring_pass_id)
        if selected is None or selected.get("run_id") != run_id:
            return None
        run["scores"] = service.store.score_sets.list_for_pass(scoring_pass_id)
        run["current_scoring_pass_id"] = scoring_pass_id
        run["scoring_pass"] = selected
    return build_public_run_report(run)
