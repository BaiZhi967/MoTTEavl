# 版本回退（rollback）

原则：先备份、再回代码、最后回 schema；数据永远比代码先保住。

## 1. 备份当前状态

按 [backup-restore.md](backup-restore.md) 做一次备份（SQLite：`motte backup`；PostgreSQL：`pg_dump -Fc`）。

## 2. 回退应用代码

```
git log --oneline                 # 找到目标版本
git checkout <commit>             # 或 git revert <commit> 走前滚式回退（推荐，保留历史）
```

推荐 `git revert`：历史线性、可审计；紧急情况才用 checkout 挂旧版本。

## 3. 回退数据库 schema（仅当目标版本的迁移更旧）

迁移版本登记在 `alembic_version` 表；按步回退，每步对应一个版本文件的 `downgrade()`：

```
DATABASE_URL=... uv run alembic current      # 查看当前版本
DATABASE_URL=... uv run alembic downgrade -1 # 回退一步
DATABASE_URL=... uv run alembic downgrade 0001_initial   # 回退到指定版本
```

注意：
- 从 `0002_platform_integrity` downgrade 会删除 CaseAttempt、ScoringPass/ScoreSet、RunCommand 和 Run revision 证据，执行前确认备份存在且可恢复；
- 新版本若只是加表/加列，通常可以不回 schema（旧代码忽略新表）；改动列语义时才必须回退；
- 回退后跑 `uv run pytest -q -m "not live"` 验证兼容。

## 4. 重启与验证

```
make worker &                     # 单 Worker 取得锁后恢复；不确定调用进入 needs_review
uv run uvicorn apps.api.app.main:app --port 8000 &
uv run python -m motte_cli doctor
```

## 灰度检查单

- [ ] 备份文件存在且校验过（manifest / pg_restore -l）
- [ ] 回退后全量测试通过
- [ ] API /health 正常，doctor 显示 harness 安装正常
- [ ] SSE 与 Run 查询正常（抽查一个历史 Run 的 Trace/Score）


## M7 平台表降级（0015）

`0015_m7_platform_tables` 的 downgrade 在 `motte_imports`、`motte_import_mappings`
或 `motte_gc_tombstones` 有行时具名拒绝（导入审计与删除审计是证据，不能随降级静默
消失）；`motte_request_keys`/`motte_meta` 是运行时协调数据，可随降级丢弃。确认要
丢弃审计时手工清空对应表后再降级，或按上方流程从备份恢复旧版本。

回退 M7 应用本身（保留 0015 表）无需降 schema：旧应用忽略新表。安全环境变量
未配置时旧应用行为不变。
