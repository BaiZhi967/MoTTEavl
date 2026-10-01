"""Local-only saved-plan Trace retention. No environment policy or automatic apply."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from motte_cli import remote


def _existing_store(args: argparse.Namespace):
    from motte_storage.run_store import SQLiteRunStore

    if os.environ.get('MOTTE_STORAGE', 'sqlite') == 'sqlite':
        path = args.db or os.environ.get('MOTTE_DB_PATH', 'var/runs.db')
        return SQLiteRunStore(path, initialize=False)
    from motte_storage.factory import create_run_store
    return create_run_store(args.db, migrate=False)


def trace_retention_command(args: argparse.Namespace) -> int:
    blocked = remote.local_only_error(args, 'trace-retention')
    if blocked is not None:
        return blocked
    from motte_contracts.identity import canonical_json_bytes
    from motte_sdk.trace_retention import (
        TraceRetentionConfig, TraceRetentionPlan, apply_trace_retention, plan_trace_retention,
    )
    from motte_storage.maintenance import BackupUnsupported, MaintenanceConflict
    from sqlite3 import DatabaseError

    applying = args.trace_retention_command == 'apply'
    if applying and not args.confirm:
        return remote.cli_error('CONFIRM_REQUIRED', 'trace-retention apply requires --confirm')
    errors = (OSError, ValueError, RuntimeError, DatabaseError, BackupUnsupported, MaintenanceConflict)
    if os.environ.get('MOTTE_STORAGE', 'sqlite') == 'postgres':
        try:
            from psycopg import Error as PostgresError
        except ImportError:
            return remote.cli_error(
                'TRACE_RETENTION_REJECTED',
                'PostgreSQL Trace retention requires the optional PostgreSQL storage driver',
            )
        errors += (PostgresError,)
    try:
        config = (TraceRetentionConfig.model_validate_json(Path(args.config).read_bytes())
                  if args.config else TraceRetentionConfig())
        if applying:
            plan = TraceRetentionPlan.model_validate_json(Path(args.plan).read_bytes())
            result = apply_trace_retention(
                _existing_store(args), args.artifacts_root, plan, config=config, confirm=True,
            )
        else:
            output = Path(args.output)
            sources = [Path(args.config)] if args.config else []
            if os.environ.get('MOTTE_STORAGE', 'sqlite') == 'sqlite':
                database = Path(args.db or os.environ.get('MOTTE_DB_PATH', 'var/runs.db'))
                # SQLite resolves a symlinked database before naming its sidecars.
                # Protect both names before even opening the source (including SHM).
                for name in (database, database.resolve()):
                    sources.extend(Path(str(name) + suffix)
                                   for suffix in ('', '-wal', '-shm', '-journal'))
            for source in sources:
                if output.resolve() == source.resolve() or (
                    output.exists() and source.exists() and output.samefile(source)
                ):
                    raise ValueError('plan output must not overwrite a database or configuration input')
            plan = plan_trace_retention(_existing_store(args), config=config)
            output.write_bytes(canonical_json_bytes(plan.model_dump(mode='json')))
            result = plan
    except errors as error:
        return remote.cli_error('TRACE_RETENTION_REJECTED', str(error))
    print(canonical_json_bytes(result.model_dump(mode='json')).decode('utf-8'))
    return 0


def add_trace_retention_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser('trace-retention', help='显式本地 Trace 归档保留（默认关闭）')
    commands = parser.add_subparsers(dest='trace_retention_command', required=True)
    plan = commands.add_parser('plan', help='只读计划；未提供配置时禁用')
    plan.add_argument('--config', help='显式 JSON 配置文件')
    plan.add_argument('--output', required=True, help='保存规范化计划 JSON')
    apply = commands.add_parser('apply', help='复验并执行保存的精确计划；必须 --confirm')
    apply.add_argument('--config', required=True, help='与计划完全一致的 JSON 配置')
    apply.add_argument('--plan', required=True, help='已审阅保存的计划文件')
    apply.add_argument('--artifacts-root', required=True, help='已持久化的工件根目录')
    apply.add_argument('--confirm', action='store_true', help='确认执行显式计划')
    for command in (plan, apply):
        command.add_argument('--db', help='已有 SQLite 数据库；不会自动创建或升级')
        remote.add_mode_arguments(command)
