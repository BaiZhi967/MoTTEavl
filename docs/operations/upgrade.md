# 升级

升级前保存 `var/artifacts` 与数据库（`var/runs.db`）；完成后运行 `make replay`、`uv run pytest tests/contract -q` 并检查 schema 兼容性。

注意：阶段 1 落地实体表（runs/case_runs/trace_events/scores）时，旧版 KV 结构的本地 `var/runs.db` 不做自动迁移，直接删除重建即可。

