# 备份与恢复

## SQLite（本地开发）

```
# 在线备份（sqlite3 backup API，WAL 安全；可带 artifacts）
uv run python -m motte_cli backup --target var/backups --artifacts-root var/artifacts

# 恢复（恢复前先停 API 与 Worker，避免活连接写坏刚恢复的库）
uv run python -m motte_cli restore --source var/backups

# 恢复到指定路径
uv run python -m motte_cli restore --source var/backups --db /tmp/restored.db
```

备份目录内容：`runs-<时间戳>.db`（数据库快照）、`artifacts-<时间戳>/`（工件快照，可选）、`manifest-<时间戳>.json`（清单：来源、大小、文件数）。恢复总是取目录中**最新**的 manifest；同秒多次备份以毫秒后缀区分。演练：`uv run pytest tests/storage/test_maintenance.py -q`。

## PostgreSQL（生产）

```
# 备份（自定义格式，支持并行恢复与选择性恢复）
pg_dump -Fc -h <host> -U motteavl -d motteavl -f motteavl-$(date +%Y%m%d).dump

# 恢复（先停 API 与 Worker）
pg_restore -h <host> -U motteavl -d motteavl --clean --if-exists motteavl-20260915.dump

# artifacts 目录直接文件级备份
tar -czf artifacts-$(date +%Y%m%d).tgz var/artifacts/
```

## Artifact TTL 清理

默认 dry-run 只报告；确认后加 `--apply` 才真正删除：

```
uv run python -m motte_cli cleanup-artifacts --older-than-days 30
uv run python -m motte_cli cleanup-artifacts --older-than-days 30 --apply
```

报告包含删除清单（名称/字节数）、保留数与可释放空间；清理后自动移除空目录。
