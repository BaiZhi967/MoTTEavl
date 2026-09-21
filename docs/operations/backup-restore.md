# 备份与恢复

> M7 起默认使用**一致备份**（维护屏障 + 引用制工件校验 + Manifest v2）与
> **staging 恢复**（全新目录全量校验 + 恢复守卫）。协议：
> `docs/protocols/sdk-and-migration.md` §9。

## SQLite：一致备份（M7 推荐）

```
# CLI（M7-T03 起接线；--consistent = 一致备份，flag-less 保持 legacy 在线备份）
motte backup --consistent --target var/backups --db var/runs.db \
    --artifacts-root var/artifacts

# Python API
uv run python - <<'PY'
from motte_storage.factory import create_run_store
from motte_storage.maintenance import consistent_backup
store = create_run_store()          # MOTTE_DB_PATH / MOTTE_STORAGE
print(consistent_backup(store, "var/backups", artifacts_root="var/artifacts"))
PY
```

行为（`consistent_backup`）：

1. 进入维护屏障：期间 API 所有 `/api/*` 写方法 503 `MAINTENANCE_MODE`，
   Worker 拒绝领取与恢复回队（与采集/评分/GC 互斥）；
2. sqlite online backup（WAL 安全）+ checkpoint；
3. **引用制**工件快照：只复制数据库实际引用的不可变工件，逐文件 sha256 校验
   （payload 嵌入 hash 时额外核对）；缺失/不符 → manifest 标 `incomplete` 并抛
   `BackupIncomplete`，**绝不标成功**；根目录多余文件记 warning（GC 候选）；
4. Manifest v2：`manifest_version/backend/schema_version/alembic_revision/
   database{snapshot,bytes,sha256}/artifacts{dir,files[]}/counts/maintenance/
   status/manifest_sha256`；
5. 无论成败 finally 解除屏障。

## SQLite：staging 恢复（M7 推荐）

```
# CLI（M7-T03 起接线；--staging 恢复到全新目录，不触碰线上库）
motte restore --source var/backups --staging var/staging-restore --db var/runs.db
motte restore-guard status --db var/staging-restore/runs.db   # 守卫只读检查
motte restore-guard clear --yes --db var/staging-restore/runs.db  # 显式解除

# Python API
uv run python - <<'PY'
from motte_storage.maintenance import restore_staging, clear_restore_guard
report = restore_staging("var/backups", "var/staging-restore")
print(report)   # counts/unresolved_runs/restore_guard
# 只读检查通过、操作员确认后解除守卫（否则 Worker 永不领取）：
clear_restore_guard("var/staging-restore/runs.db", confirm=True)
PY
```

校验链：manifest 哈希 → DB 快照哈希 → schema 指纹 → 逐工件哈希 → 打开 staging
核对 counts → 引用完整性。任一失败抛 `RestoreIncomplete`。恢复后：

- staging 库带 `restored_from_backup` 守卫，Worker 拒绝在其中领取任何 Run；
- `queued/preparing/running/collecting/scoring/needs_review` Run 原样列入
  `unresolved_runs`，等显式决定，**不自动付费执行**；
- staging 目录已存在且非空需 `confirm_overwrite=True`；线上库永不被触碰。

旧 `restore_sqlite` 仍在（覆盖已有目标必须 `confirm_overwrite=True`）；新代码
一律用 staging 恢复。

## PostgreSQL（生产）

`consistent_backup_postgres(dsn, target, artifacts_root=...)`：屏障 +
`pg_dump --format=custom`（argv 列表）+ 同一套引用制工件快照与 Manifest v2；
PATH 无 `pg_dump` 时抛 `BackupUnsupported`（支持矩阵如实登记 blocked）。
手工等价：

```
pg_dump -Fc -h <host> -U motteavl -d motteavl -f motteavl-$(date +%Y%m%d).dump
pg_restore -h <host> -U motteavl -d motteavl --clean --if-exists motteavl-20260915.dump
tar -czf artifacts-$(date +%Y%m%d).tgz var/artifacts/
```

## Artifact 清理 / GC（M7）

TTL 清理（旧入口，仅按 mtime；CLI `cleanup-artifacts` 保留）与 M7 GC 并存；
M7 GC 见 `motte_storage.gc`：默认 dry-run（`plan_gc`），apply（`apply_gc(confirm=True)`）
在维护屏障内执行，被引用/被 pin（needs_review、imported、baseline 引用）/
导入审计工件永不删除，每次删除写 tombstone（`motte_gc_tombstones`）保留
hash/bytes/原因/时间。CLI `gc plan/apply` 自 M7-T03 起接线：

```
motte gc plan --artifacts-root var/artifacts --ttl-days 90 --db var/runs.db
motte gc apply --confirm --artifacts-root var/artifacts --db var/runs.db
```

演练：`uv run pytest -q tests/storage/test_maintenance.py
tests/integration/test_backup_restore_consistency.py tests/security/test_gc_retention.py`。
