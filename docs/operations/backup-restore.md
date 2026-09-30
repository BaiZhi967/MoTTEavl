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
M8 的 `restore_postgres_staging(backup_dir, staging_dsn, artifacts_target)` 只接受
**已创建但没有用户对象的独立 staging 数据库**与空 Artifact 目标。调用前核对
Manifest v2 自身、dump 和每个 Artifact 的大小/hash；损坏备份在连接目标前拒绝。
它用 `pg_restore --single-transaction --exit-on-error` 写入空目标，随即设置 `restored_from_backup`
守卫并清除 dump 继承的 maintenance 标志，然后核对 Alembic revision、
Run/Pass/Baseline/Gate 计数和引用文件；失败时不覆盖来源库，部分恢复目标
留给操作者调查。
报告只返回实际数据库名，不回显带凭据的 staging DSN；`pg_restore` 失败也只给
退出码与调查提示，不把可能包含连接参数的 stderr 写进公共错误。
不使用 `--clean` 删除现有数据库对象，也不自动解除守卫或启动 Worker。

操作员把 staging DSN 作为环境变量引用传入，不在命令行写凭据正文。例如在已
准备好**新的空库**和空工件目录后，从受控脚本调用：

```python
import os
from motte_storage.maintenance import restore_postgres_staging

report = restore_postgres_staging(
    "./backup", os.environ["MOTTE_STAGING_PG_DSN"], "./staging-artifacts",
    expected_manifest_sha256="从备份收据固定的 manifest_sha256",
)
print(report)
```

先核对 `unresolved_runs`、引用、工件与环境版本；`restore-guard clear --yes`
必须是另一次显式操作。当前没有 Compose build/up 与生产恢复演练收据，不能因
一次性 CI 数据库通过就声明生产恢复就绪。

## Artifact 清理 / GC（M7）

旧入口 `cleanup_artifacts` / CLI `cleanup-artifacts` 仅保留按 mtime 的只读诊断；
其报告字段为兼容旧消费者仍使用 `deleted`，dry-run 列出的只是候选，绝不代表
允许删除或已经删除。未绑定数据库的 `dry_run=False` / `--apply` 已弃用并明确拒绝：
Python 抛 `BackupUnsupported`，CLI 返回 `CLEANUP_UNSUPPORTED` 与退出码 2。
请明确指定正确数据库和工件根目录，改用引用保护的 `motte gc plan/apply`；
不会从工件路径猜测数据库或静默改用另一目标。
M7 GC 见 `motte_storage.gc`：默认 dry-run（`plan_gc`），apply（`apply_gc(confirm=True)`）
在维护屏障内执行，被引用/被 pin（needs_review、imported、baseline 引用）/
导入审计工件永不删除，每次删除写 tombstone（`motte_gc_tombstones`）保留
hash/bytes/原因/时间。删除前先持久写入 `deleting` 意图，观察到 unlink 成功后
才标 `deleted`；明确错误标 `failed`，重放时只观察到文件缺失则标
`absence_unconfirmed`/`missing_unobserved`，不伪造已观察删除，也不宣称文件系统
与数据库删除具有原子事务性。CLI `gc plan/apply` 自 M7-T03 起接线：

```
motte gc plan --artifacts-root var/artifacts --ttl-days 90 --db var/runs.db
motte gc apply --confirm --artifacts-root var/artifacts --db var/runs.db
```

演练：`uv run pytest -q tests/storage/test_maintenance.py
tests/integration/test_backup_restore_consistency.py tests/security/test_gc_retention.py`。

## M8 维护互斥与一致性边界

- 先停止 Worker/CLI 执行器，再开始备份、GC 或导入回退。维护过程使用与
  执行器相同的跨进程锁；执行器仍占锁时返回 `MaintenanceConflict`，不会在
  一个执行步骤中间截断。竞争维护即使 `reason` 相同也不能复用另一个 owner；
  只有传回当前活跃 owner 才能幂等重入，解除也核对 owner
- 持久库的激活/解除在一个数据库事务内完成。SQLite 使用库内触发器阻止
  Run、子记录、Baseline/Gate pin 等业务表的直接 INSERT/UPDATE/DELETE；激活前
  预建正常运行会按需创建的 ScoringJob/ResourceStore 表，避免晚建表绕过屏障；
  PostgreSQL 另持有 SHARE 表锁，排空在途写事务后保持到 dump、引用扫描、
  counts 和工件复制全部结束。tombstone 会影响已回退引用的识别，因此备份也
  冻结它；只有 GC/rollback 显式声明写审计模式才允许更新该表，reason 标签
  不赋予这一权限。PG 锁等待上限为 10 秒，超时失败，不输出成功清单
- 备份、GC、回退传入同一个 `artifacts_root` 并锁住整个窗口；`ArtifactStore`
  的写入/删除也使用该根目录旁的跨进程锁。维护中的删除仅接受活跃维护 owner。
  直接用文件系统工具修改工件、并行 DDL/迁移或绕过平台维护元数据的管理操作不在
  支持范围内，必须停机协调。锁文件位于根目录旁，不会作为工件备份或清理
- 正常成功或失败均释放本次操作一次。进程崩溃会释放 OS/PG 会话锁，但保留
  maintenance 标志与数据库写保护；核对执行器已退出后，用原 owner 显式解除。
  staging 恢复清除全部继承的 maintenance owner/reason 字段，保持恢复守卫激活
- 备份、GC、回退共用引用解码/遍历：包括 artifact id/artifact_id/sha256、
  `kind=artifact` 的 locator、raw_ref/raw_bundle_artifact、Run 子记录、ScoreSet、
  Baseline/Gate、Trace/旧 scores 和导入账本。GC 也按内容 hash 保护只含摘要的引用，
  plan/apply 都保护 needs_review；apply 会跳过计划后内容/大小已改变的文件。
  同样扫描独立评分 Job/校准 Invocation 与待运行 ExperimentSpec/Cell 引用。
  备份把只含摘要的引用解析为工件实际内容；找不到对应内容或存在引用却未传
  `artifacts_root` 时写出 incomplete 清单，不会静默生成无法完整恢复的备份。
  同一路径出现互相矛盾的 hash 声明属于无效引用集合，抛
  `ArtifactReferenceConflict`（`ValueError`），不生成 manifest；这区别于有效引用
  对应文件缺失/内容不符的 `BackupIncomplete` + incomplete manifest
- 内存后端没有持久写屏障，维护操作明确抛 `BackupUnsupported`；不得把内存
  metadata 标志当成 SQLite/PG 级别互斥。PG 备份传入的 store 必须与 dump 使用
  同一 DSN，避免锁住一个库、实际导出另一个库

本轮 SQLite 跨进程反例与恢复测试属于本地验证。PG dump 后竞争写入及工件改写
的隔离恢复测试位于 `tests/integration/test_m8_pg_restore.py`；只有提供一次性
loopback PG 和 pg_dump/pg_restore 实际通过后才能升级该项证据，缺依赖时仍为
未运行，不能用 SQLite 或 mock 结果宣称 PG/Compose 生产恢复已经通过。
