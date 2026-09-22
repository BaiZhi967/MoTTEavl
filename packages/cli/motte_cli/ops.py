"""M7 运维命令：一致备份 / staging 恢复 / 恢复守卫 / 维护屏障 / GC / 历史导入。

对应协议 docs/protocols/sdk-and-migration.md frozen@1 §5/§9 与 T09/T10/T06-08
已交付的存储能力（motte_storage.maintenance / gc / sdk migration 管线）。

这些命令涉及宿主文件或本地库，**只有 local 模式**：``--mode server`` 一律
MODE/LOCAL_ONLY 错误退出 2（协议 §3），绝不远程执行。
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

from motte_cli import remote


def _store(args):
    from motte_storage.factory import create_run_store

    return create_run_store(getattr(args, "db", None))


def _db_path(args) -> str:
    return getattr(args, "db", None) or os.environ.get("MOTTE_DB_PATH", "var/runs.db")


def _artifacts_root(args) -> str | None:
    return getattr(args, "artifacts_root", None) or os.environ.get("ARTIFACT_ROOT")


def _emit(payload: Any, *, pretty: bool = False) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None))
    return 0


def _local_only(args, command: str) -> int | None:
    return remote.local_only_error(args, command)


# --------------------------------------------------------------- backup/restore


def backup_command(args) -> int:
    blocked = _local_only(args, "backup")
    if blocked is not None:
        return blocked
    from motte_storage.maintenance import (
        BackupIncomplete,
        BackupUnsupported,
        consistent_backup,
    )

    if args.consistent:
        try:
            manifest = consistent_backup(
                _store(args), args.target, artifacts_root=args.artifacts_root,
            )
        except BackupUnsupported as error:
            return remote.cli_error("BACKUP_UNSUPPORTED", str(error))
        except BackupIncomplete as error:
            # manifest 已落盘（status=incomplete）；失败必须可见，不静默标成功。
            return remote.cli_error(
                "BACKUP_INCOMPLETE",
                str(error),
                exit_code=3,
                missing=list(error.missing),
                hash_mismatches=list(error.mismatched),
            )
        return _emit(manifest, pretty=getattr(args, "pretty", False))
    # 既有 flag-less 行为 = legacy backup_sqlite（保持向后兼容）。
    from motte_storage.maintenance import backup_sqlite

    manifest = backup_sqlite(
        _db_path(args), args.target, artifacts_root=args.artifacts_root,
    )
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


def restore_command(args) -> int:
    blocked = _local_only(args, "restore")
    if blocked is not None:
        return blocked
    staging = getattr(args, "staging", None)
    if staging:
        from motte_storage.maintenance import RestoreIncomplete, restore_staging

        try:
            report = restore_staging(
                args.source, staging,
                confirm_overwrite=bool(getattr(args, "confirm_overwrite", False)),
            )
        except RestoreIncomplete as error:
            return remote.cli_error(
                "RESTORE_INCOMPLETE", str(error), exit_code=3, **error.details,
            )
        except FileExistsError as error:
            return remote.cli_error(
                "RESTORE_TARGET_EXISTS",
                f"{error} (pass --confirm-overwrite to replace it after backing up)",
            )
        return _emit(report, pretty=getattr(args, "pretty", False))
    # 既有行为 = legacy restore_sqlite（覆盖线上目标，v2 起要求确认）。
    from motte_storage.maintenance import restore_sqlite

    try:
        restored = restore_sqlite(
            args.source, _db_path(args),
            artifacts_root=_artifacts_root(args),
            confirm_overwrite=bool(getattr(args, "confirm_overwrite", False)),
        )
    except FileExistsError as error:
        return remote.cli_error(
            "RESTORE_TARGET_EXISTS",
            f"{error} (pass --confirm-overwrite to replace it after backing up)",
        )
    except FileNotFoundError as error:
        return remote.cli_error("BACKUP_NOT_FOUND", str(error))
    print(json.dumps(restored, ensure_ascii=False))
    return 0


def restore_guard_command(args) -> int:
    blocked = _local_only(args, "restore-guard")
    if blocked is not None:
        return blocked
    from motte_storage.maintenance import RESTORE_GUARD_KEY
    from motte_storage.platform import platform_for

    if args.restore_guard_command == "status":
        store = _store(args)
        manifest = platform_for(store).meta.get(RESTORE_GUARD_KEY)
        return _emit(
            {"active": manifest is not None, "manifest": manifest},
            pretty=getattr(args, "pretty", False),
        )
    if args.restore_guard_command == "clear":
        if not args.yes:
            return remote.cli_error(
                "CONFIRM_REQUIRED",
                "restore-guard clear is an explicit operator decision; re-run with --yes",
            )
        from motte_storage.maintenance import clear_restore_guard

        try:
            outcome = clear_restore_guard(_db_path(args), confirm=True)
        except FileNotFoundError as error:
            return remote.cli_error("DB_NOT_FOUND", str(error))
        return _emit(outcome, pretty=getattr(args, "pretty", False))
    return remote.cli_error(
        "CONTRACT_INVALID", f"unknown restore-guard subcommand: {args.restore_guard_command}"
    )


def maintenance_command(args) -> int:
    blocked = _local_only(args, "maintenance")
    if blocked is not None:
        return blocked
    from motte_storage.maintenance import (
        MaintenanceConflict,
        begin_maintenance,
        end_maintenance,
        maintenance_status,
    )

    store = _store(args)
    try:
        if args.maintenance_command == "status":
            view = maintenance_status(store)
        elif args.maintenance_command == "begin":
            view = begin_maintenance(store, reason=args.reason)
        elif args.maintenance_command == "end":
            view = end_maintenance(store, owner=args.owner)
        else:
            return remote.cli_error(
                "CONTRACT_INVALID", f"unknown maintenance subcommand: {args.maintenance_command}"
            )
    except MaintenanceConflict as error:
        return remote.cli_error("MAINTENANCE_CONFLICT", str(error))
    return _emit(view, pretty=getattr(args, "pretty", False))


# -------------------------------------------------------------------------- gc


def gc_command(args) -> int:
    blocked = _local_only(args, "gc")
    if blocked is not None:
        return blocked
    from motte_storage.gc import apply_gc, plan_gc

    artifacts_root = _artifacts_root(args)
    if not artifacts_root:
        return remote.cli_error(
            "ARTIFACT_ROOT_REQUIRED",
            "gc requires --artifacts-root or ARTIFACT_ROOT",
        )
    ttl = float(getattr(args, "ttl_days", 90) or 90)
    if args.gc_command == "plan":
        plan = plan_gc(_store(args), artifacts_root, artifact_ttl_days=ttl)
        return _emit(
            {"summary": plan.summary(), "protected": plan.protected,
             "deletable": plan.deletable},
            pretty=getattr(args, "pretty", False),
        )
    if args.gc_command == "apply":
        if not args.confirm:
            return remote.cli_error(
                "CONFIRM_REQUIRED",
                "gc apply is destructive; dry-run ('gc plan') is the default. "
                "Re-run with --confirm to delete.",
            )
        store = _store(args)
        plan = plan_gc(store, artifacts_root, artifact_ttl_days=ttl)
        report = apply_gc(store, artifacts_root, plan, confirm=True)
        return _emit(report, pretty=getattr(args, "pretty", False))
    return remote.cli_error("CONTRACT_INVALID", f"unknown gc subcommand: {args.gc_command}")


# ---------------------------------------------------------------------- import


def _import_report_json(report: Any) -> dict[str, Any]:
    return report.model_dump(mode="json") if hasattr(report, "model_dump") else dict(report)


def import_command(args) -> int:
    blocked = _local_only(args, "import")
    if blocked is not None:
        return blocked
    from motte_sdk.migration import (
        RollbackNotConfirmed,
        SourceChangedError,
        SourcePackageError,
        apply_import,
        load_source_package,
        plan_import,
        rollback_import,
    )
    from motte_storage.platform import ImportConflict

    command = args.import_command
    if command not in ("plan", "apply", "rollback"):
        return remote.cli_error("CONTRACT_INVALID", f"unknown import subcommand: {command}")

    store = _store(args)
    if command in ("plan", "apply"):
        try:
            source = load_source_package(args.source)
        except SourcePackageError as error:
            return remote.cli_error(error.code, str(error))
        try:
            if command == "plan":
                report = plan_import(store, source, operator=args.operator)
            else:
                # apply_import 内部复验 content_sha256（协议 §5.2：hash 不一致 →
                # 计划失效，必须重新 dry-run）。
                report = apply_import(store, args.artifacts_root, source, operator=args.operator)
        except SourceChangedError as error:
            return remote.cli_error("SOURCE_CHANGED", str(error))
        except ImportConflict as error:
            return remote.cli_error(error.code, str(error))
        return _emit(_import_report_json(report), pretty=getattr(args, "pretty", False))

    # rollback
    if not args.confirm:
        return remote.cli_error(
            "CONFIRM_REQUIRED",
            "import rollback is destructive; re-run with --confirm after reviewing the batch",
        )
    try:
        outcome = rollback_import(
            store, args.artifacts_root, args.import_id,
            operator=args.operator, confirm=True,
        )
    except RollbackNotConfirmed as error:  # pragma: no cover - confirm=True 已给出
        return remote.cli_error("CONFIRM_REQUIRED", str(error))
    except KeyError as error:
        return remote.cli_error("IMPORT_NOT_FOUND", str(error))
    except ImportConflict as error:
        return remote.cli_error(error.code, str(error))
    return _emit(outcome, pretty=getattr(args, "pretty", False))


# -------------------------------------------------------------------- parsers


def add_ops_parsers(sub: argparse._SubParsersAction) -> None:
    backup = sub.add_parser(
        "backup",
        help="备份本地库（--consistent：M7 一致备份=维护屏障+引用制校验+Manifest v2；"
             "缺省保持 legacy 在线备份）",
    )
    backup.add_argument("--target", required=True, help="备份目录")
    backup.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH（var/runs.db）")
    backup.add_argument("--artifacts-root", default=None, help="artifact 根目录（默认不备份工件）")
    backup.add_argument("--consistent", action="store_true",
                        help="M7 一致备份（begin/end_maintenance 屏障 + 引用制 artifact 快照 + Manifest v2）")
    backup.add_argument("--pretty", action="store_true", help="缩进 JSON 输出")
    remote.add_mode_arguments(backup)

    restore = sub.add_parser(
        "restore",
        help="恢复备份（--staging：M7 全新 staging 目录全量校验；缺省保持 legacy 覆盖恢复）",
    )
    restore.add_argument("--source", required=True, help="备份目录")
    restore.add_argument("--staging", help="恢复到全新 staging 目录（不触碰线上库；置恢复守卫）")
    restore.add_argument("--confirm-overwrite", action="store_true",
                         help="覆盖已存在且非空的 staging 目录（先备份）")
    restore.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH（var/runs.db）")
    restore.add_argument("--artifacts-root", default=None, help="artifact 根目录（备份含工件时恢复）")
    restore.add_argument("--pretty", action="store_true", help="缩进 JSON 输出")
    remote.add_mode_arguments(restore)

    guard = sub.add_parser(
        "restore-guard", help="恢复守卫：staging 恢复后阻止 Worker 领取，直到显式解除",
    )
    guard_sub = guard.add_subparsers(dest="restore_guard_command", required=True)
    guard_status = guard_sub.add_parser("status", help="读取守卫状态（只读）")
    guard_clear = guard_sub.add_parser("clear", help="解除守卫（必须 --yes）")
    guard_clear.add_argument("--yes", action="store_true", help="显式确认解除守卫")
    for parser in (guard_status, guard_clear):
        parser.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH（var/runs.db）")
        parser.add_argument("--pretty", action="store_true", help="缩进 JSON 输出")
        remote.add_mode_arguments(parser)

    maintenance = sub.add_parser(
        "maintenance", help="维护屏障：备份/GC 窗口内 API 写入口 503、Worker 拒绝领取",
    )
    maintenance_sub = maintenance.add_subparsers(dest="maintenance_command", required=True)
    maintenance_status_parser = maintenance_sub.add_parser("status", help="读取屏障状态（只读）")
    maintenance_begin = maintenance_sub.add_parser("begin", help="置维护屏障（幂等）")
    maintenance_begin.add_argument("--reason", default="cli maintenance", help="维护原因（审计）")
    maintenance_end = maintenance_sub.add_parser("end", help="使用 begin 返回的 owner 解除维护屏障")
    maintenance_end.add_argument("--owner", required=True, help="maintenance begin 返回的 lease owner")
    for parser in (maintenance_status_parser, maintenance_begin, maintenance_end):
        parser.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH（var/runs.db）")
        parser.add_argument("--pretty", action="store_true", help="缩进 JSON 输出")
        remote.add_mode_arguments(parser)

    gc = sub.add_parser(
        "gc", help="M7 retention/GC：引用与 pin 保护、dry-run 计划、tombstone 审计（默认 dry-run）",
    )
    gc_sub = gc.add_subparsers(dest="gc_command", required=True)
    gc_plan = gc_sub.add_parser("plan", help="只读计算删除计划（summary + 分类清单）")
    gc_apply = gc_sub.add_parser("apply", help="执行计划（必须 --confirm；自动持维护屏障）")
    gc_apply.add_argument("--confirm", action="store_true", help="确认删除（缺省拒绝）")
    for parser in (gc_plan, gc_apply):
        parser.add_argument("--artifacts-root", default=None, help="artifact 根目录，默认 ARTIFACT_ROOT")
        parser.add_argument("--ttl-days", type=float, default=90, dest="ttl_days",
                            help="artifact TTL 天数（默认 90）")
        parser.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH（var/runs.db）")
        parser.add_argument("--json", action="store_true", help="单行 JSON 输出")
        parser.add_argument("--pretty", action="store_true", help="缩进 JSON 输出")
        remote.add_mode_arguments(parser)

    importer = sub.add_parser(
        "import",
        help="M7 历史迁移：来源包加载 → dry-run 计划 → checkpoint 化 apply → 受限回退",
    )
    import_sub = importer.add_subparsers(dest="import_command", required=True)
    import_plan = import_sub.add_parser(
        "plan", help="dry-run：零业务写入，输出只读 ImportReport（协议 §5.2）",
    )
    import_plan.add_argument("--source", required=True, help="来源包目录（manifest.json + records/ + artifacts/）")
    import_plan.add_argument("--operator", default="cli", help="操作者（审计）")
    import_apply = import_sub.add_parser(
        "apply", help="执行（或续跑）导入；每单元一个提交点，apply 前复验 content hash",
    )
    import_apply.add_argument("--source", required=True, help="来源包目录")
    import_apply.add_argument("--operator", default="cli", help="操作者（审计）")
    import_apply.add_argument("--artifacts-root", default=None,
                              help="artifact 根目录，默认 ARTIFACT_ROOT")
    import_rollback = import_sub.add_parser(
        "rollback", help="受限回退一个导入批次（只删本批次创建且未被引用的对象）",
    )
    import_rollback.add_argument("--import-id", required=True, dest="import_id")
    import_rollback.add_argument("--confirm", action="store_true", help="确认回退")
    import_rollback.add_argument("--operator", default="cli", help="操作者（审计）")
    import_rollback.add_argument("--artifacts-root", default=None,
                                 help="artifact 根目录，默认 ARTIFACT_ROOT")
    for parser in (import_plan, import_apply, import_rollback):
        parser.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH（var/runs.db）")
        parser.add_argument("--pretty", action="store_true", help="缩进 JSON 输出")
        remote.add_mode_arguments(parser)
