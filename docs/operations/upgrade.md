# 升级

升级前停止 API、Worker 和 Celery consumer，并备份 `var/artifacts` 与数据库（SQLite：`var/runs.db`；PostgreSQL：`pg_dump` 或卷快照）。升级后运行 `make replay`、`uv run pytest tests/contract -q` 和 `make check`。

PostgreSQL 使用 Alembic：`DATABASE_URL=... uv run alembic upgrade head`。当前 head 是 `0003_resource_publications`：`0002_platform_integrity` 新增 Run revision、CaseAttempt、ScoringPass/ScoreSet 与 RunCommand 存储，`0003_resource_publications` 新增 append-only dataset/scenario 发布审计。迁移版本登记在 `alembic_version`；新增迁移使用 `uv run alembic revision -m "..."`。

SQLite 在打开数据库时执行幂等 schema upgrade，不再要求删除既有实体数据库。旧 `run-N`、旧事件和旧报告保持可读；新 Run 使用 UUID ID、schema v2 与 revision CAS。升级不会把旧 Score 物理改写成新 pass，新评分或 rescore 才会产生追加式 ScoringPass。

只启动一个 Worker。它必须在恢复中间态之前取得执行锁；升级前遗留的 `dispatching` CaseAttempt 会进入 `indeterminate`，对应 Run 进入 `needs_review`，不会自动重复外部调用。

完整行为变化、验证步骤和降级风险见 [平台完整性升级与运行说明](platform-integrity.md)。从当前 head 执行 `uv run alembic downgrade -1` 会删除 resource publication audit 表；继续回退才会删除 `0002` 的执行审计表。任何降级都只能在备份后执行；SQLite 应通过恢复升级前备份来回退。
