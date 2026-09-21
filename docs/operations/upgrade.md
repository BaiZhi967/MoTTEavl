# 升级

升级前停止 API、Worker 和 Celery consumer，并备份 `var/artifacts` 与数据库（SQLite：`var/runs.db`；PostgreSQL：`pg_dump` 或卷快照）。升级后运行 `make replay`、`uv run pytest tests/contract -q` 和 `make check`。

PostgreSQL 使用 Alembic：`DATABASE_URL=... uv run alembic upgrade head`。当前 head 是 `0003_resource_publications`：`0002_platform_integrity` 新增 Run revision、CaseAttempt、ScoringPass/ScoreSet 与 RunCommand 存储，`0003_resource_publications` 新增 append-only dataset/scenario 发布审计。迁移版本登记在 `alembic_version`；新增迁移使用 `uv run alembic revision -m "..."`。

SQLite 在打开数据库时执行幂等 schema upgrade，不再要求删除既有实体数据库。旧 `run-N`、旧事件和旧报告保持可读；新 Run 使用 UUID ID、schema v2 与 revision CAS。升级不会把旧 Score 物理改写成新 pass，新评分或 rescore 才会产生追加式 ScoringPass。

只启动一个 Worker。它必须在恢复中间态之前取得执行锁；升级前遗留的 `dispatching` CaseAttempt 会进入 `indeterminate`，对应 Run 进入 `needs_review`，不会自动重复外部调用。

完整行为变化、验证步骤和降级风险见 [平台完整性升级与运行说明](platform-integrity.md)。从当前 head 执行 `uv run alembic downgrade -1` 会删除 resource publication audit 表；继续回退才会删除 `0002` 的执行审计表。任何降级都只能在备份后执行；SQLite 应通过恢复升级前备份来回退。


## M7（SDK/迁移/发布）升级说明

- PostgreSQL Alembic head 现为 `0015_m7_platform_tables`（在 `0014_experiments_and_gates`
  之上新增平台表：`motte_request_keys` 幂等注册表、`motte_meta` 运维 KV、
  `motte_imports`/`motte_import_mappings` 导入账本、`motte_gc_tombstones` GC 审计）。
  SQLite 在打开库时自动补齐同名表，无需手工操作。
- 安全环境变量（全部可选，默认仅信任 loopback Host）：
  - `MOTTE_ALLOWED_HOSTS`：逗号分隔 Host 白名单（默认 `localhost,127.0.0.1,::1,testserver`）。
    LAN/远程部署必须显式配置，否则 Host 校验返回 400 `HOST_REJECTED`（防 DNS rebinding）。
  - `MOTTE_ALLOWED_ORIGINS`：跨站 Origin 白名单；未列入的 Origin 对写方法返回 403
    `ORIGIN_REJECTED`（防不可信网页触发执行/改凭据）。
  - `MOTTE_API_TOKEN`：设置后所有 `/api/*` 要求 `Authorization: Bearer <token>`
    （`/health` 与 `/api/v1/capabilities` 豁免）。远程部署必须设置。
- 维护模式：`POST /api/v1/maintenance/begin` 后所有 `/api/*` 写方法返回 503
  `MAINTENANCE_MODE`，Worker 拒绝领取与恢复回队；`POST /api/v1/maintenance/end` 解除。
  一致备份（`motte backup`）自动持有该屏障。
- 恢复守卫：从备份恢复出的库带 `restored_from_backup` 标志，Worker 拒绝在其中
  领取任何 Run，直到 `motte restore-guard clear --yes` 显式解除（防止恢复包中的
  queued 任务被自动付费执行）。
- Run 创建幂等：`POST /api/v1/runs` 支持可选 `request_key`；同 key 同 body 幂等
  返回同一 Run，同 key 不同 body 返回 409 `REQUEST_KEY_CONFLICT`。
