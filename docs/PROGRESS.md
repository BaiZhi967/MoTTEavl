# MoTTEavl 开发进度

依据：`docs/superpowers/specs/2026-09-14-llm-agent-harness-evaluation-platform-design.md`、实现计划与运行安全 ADR。

约定：每个 Task 完成后运行对应 focused tests，提交一个 commit；每个里程碑完成后推送 `main`。真实 Provider/Harness 付费评测由操作者自行启动，本记录只标记仓库内验证与 replay 验证。

| Task | 内容 | 状态 | Commit | 验证 |
|---|---|---|---|---|
| 1 | 工作区、依赖、Compose、CI | ✅ | `2f8e15c`（含基础提交） | `uv run pytest tests/test_workspace_health.py -q`（1 passed）；Compose config 通过 |
| 2 | Canonical contracts 与 schema | ✅ | `02f698c` | `uv run pytest tests/contract -q`（8 passed）；schema 导出 16 个 contract |
| 3 | Storage、migration、artifact | ✅ | `96d92ee` | `uv run pytest tests/storage -q`（4 passed）；ArtifactStore SHA-256/path safety |
| 4 | Provider runtime 与 model catalog | ✅ | `4aacb84` | `uv run pytest tests/provider -q`（4 passed）；四协议归一化、strict 校验、pricing |
| 5 | Trace、replay、executor、foundation slice | ✅ | `5599e6c` | `uv run pytest tests/trace tests/runtime -q`（3 passed）；trace/redaction/replay/idempotent executor/scheduler 已验证 |
| 6 | Docker sandbox、Skill、ToolRegistry | ✅ | `1faeaa3` | `uv run pytest tests/sandbox -q`（4 passed）；默认 deny network、策略分离、tool modes |
| 7 | BuiltinReAct、Pi bridge | ✅ | `ff9fcb0` | `uv run pytest tests/runtime -q`（4 passed）；BuiltinReAct、Pi JSONL protocol |
| 8 | Claude/Codex Harness | ✅ | `1d67798` | `uv run pytest tests/harness -q`（3 passed）；JSONL parser、进程生命周期、terminal channel、probe |
| 9 | Evaluators、aggregation、gates、Inspect | ✅ | `1702957` | `uv run pytest tests/evaluators -q`（4 passed）；deterministic/trajectory/aggregate/pass@k/gate/judge metadata |
| 10 | API、Celery worker、CLI | ✅ | `c6daa75`（基于 `60f03e2`、`e1f9b49`） | `uv run pytest tests/api tests/cli -q`（3 passed）；`uv run python -m motte_cli --help` 可用 |
| 11 | React Web console | ✅ | `c5ecf86` | `pnpm --dir apps/web test`（web workspace healthy）；中文能力/限制/运行时间线/评分组件 |
| 12 | Replay integration、发布与运维文档 | ✅ | `49138cc`（基于 `e428fae`、`d9aa9a6`、`2afcfe0`） | `uv run pytest -q`（38 passed）；replay 1 passed；`uv run ruff check .`；compileall；Compose config；Web test 通过；CI 使用 portable compileall type gate |

状态说明：⏳ 未开始，🚧 开发中，✅ 已通过仓库验证，⚠️ 受外部依赖或未运行 live smoke 影响。

## 当前推进阶段：Direct LLM Run 纵向切片

阶段目标是让 API、CLI、Worker 和 SDK 共用一个可持久化的 RunService，并先以 replay Provider 验证完整生命周期，再接入真实 Provider。

- [x] 共享 `RunService`：create/get/execute/cancel/rescore/events
- [x] queued/running/completed/cancelled 生命周期与幂等执行
- [x] API 使用共享服务并提供 Run 查询、SSE、cancel、rescore 入口
- [x] CLI 与 Worker 使用同一服务入口
- [x] 新增 4 个 RunService 回归测试；全量测试达到 42 passed
- [ ] 将进程内 repository 替换为 SQLite 开发实现与 PostgreSQL 生产实现
- [ ] 接入 replay Provider 的真实 Trace/Score 产物
- [ ] 在不触发付费调用的前提下完成 Direct LLM E2E
- [ ] 由操作者执行一次显式 live smoke 并记录 Provider 证据
