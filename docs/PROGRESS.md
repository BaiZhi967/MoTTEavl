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
- [x] CLI 与 Worker 使用同一服务入口；Worker 可通过 SQLite 路径接管 API 创建的 Run
- [x] TraceEvent 写入 repository，服务重启后可回放事件
- [x] 新增 8 个 RunService/Worker 回归测试及 API durable repository 验证；全量测试达到 49 passed
- [x] 增加 SQLite durable repository；API 默认使用 `MOTTE_DB_PATH`；RunService 重启后可继续分配 Run ID；PostgreSQL adapter 仍待接入
- [x] 接入 replay Provider 的确定性 Trace/Score 产物；RunService 持久化 model_response/score/completed 事件
- [x] 在不触发付费调用的前提下完成 Direct LLM E2E 基础路径
- [ ] 由操作者执行一次显式 live smoke 并记录 Provider 证据

## 阶段 A 后续步骤（2026-09-15）

- [x] **步骤 1：Worker 调度** — 增加 `enqueue_run` / `execute_queued_run`，覆盖 queued → running → completed。
- [x] **步骤 2：API replay E2E** — `POST /api/v1/runs/{id}/replay` 产出确定性 Score 和持久化 Trace。
- [x] **步骤 3：状态持久化** — failed、cancelled、unsupported、retry child run 均有明确状态与测试。
- [x] **步骤 4：PostgreSQL 边界** — 增加 DSN 校验、迁移入口和无依赖时的显式 unsupported 行为；真实数据库 smoke 待环境具备后执行。

本轮验证：`uv run pytest -q`（60 passed）、`uv run ruff check .`、PostgreSQL boundary tests 通过。真实 Provider、Celery broker 和 PostgreSQL 服务未启动。

阶段 A 的纵向链路已推进到 API → SQLite → Worker → RunService → replay Trace/Score；Provider 已增加可离线测试的 HTTP transport（`ce465fa`），尚未执行真实网络调用。

## 阶段 0：可复现开发基线（2026-09-15，`54761f9`）

- [x] pnpm-lock.yaml 入库；apps/web 依赖从 `latest` 固定为具体版本范围；vite 增加 `/api` 代理
- [x] 版本钉住：`.python-version`(3.12)、`.nvmrc`/`.node-version`(24)、`[tool.uv] required-version`；补齐 uvicorn 依赖
- [x] Makefile：`make install/test/lint/replay/worker/web-build/web-test/check/dev/clean`；删除占位 `scripts/*.ps1`
- [x] README/install.md/upgrade.md 补齐三进程启动说明；`.env.example` 标注各变量消费状态
- [x] CI 与本地命令对齐：全量 pytest + ruff + compileall + compose config + pnpm frozen install + web build
- [x] 六条验收门（uv sync / frozen pnpm / pytest / ruff / web build / compose config）全过；uvicorn 冒烟通过

## 阶段 1：Run 执行内核（2026-09-15）

状态机：`queued → preparing → running → collecting → scoring → completed`，终态含 `failed/cancelled/unsupported/profile_stale`；retry 创建 `parent_run_id` 子 Run。

| Task | Commit | 内容 |
|---|---|---|
| P1-1 | `83b4d29` | `build_run_service()` 工厂统一 API/CLI/Worker 构造；完整状态机与迁移校验；cancel 记录 reason 并阻断后续 case；retry 暴露 API；rescore 改为真实重算；`expected_for` 协议替代 `__self__.fixture` 反射 |
| P1-2 | `c12157f` | 实体存储 `run_store.py`：runs/case_runs/trace_events/scores 四表，`(run_id, seq)` 与 `(run_id, case_id)` 唯一约束；SQLite + InMemory 双实现；KV blob 不再承载 Run 实体 |
| P1-3 | `6e49edc` | Worker 入口 `python -m apps.worker.motte_worker`（`--once`）；`claim_next_queued` 原子抢占 + `requeue_interrupted` 重启恢复；Celery app 真实化（eager 可测）；compose worker 补 command/env；Makefile 增加 `make worker` |
| P1-4 | `a3a94ba` | replay 端点去掉共享 service mutation，provider 显式注入；SSE 支持 `?after=`/`Last-Event-ID` 断线恢复 + 心跳 + 终态关闭；CLI 新增 `replay` 子命令；API/CLI/SDK 三入口 Trace+Score 一致性 golden 测试 |
| P1-5 | `c4fe6ab` | 真实子进程 Worker 重启恢复测试；API rescore 不触发新模型调用测试 |

验收门核对：同 Run 重复执行无重复 CaseRun ✓；Worker 重启续跑 queued Run（子进程级）✓；cancel 阻断并记录原因 ✓；retry 创建子 Run ✓；rescore 不调模型 ✓；replay 三入口一致 ✓。

本轮验证：`uv run pytest -q`（92 passed）、`uv run ruff check .`、compileall、`pnpm --dir apps/web build`、`docker compose config`。另修复基线缺陷：pytest 此前未声明依赖、靠系统 Python 兜底，现入 dev 依赖组（`a44bb8f`）。

未完成（留给阶段 2+）：真实 Provider 调用（openai_compatible 真适配器、canonical 落盘、价格版本、live smoke）；Redis broker 上的 Celery 实测（当前 eager）；PostgreSQL repository。

## 阶段 2：第一个真实 Provider —— openai_compatible（2026-09-15）

| Task | Commit | 内容 |
|---|---|---|
| P2-1..P2-5 | `cf2a67a` | 真适配器：ModelRequest → `/chat/completions` → 统一 envelope（content/usage/metering/cost/canonical/error）；transport 增加 `post_json_detailed`（attempts/latency_ms/错误分类 auth/rate_limit/timeout/server/client/network/protocol，异常携带 outcome）；canonical request/response 落盘前经 redaction 脱敏，Authorization 头永不持久化；PriceTable 版本化（未知价格保持 None，成本带 price_table_version 快照）；strict 参数预检在构造与请求两级执行 |
| — | `61c0112` | run 链路接线：API 创建时预检 provider 配置（422 UNSUPPORTED_PARAMETER / PROVIDER_CONFIG_INVALID）；Worker 构造失败落 unsupported（零网络调用）；失败 Run 记录 error.class + 脱敏 evidence；评分对 envelope 结果取 content 比较 |
| P2-6 | `aeab491` | CLI 子命令化（doctor/run/replay/live-smoke）；`live-smoke` 显式真实调用（密钥仅从环境变量读取，缺失即拒绝），支持 `--report` JSON 与 `--record` markdown 记录（`docs/operations/live-smoke-log.md` 含模板） |

验收门核对：默认测试全部 fixture/replay（114 passed，零网络）✓；live smoke 仅显式启动（CI/测试永不触发）✓；API key 不出现在 envelope/Run/CaseRun/TraceEvent/SSE/CLI/记录（多个断言测试）✓；strict 不支持参数在付费调用前失败（构造级 + 请求级 + API 创建级三层，opener 调用数为 0 的测试）✓；成本计算使用请求发生时的 price table version（manifest 快照）✓。

剩余：live smoke 待操作者带真实 key 执行一次并记录（`docs/operations/live-smoke-log.md`）；Redis broker 上的 Celery 实测仍为 eager；OpenAI Responses / Anthropic Messages 仍为 openai_chat 别名 shim（按计划后续接入）。

## 阶段 3：PostgreSQL 生产存储（2026-09-15）

| Task | Commit | 内容 |
|---|---|---|
| P3-1 | `81fc492` | 版本化迁移管理：`migrations/versions/` 每个版本声明 revision/down_revision/up/down，runner 记录于 `schema_migrations`、线性链校验、支持 `--revert` 按步回退；`0001_initial` 建全量十表（ProviderConnection/ModelProfile/PriceTable/DatasetVersion/ScenarioVersion/Run/CaseRun/TraceEvent/Artifact/Score）；`python migrations/env.py` 可执行入口（兼容 `postgresql+asyncpg://` DSN） |
| P3-2+P3-3 | `58c69e0` | `PostgresRunStore`（psycopg 3）实装，与 SQLiteRunStore 同构四实体 repository：乐观原子抢占（`UPDATE ... WHERE status='queued'`）、advisory lock 内分配单调 seq、`(run_id, seq)`/`(run_id, case_id)` 唯一约束；删除恒抛错的旧 boundary 与 db.py 占位；`motte_storage.factory.create_run_store()` 按 `MOTTE_STORAGE`（sqlite/postgres）选择后端，API/Worker 经 `build_run_service` 只依赖工厂 |
| P3-5+P3-6 | `a51c04d` | 真实 PG 集成测试（`MOTTE_PG_DSN` 存在才跑）：空库 → migration → API create → worker execute → 查询 Run/Trace/Score，含回滚重放、Worker 崩溃恢复、抢占互斥；CI python job 增加 postgres service；compose 增加 healthcheck、migrate 一次性服务、api 服务与 `service_completed_successfully` 依赖 |

验收门核对：空库 → migration → API create → worker execute → 查询 Run/Trace/Score 已在本地 Docker 真实 PostgreSQL 上全链路通过（4 passed）；SQLite 路径行为不变（全量 116 passed）；迁移幂等（重复 apply 无操作）✓；`--revert` 回退后可重新应用 ✓。过程中修复一处 Trace 一致性缺陷：Worker 抢占路径此前缺少 `preparing` 事件，现幂等补齐，三入口事件序列一致。

已知限制：compose 中 migrate/api/worker 仍使用 `python:3.12-slim` 通用镜像（未内嵌依赖，真实镜像构建留待发布阶段）；PG 表中版本资源六表（provider_connections 等）schema 已就位、repository 待阶段 5 资源化 API 启用。

## 下一阶段

阶段 A 收尾后的任务安排见 [`superpowers/plans/2026-09-15-next-phase-task-plan.md`](superpowers/plans/2026-09-15-next-phase-task-plan.md)：阶段 0 可复现基线 → 阶段 1 Run 执行内核 → 阶段 2 真实 Provider → 阶段 3 PostgreSQL → 阶段 4 Sandbox/Agent/Harness → 阶段 5 产品层 → 阶段 6 质量门禁。
