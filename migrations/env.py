"""迁移执行入口：`python migrations/env.py [--revert]`。

DSN 读取顺序：--dsn 参数 > MOTTE_PG_DSN > DATABASE_URL（兼容 postgresql+asyncpg 前缀）。
"""
from __future__ import annotations

import os
import sys


def _normalize_dsn(dsn: str) -> str:
    scheme, _, rest = dsn.partition("://")
    scheme = scheme.split("+", 1)[0]
    if scheme not in {"postgres", "postgresql"}:
        raise SystemExit(f"DSN 必须使用 postgres:// 或 postgresql://，当前：{scheme}://")
    return f"postgresql://{rest}"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    revert = "--revert" in argv
    dsn_flag = "--dsn"
    dsn = None
    if dsn_flag in argv:
        index = argv.index(dsn_flag)
        dsn = argv[index + 1]
        del argv[index : index + 2]
    if dsn is None:
        dsn = os.environ.get("MOTTE_PG_DSN") or os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("缺少 DSN：设置 MOTTE_PG_DSN 或 DATABASE_URL，或使用 --dsn")

    import psycopg

    from motte_storage.migrations import apply_migrations, revert_last_migration

    dsn = _normalize_dsn(dsn)
    with psycopg.connect(dsn) as connection:
        if revert:
            reverted = revert_last_migration(connection)
            print(f"reverted: {reverted}" if reverted else "nothing to revert")
        else:
            applied = apply_migrations(connection)
            print(f"applied: {applied}" if applied else "already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
