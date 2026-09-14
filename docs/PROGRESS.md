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
| 6 | Docker sandbox、Skill、ToolRegistry | ⏳ | — | — |
| 7 | BuiltinReAct、Pi bridge | ⏳ | — | — |
| 8 | Claude/Codex Harness | ⏳ | — | — |
| 9 | Evaluators、aggregation、gates、Inspect | ⏳ | — | — |
| 10 | API、Celery worker、CLI | ⏳ | — | — |
| 11 | React Web console | ⏳ | — | — |
| 12 | Replay integration、发布与运维文档 | ⏳ | — | — |

状态说明：⏳ 未开始，🚧 开发中，✅ 已通过仓库验证，⚠️ 受外部依赖或未运行 live smoke 影响。
