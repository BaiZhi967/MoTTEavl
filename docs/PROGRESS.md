# MoTTEavl 开发进度

依据：`docs/superpowers/specs/2026-09-14-llm-agent-harness-evaluation-platform-design.md`、实现计划与运行安全 ADR。

约定：每个 Task 完成后运行对应 focused tests，提交一个 commit；每个里程碑完成后推送 `main`。真实 Provider/Harness 付费评测由操作者自行启动，本记录只标记仓库内验证与 replay 验证。

M6 收口（2026-09-21，分支 codex/m6-experiments-comparison-gates，worktree `.worktree/m6-experiments-comparison-gates`，基线 da077c2）：Experiment/Comparison/Statistics/Baseline/Gate 全链路实现并逐包验收——冻结协议与真值表；三级比较结论；版本化 metric registry 与 disposition 真值表；规则化 Gate 引擎（六类决策/冻结优先级/experimental fail-closed）；statistical_policy@1（Task 聚类 bootstrap/pass@k=0.7）；回归分类 8 类；ExperimentService 只编排既有 RunService/Dispatcher；Baseline v2 三存储+0014 迁移；API/CLI/Web 三端统一入口与 exporter v1（JSON/JUnit、退出码 0-6）。离线门禁全绿（与基线精确 node-id 差分仅 1 项迁移链登记，更新后通过）；CLI 实测退出码 0/1/2/5；浏览器实测实验/比较/基线（CAS 冲突）/门禁四页；DeepSeek V4.1 Flash 有界 live 验收（4 调用/316 tokens/4 对；重复 compare/gate/export 零新增调用）。已知边界：真实 PG 无环境（skipif 如实登记）、Judge 人工校准缺失（正式门禁 fail-closed）、GSM8K/DirectLlm 旧 compare 页本地自算为 M5 遗留债务。逐目标/逐验收证据见 [M6 验证记录](verification/M6.md)。

当前里程碑更新（2026-09-21）：M4 实现与修复已按用户要求快进合入并推送主干，合入点 `adf468a`，完整 Linux/PG CI 通过；真实模型验收仍为 `live_pending`。当前事实见 [M4 复核记录](verification/M4-completion-review-2026-09-21.md)。M5 可从此主干开始开发，使用 [详细计划](superpowers/plans/2026-09-21-m5-execution.md) 和 [开发提示词](prompts/M5-development-agent.md)。下列早期 Task 状态保留为历史记录。

M5 进行中（2026-09-21，分支 codex/m5-scenarios-skills-judges）：Workflow/Fixture/Skill/Judge 契约与版本资源、受限条件与旧 DSL 只读转换、有界场景引擎与受控进程真实停止、Scenario 装配器与目标 adapter、Skill 注入与权限交集、三臂对照计划、独立 Judge 契约与持久 ScoringJob、API/CLI/Web 资源链路均已落地并各有独立提交。**尚未完成**：T10 校准（软件在途；≥30 条真实人工样本在本环境不存在，保持 not_run）、WorkerLoop/RunService/API 的 ScoringJob 接线、Judge 路由、Skill 发布入口、scenario scoring 投影（SCENARIO_BACKEND_AVAILABLE 仍为 False）、真实 PostgreSQL 与完整门禁。逐目标/逐验收证据见 [M5 验证记录](verification/M5.md)。

| Task | 内容 | 状态 | Commit | 验证 |
|---|---|---|---|---|
| 1 | 工作区、依赖、Compose、CI | ✅ | `2f8e15c`（含基础提交） | `uv run pytest tests/test_workspace_health.py -q`（1 passed）；Compose config 通过 |
| 2 | Canonical contracts 与 schema | ✅ | `02f698c` | `uv run pytest tests/contract -q`（8 passed）；schema 导出 16 个 contract |
| 3 | Storage、migration、artifact | ✅ | `96d92ee` | `uv run pytest tests/storage -q`（4 passed）；ArtifactStore SHA-256/path safety |
| 4 | Provider runtime 与 model catalog | ⚠️ | `4aacb84` | OpenAI-compatible 已接入并修复真实 urllib timeout；Responses/Anthropic 仍为 shim，需 live smoke |
| 5 | Trace、replay、executor、foundation slice | ✅ | `5599e6c` | `uv run pytest tests/trace tests/runtime -q`（3 passed）；trace/redaction/replay/idempotent executor/scheduler 已验证 |
| 6 | Docker sandbox、Skill、ToolRegistry | ✅ | `1faeaa3` | `uv run pytest tests/sandbox -q`（4 passed）；默认 deny network、策略分离、tool modes |
| 7 | BuiltinReAct、Pi bridge | ✅ | `ff9fcb0` | `uv run pytest tests/runtime -q`（4 passed）；BuiltinReAct、Pi JSONL protocol |
| 8 | Claude/Codex Harness | ⚠️ | `1d67798` | Linux/WSL2 fake binary 路径可测；Windows 进程树/PTY 与 Codex app-server 仍待完成 |
| 9 | Evaluators、aggregation、gates、Inspect | ⚠️ | `1702957` | 基础 evaluator 骨架可测；Inspect 仍 dry-run，完整统计与回归门禁待实现 |
| 10 | API、Celery worker、CLI | ✅ | `c6daa75`（基于 `60f03e2`、`e1f9b49`） | `uv run pytest tests/api tests/cli -q`（3 passed）；`uv run python -m motte_cli --help` 可用 |
| 11 | React Web console | ✅ | `c5ecf86` | `pnpm --dir apps/web test`（web workspace healthy）；中文能力/限制/运行时间线/评分组件 |
| 12 | Replay integration、发布与运维文档 | ⚠️ | `49138cc`（基于 `e428fae`、`d9aa9a6`、`2afcfe0`） | 当前 Windows 全量：164 passed、8 skipped；Ruff/contracts mypy/compileall/pip-audit/Web build+test 通过；Docker build/up 尚待验证 |

状态说明：⏳ 未开始，🚧 开发中，✅ 已通过仓库验证，⚠️ 受外部依赖或未运行 live smoke 影响。

## Direct LLM 题库扩充 Phase 0-11（2026-09-19 至 2026-09-20）

- [x] Phase 0：冻结 GSM8K 不转换、Direct LLM v1/v2 并存、selected-only snapshot、来源 fail-closed 与 CLI 联网边界；新增来源治理文档和 7 份静态 SourceSpec 草案。
- [x] 修复 v1 `judged` 分母事实来源；overview、report 和 scoring pass 共用 aggregate accuracy，同时保留 v1 completion/attempt_rate 历史语义。
- [x] 数据集身份改为完整 `dataset_fingerprint`，兼容已发布且没有 fingerprint 字段的 v1 资源；伪造 fingerprint fail closed；GSM8K 继续使用其原有 cases hash 幂等语义。
- [x] dataset + scenario 在 InMemory、SQLite 和 PostgreSQL 使用单事务原子发布；增加并发同/异内容、12 路自动版本竞争和 scenario 写入故障回滚测试。
- [x] Direct CLI 拒绝 GSM8K/未知 suite scenario；Direct import API 使用 strict Pydantic request/response 并重新生成 OpenAPI/TypeScript schema。
- [x] Web 修复空可选导入字段、跨数据集 selection、返回 ids 模式与 Compare dataset/version/case-set 可比性门禁。
- [x] 仓库门禁：Ruff、contracts mypy、compileall、`629 passed / 11 skipped`、Web build、Web `108 passed`、Pi selftest 均通过。
- [ ] Compose config：当前机器没有 `docker` 可执行文件，`make check` 仅在最后一步因此失败；需在具备 Docker 的 CI/发布环境补证据。PostgreSQL 专项测试也因未设置 `MOTTE_PG_DSN` 本地跳过。
- [x] Phase 2-3：新增 strict Direct LLM v2 contract、`choice/numeric/json_equal/exact@1` scorer registry、selected-only snapshot、固定 profile、dataset fingerprint 和 selected/judged/attempted/coverage outcome 语义；未知显式版本 fail closed，v1 历史语义不变。
- [x] Phase 4：新增 strict SourceSpec、HTTPS/DNS/IP/redirect/大小/hash/cache/JSON/JSONL/CSV/Parquet 资源限制和 CLI-only `list/inspect/fetch/verify/convert/import/prepare`；restricted 只能通过受信 `ApprovalVerifier`，当前 CLI 无此 capability。
- [x] Phase 4 原子发布：dataset、scenario 和 append-only publication audit 在 InMemory/SQLite/PostgreSQL 使用单事务；`0003_resource_publications` 已纳入升级/回滚文档。
- [x] Phase 5-7：TruthfulQA Binary、MMLU-Pro 5-shot/zero-shot 和 1,000 题 MoTTE Core ZH 的确定性 adapter/generator、固定 profiles、provenance 与 quality checks 已实现；三者来源仍为 `pending`，不能用自动测试替代许可、ownership/privacy 双人复核或三模型校准。
- [x] Phase 8：typed DatasetSummary/Source/run/dry-run/publication API、OpenAPI 类型、Web profile、保守 token/费用上界、v2 outcome/coverage 展示和 restricted 默认隐藏已完成；服务端不提供隐式远程下载。
- [x] Phase 9-10：C-Eval/CMMLU 保持 `restricted` 且仅允许隔离实验转换；IFEval 使用独立 preview rule evaluator 和四项指标；LongBench v2 固定 503 题/6 域/full-verbatim context/50-200-503 profiles。全部保持非官方、不可默认发布，不并入普通 Direct accuracy。
- [x] Phase 11：fake-provider E2E 真实经过 prepare/run/score/aggregate，覆盖三个首批数据集转换器、四个 scorer、失败 outcome、provider gold 隔离、retry/rescore snapshot 复用；新增 v2 主操作指南和高级基准限制文档。
- [x] 最终 P1 加固：numeric 极端指数有界失败；selected snapshot 全内容 hash；provider 只接收 execution allowlist；显式版本不降级；data/code license 独立验证；publication 保存 portable receipt 并重算 hash/identity；generic v2 POST、bundle 关系、当前 SourceSpec/provenance、普通 retry 与 cache symlink/junction 全部 fail closed；Web v2 下钻读取 `selected_cases`，报告区分 judged failure 与 unjudged。
- [x] 最终离线门禁：Ruff、contracts mypy、compileall、`982 passed / 15 skipped`、Web production build、Web `130 passed` 和 Pi selftest 通过；OpenAPI/TypeScript 二次生成 SHA-256 不变，TypeScript `tsc --noEmit` 通过。`make check` 可执行部分全绿，仍只在本机缺少 Docker 的 Compose config 步骤失败。
- [ ] 外部发布门禁：当前 7 个来源为 5 个 `pending`、2 个 `restricted`，没有 stable approved 来源；有限付费 live smoke 未执行。Docker Compose 因本机缺少 `docker` 未验证，PostgreSQL E2E 因未设置 `MOTTE_PG_DSN` 为 `10 skipped`。以上均保持 fail closed，未伪造证据。

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

## 迁移管理切换到标准 Alembic（2026-09-15）

按决策弃用自研 runner，改用标准 Alembic（`alembic>=1.13` + `sqlalchemy>=2`）：根目录 `alembic.ini`、标准 `migrations/env.py`（DSN 从 MOTTE_PG_DSN / DATABASE_URL 读取，归一化为 `postgresql+psycopg://`）与 `script.py.mako`；`0001_initial` 转为标准 `upgrade()/downgrade()` 格式（DDL 不变）；`motte_storage.migrations` 变为 Alembic 薄封装（`upgrade/downgrade/current/revision_ids`），`create_postgres_run_store(migrate=True)` 与测试走编程入口，CLI 用 `uv run alembic upgrade head` / `downgrade -1`；版本登记表改为 `alembic_version`。已在本地真实 PostgreSQL 上验证：空库 upgrade → 幂等复跑 → downgrade → re-upgrade，以及全套 PG e2e（4 passed）与全量测试（116 passed）。

## 阶段 4：真实 Sandbox、Agent 与 Harness（2026-09-15）

| Task | 内容 |
|---|---|
| Docker Sandbox | 真实实现（docker SDK，client 可注入）：统一适配器契约 prepare/start/send/events/interrupt/collect/cleanup；command policy 与 network policy 分离（argv-only、无 shell、程序黑白名单；网络仅允许 "none"）；永不 privileged、drop ALL caps、no-new-privileges、非 root；只挂载受控 workspace（绝不挂 Docker socket）；环境变量仅白名单透传；CPU/内存/PID/磁盘(tmpfs)/输出字节/TTL 限制；cleanup 在成功/失败/超时/策略违规路径恒执行；workspace artifact 采集（name/size/sha256） |
| BuiltinReAct | 真实 agent 循环：模型经 `complete(ModelRequest)->envelope` 调用（阶段 2 provider 接口），文本 JSON 动作协议（tool/final），工具调用带 deny 与错误回灌（observation），步数预算，全程事件（step/tool/final/budget） |
| Pi bridge | `bridges/pi/bridge.mjs` 真实桥接进程（协议 v1：probe→version、prompt→started/output/finished、malformed→error；默认确定性 echo 模式，真实 Pi runtime 接入时替换实现）；Python 侧 `PiAgentRuntime` spawn 驱动 + 版本/协议握手；`node selftest.mjs` 自测并入 `pnpm -r test` |
| Claude CLI harness | `ProcessRunner` 实装（asyncio 子进程、超时杀进程组、退出码/stdout/stderr/耗时采集）；probe=`claude --version`；执行 `claude -p <prompt> --output-format json`；JSONL 解析带 parser version；terminal channel 保持 deny-by-default |
| Codex harness | 同一 `ProcessRunner` 通道：probe=`codex --version`、执行 `codex exec --json`，结果记录 transport=cli（app-server JSON-RPC 传输为后续形态） |

验证：全量 137 passed（离线零 Docker、零网络）；Docker Sandbox live 冒烟（`MOTTE_SANDBOX_LIVE=1 uv run pytest tests/sandbox/test_docker_sandbox_live.py`，操作者显式启动）在本机真实 Docker 上 4 passed：echo 完成即清理、默认无网络（出站全部失败）、TTL 超时杀伤 + 清理、workspace artifact 采集。

已知限制：磁盘限制经 tmpfs(/tmp) 实现而非根文件系统配额（overlay2 配额依赖存储驱动）；Codex app-server JSON-RPC 会话与 Pi 真实 runtime 待对应环境具备后接入；send 通道对 sandbox 显式 UnsupportedOperation。

## Harness 本地安装检测（2026-09-15）

`motte_harness.install`：`inspect_installation(binary)` 报告 installed/path/realpath/source（npm/homebrew/local/system/unknown，基于 realpath 启发式——npm 全局符号链接可还原到 node_modules）/version（semver 提取）/version_ok/error；`ClaudeHarness.inspect()` 与 `CodexHarness.inspect()` 在此之上给出 `runnable` 结论；`motte doctor` 输出 claude/codex/pi-bridge 三项安装情况（JSON 与文本两种形式，缺失 CLI 不是 doctor 失败）。本机验证：claude（npm, v2.1.170）、codex（npm, v0.154.0）、pi-bridge（node + bridge 就绪）均正确识别。

## 阶段 5：Evaluator 与 Web 产品层（2026-09-15）

| Task | Commit | 内容 |
|---|---|---|
| 资源存储 | `75a512c` | 版本化资源存储 `resource_store.py`：providers/models/datasets/scenarios/price_tables 五类（PG 表结构对应 0001 迁移，SQLite/InMemory/PG 三后端，键幂等 upsert） |
| 资源与目录 API | `4d42a3b` | 九组资源齐备：providers/models/datasets/scenarios CRUD（models 走 ModelProfile contract 校验；provider 拒绝明文凭据字段，只收 api_key_env）；agents/harnesses/skills 目录（harnesses 返回真实安装检测报告）；runs 增加列表与状态过滤；`GET /runs/{id}/report` 汇总（评分统计 + 成本合计 + price_table_versions 溯源） |
| Web 控制台 | 见下方提交 | 四页签：运行（创建/列表/取消/重试/重评分 + SSE 实时时间线 + 事件过滤 + 评分表 + 报告导出）、Provider 管理（只存密钥环境变量名）、模型管理、Agent/Harness 安装情况；简体中文 UI，任何页面不显示凭据；vitest + happy-dom + RTL 组件测试（7 个）替换原 echo 健康检查 |
| 类型生成 | 见下方提交 | `api/openapi.json`（20 个路径）+ `openapi-typescript` 生成 `src/api/schema.d.ts` 入库；`make openapi` 一键再生成；CI 增加两道漂移门禁（openapi.json diff + schema.d.ts diff）；web 的 TS 降至 5.x（openapi-typescript 尚不支持 TS7 原生编译器） |

验证：全量 152 passed + web 7 passed；真实服务器冒烟：provider 创建/凭据拒绝/harnesses 安装报告/运行+报告/SSE 全通过。已知限制：FastAPI 端点目前为 dict body，生成类型的响应面较弱，client 暂用手写接口（schema.d.ts 作为形状事实源 + 漂移门禁），待端点类型化后切换。

## 阶段 6：发布与质量门禁（2026-09-15）

此前已提前落地的门禁：replay-only CI（阶段 0）、PostgreSQL migration/全链路测试与 CI service（阶段 3）、OpenAPI diff 与 TypeScript 类型漂移门禁（阶段 5）、live smoke 记录模板（阶段 2）。本阶段补齐剩余项：

| 项 | 内容 |
|---|---|
| 依赖与镜像扫描 | `make audit` = pip-audit（`--skip-editable`，本地 workspace 包跳过）+ pnpm audit（high 级）；CI python job 增加 pip-audit 与 trivy config 扫描（compose IaC），web job 增加 pnpm audit。**门禁即抓即修**：接入当天发现 pytest 8.4.2 存在 PYSEC-2026-1845，已升级至 9.1.1 |
| backup/restore | `motte_storage.maintenance`：SQLite 在线备份（sqlite3 backup API，WAL 安全）+ artifacts 快照 + manifest（毫秒时间戳防同秒覆盖）；恢复取最新 manifest；CLI `motte backup / restore`；PG 侧 pg_dump/pg_restore runbook（docs/operations/backup-restore.md）；6 个测试覆盖往返/最新恢复/工件/dry-run |
| artifact cleanup | TTL 清理默认 dry-run（报告删除清单/保留数/可释放字节），`--apply` 才执行并清空目录；CLI `motte cleanup-artifacts --older-than-days N [--apply]` |
| rollback | docs/operations/rollback.md：先备份→回代码（推荐 git revert）→按步 alembic downgrade→重启验证（Worker 自动 recover）→灰度检查单 |
| 兼容矩阵 | docs/protocols/provider-compatibility.md 从 3 行占位重写为完整矩阵：Provider（openai_compatible 完整/openai_responses 与 anthropic 仍为 shim）、Agent、Harness（含本机实测版本）、Sandbox、工具链版本锁定 |
| mypy strict 分期 | 修复 contracts 包全部 6 个 strict 错误后纳入 CI（`uv run mypy packages/contracts`，dev 依赖加 mypy）；其余包待逐包清零后加入（pyproject 有注释说明分期策略） |

验证：全量 158 passed + web 7 passed；`make check` 与 `make audit` 全绿；mypy contracts strict 零错误。

## 路线图收尾状态（2026-09-15）

阶段 0–6 的基础产物已落库，但不再视为“稳定完成”：真实 Responses/Anthropic/Pi/Codex app-server、Evaluator/Inspect、Windows Harness 和生产运行验证仍在进行。2026-09-15 已修复真实 HTTP transport 的 timeout 调用、暂态网络退避（含 Retry-After）、嵌套凭据拒绝、direct-llm 缺 Provider 的创建期拒绝、Harness 消息 API、缺 Provider 安全终态和跨平台 workspace 路径校验；新增统一 Dockerfile，尚待 Docker build/up 验证。

Provider 层重构（2026-09-15，全量测试 207 passed）：适配器注册表（`motte_provider.registry`，Worker/API/CLI 分发单一来源）；资源关系定型为 ProviderConnection（怎么连）← ModelProfile（调什么，新增 `model` 字段）→ PriceTable（`(model_id, version)` 版本化，REST 端点已接）；manifest 引用解析下沉到 `motte_sdk.resolve`（API 与 CLI 共用，创建期展开快照）；凭据文件 `~/.motte/credentials.toml`（0600，CLI `credentials set/list/remove` 管理，环境变量降级为回退）。

真适配器落地（2026-09-16）：`anthropic_messages` 与 `openai_responses` 从 shim 变为完整适配器（离线 fixture 验证，零网络零费用）——Anthropic 的 x-api-key/anthropic-version 头、system 顶层、content blocks、tool_use/tool_result、必填 max_tokens、529 overloaded 重试；Responses 的 instructions/input、扁平 function 工具、response id、reasoning/cached usage、incomplete→length。公共流程抽取为 `BaseHTTPProvider`（envelope/计量/脱敏单一来源）；工具调用契约全链路打通（canonical = OpenAI 形状，`manifest.tools`/`case.tools` 注入，模型档案 `supports_tools=false` 时付费前拒绝）；错误体 `error.type` 精化分类；live-smoke 改为注册表驱动，新适配器自动可冒烟。流式 SSE、GET probe 与真实端点冒烟记录仍待操作者执行。

## 下一阶段

当前修复与接入顺序见 [`superpowers/plans/2026-09-15-next-phase-task-plan.md`](superpowers/plans/2026-09-15-next-phase-task-plan.md)：Direct LLM 可复核链路 → 生产镜像 → 真实协议与 Harness → Evaluator/Inspect → 并发、类型和发布质量。

## Direct LLM 评测接入（2026-09-20）

参考 GSM8K 评测，把 Direct LLM 从「前端空壳」补齐为完整纵向切片。接入前的实际状态：`/direct-llm` 操作页只把 `case_ids` 发给 `POST /api/v1/runs`，而 `manifest.cases`（题面 → prompt 的映射）没有任何来源，`CaseDrivenProvider.invoke` 必然 `KeyError`，因此该套件此前无法真正跑通。

| Task | 内容 |
| --- | --- |
| C1 契约 | `motte_contracts/direct_llm.py`：本地 JSONL 整份校验并冻结成不可变 `数据集名@版本`（记录用顶层 `eval` 键做套件判定）、三种评分器（`exact`/`contains`/`regex`，正则在导入期编译校验）、`no_expectation` 语义；抽出共享 `motte_contracts/selection.py`（运行级题目选择）与 `motte_contracts/suites.py`（套件分发的单一来源） |
| C2 评分 | `motte_eval/direct_llm.py`：判定分母 = `judged`（`correct + wrong_answer + call_failed`），无期望的题不进分母；`CONTINUE_ERROR_CLASSES` 提取为套件无关的 `motte_eval/execution.py` |
| C3 SDK | `motte_sdk/direct_llm.py`（导入落库、内置样例注册表、创建期展开、评分、`GET .../builtins` 数据源）与 `motte_sdk/suites.py`（manifest 展开 / 评分 / 聚合分发，`resolve.py`、`service.py`、报告链路不再写 `if suite == ...`） |
| C4 数据 | `datasets/direct-llm/`：`direct-llm-exact-answer`(8) / `direct-llm-classify`(8) / `direct-llm-json-extract`(7) 三份内置样例 + 目录 README；`MOTTE_BUILTIN_DATASET_DIR` 可覆盖，目录缺失时结构化报错 |
| C5 API | `GET /benchmarks/direct-llm`、`GET .../builtins`、`POST .../import`、`GET .../cases`、`POST .../runs`；`_build_report` 改为套件分发聚合，新增 `_direct_llm_accuracy`（judged 分母） |
| C6 CLI | `motte direct-llm builtins / list / import (--builtin\|--file) / run`，stdout 始终单行 JSON；显式传入的空值不再静默回落默认值 |
| C7 Web | Direct LLM 拆成操作 / 题目 / 过程 / 结果 / 对比五页（对齐 GSM8K），共享 `evalTypes/selection.ts` 与 `evalTypes/grid.ts`；注册表改用 `provenance.suite` 判别套件（旧运行回落 gsm8k） |
| C8 文档 | `docs/operations/direct-llm.md`、`datasets/direct-llm/README.md`、DESIGN.md 组件条款、eval type suite 设计文档第 15 节接入记录 |

验收门核对：契约 28 + 评分器 19 + SDK 16 + API 14 + CLI 11 + 运行时 7 个新测试全绿；Web 97 passed；`uv run ruff check .` 与 `uv run mypy packages/contracts` 零问题；`pnpm --dir apps/web build` 通过。既有 GSM8K 行为不变（`benchmark` 记录键、`is_benchmark`、评分口径、报告 summary 字段全部原样），仅新增 `benchmark_provenance.suite` 判别字段。

已记录的范围裁剪：Web 不做字符级 diff 高亮；题目页粘贴的 case id 由服务端在发起时校验（分页下客户端拿不到全量 id）；导入不引入 multipart（本地文件由浏览器读成文本放进 JSON body）。

## Worker Ctrl-C 处理补完（2026-09-20）

`README.md` 的 Worker 条目此前已写明「Ctrl-C 干净停止：无 traceback、stderr 最后一行 `worker_stopped`、退出码 130，中间态 Run 由下次启动回收」，但对应实现一直留在工作树里未提交（见上一节 D 组提交的说明），即文档描述了尚未落库的行为。本次补齐：

- `apps/worker/motte_worker/__main__.py`：把 `recover_interrupted()` 与执行循环包进同一个 `try`，捕获 `KeyboardInterrupt` → 输出 `worker_stopped{reason:"interrupted"}` 并返回 130（与 `apps/dev.py` 同一约定）；长轮询与 `--once` 两条路径共用该处理。
- `tests/runtime/test_worker_loop.py`：新增 4 个测试——空队列 `--once` 静默退出、`--quiet` 抑制进度日志、中断后退出码 130 且 Run 停在 `preparing` 无 case 落库（下次启动回收为 `queued` 并跑完）、`--once` 路径的中断一致性。

验证：`uv run pytest -q tests/runtime/test_worker_loop.py`（9 passed）、全量 `465+4` passed / 8 skipped、`uv run ruff check .` 与 `uv run mypy packages/contracts` 零问题。

## make dev 热重载监听范围收敛（2026-09-20）

现象：`make dev` 期间改动 `.worktree/` 里的文件也会让 API 进入重载，随后 API 长时间不再恢复。定位与修复：

- 根因：未安装 `watchfiles` 时 uvicorn 使用 `StatReload`，其 `iter_py_files()` 对每个 reload 目录递归 `rglob("*.py")`；不显式指定目录时该目录就是进程工作目录（仓库根），于是 `.worktree/**` 全部进入监听范围——本机 12,591 个 `.py` 中有 8,317 个来自 `.worktree/`，单次扫描约 12–25 秒。`--reload-include` / `--reload-exclude` 无法收敛范围：uvicorn 明确提示这两个选项在缺少 `watchfiles` 时不生效（`supervisors/statreload.py` 启动即打 warning）。
- 修改：`apps/dev.py` 新增 `RELOAD_DIRS = ("apps", "packages")` 与 `api_command()`，API 子进程改用显式 `--reload-dir` 启动，只监听它真正会 import 的目录（API 不引用 `apps/`、`packages/` 之外的仓库代码）。
- 范围可调：`MOTTE_DEV_RELOAD_DIRS` 覆盖白名单（默认 `apps packages`），`MOTTE_DEV_RELOAD_EXCLUDE` 追加排除项；两者以空白、逗号或 `;` 分隔。典型用法是恢复"整仓监听但排掉 worktree"：`MOTTE_DEV_RELOAD_DIRS="." MOTTE_DEV_RELOAD_EXCLUDE=".worktree" make dev`。
- 依赖：`uv add --dev watchfiles`（解析到 1.2.0）加入 dev 依赖组，uvicorn 因此从 `StatReload` 切到 `WatchFilesReload`，`--reload-exclude` 才真正生效；缺它时 uvicorn 只认 `--reload-dir`，`apps/dev.py` 会在启动 API 前自行打印提示（含安装命令与改用 `MOTTE_DEV_RELOAD_DIRS` 的建议），不再静默失效。
- 排除项按绝对路径传递：uvicorn 的 `FileFilter` 把排除目录与 watcher 上报的**绝对**事件路径做 `in path.parents` 比较，相对路径（如 `.worktree`）会静默匹配不到任何东西。实测对比：相对形式下改 `.worktree/m4/apps/dev.py` 仍被侦测到（detects=1），绝对形式下为 0；两种情形下改 `apps/api/app/main.py` 都为 1（阳性对照，证明 watcher 存活）。`reload_watch()` 因此把"存在且为目录"的排除项解析为绝对路径，glob 模式原样透传。
- 测试：`tests/test_dev.py::test_api_reload_scope_excludes_local_worktrees` 断言监听目录均存在、覆盖 `apps/` 与 `packages/`、且既不是仓库根也不包含 `.worktree/`；另有 `test_reload_scope_is_overridable_per_run`（两种分隔符、目录排除转绝对、glob 透传、默认值不变）、`test_directory_excludes_are_made_absolute`、`test_reload_exclude_defaults_to_nothing`、`test_excludes_warn_that_watchfiles_is_required`。

验证（本机实测）：启动后 uvicorn 打印 `Will watch for changes in these directories: ['…\apps', '…\packages']`；改写 `.worktree/m4/apps/dev.py` 后 30 秒内 `detected changes` 计数保持 0、`/health` 持续 200、supervisor 存活；改写 `apps/api/app/main.py` 后 5 秒内即被侦测（旧配置下最坏要等一整轮扫描）。单次扫描从 12,591 个文件约 12–25 秒降到 189 个文件约 0.46 秒。装上 watchfiles 后默认配置复测：reloader 为 `WatchFiles`、无"选项无效"提示、`.worktree` 改动 0 次侦测而 `apps/` 改动 1 次；黑名单组合（`DIRS="."` + `EXCLUDE=".worktree"`）在相对与绝对两种写法下均为 0/1 的同一结果。`uv lock --check`、`make lint`（ruff + contracts mypy + compileall）、`uv run pytest -q tests/test_dev.py`（27 passed）全部通过；CI 用 `uv sync`（含 dev 组）安装，故 CI 与本机一致。

遗留问题（与 `.worktree` 无关，本次未改）：Windows 上 uvicorn 0.53 的 `BaseReload.restart()` 用 `os.kill(child_pid, signal.CTRL_C_EVENT)` 通知子进程退出后 `process.join()` 等待。本机实测该 Ctrl+C 送不到目标子进程（同进程组与 `CREATE_NEW_PROCESS_GROUP` 两种布局都验证过，子进程均存活），于是**任何**被侦测到的改动都会停在 `Reloading...`：旧进程继续用旧代码返回 200，reloader 既不启动新 server 也不再扫描。关闭 `.worktree` 监听消除了绝大部分误触发，但正常改 `apps/` 代码仍会命中该缺陷，需要在 reload 策略上单独决策（升级 uvicorn / 让 supervisor 自己重启 API / 关闭 `--reload`）。

## 平台完整性加固（2026-09-18）

本轮把 Run 执行、审计、资源快照、插件、API、Worker、Pi bridge 与 Web 合同收敛为可恢复的持久化边界：

- Run/CaseAttempt 使用 revision/status CAS；provider 调用后的持久化不确定性进入 `needs_review`，安全 replay 可恢复；取消请求在 open attempt 结束后统一 settling。
- ScoringPass 与 score set 原子落盘，包含终态、终端事件和完整 benchmark aggregate；历史报告只读取选定 pass 的快照，不在 GET 时重跑可变插件。
- Provider/Model generation 更新与 create-if-absent 具备并发保护；已发布 ModelProfile、数据集、场景和价格表保持不可变；PostgreSQL 更新/删除使用行锁。
- Dispatcher、WorkerLoop、Celery 和 CLI 共用锁与 claim 语义；重复投递、进程恢复和 provider 错误均幂等处理。
- Replay fixture 有严格的 `ReplayCase` 合同，profile-stale retry 保留资源引用与随机选择快照；BenchmarkPlugin 的准备、评分和 aggregate 错误均映射为结构化配置/终态错误。
- SSE 使用严格 `TraceEvent` envelope 并保留旧事件读取兼容；Web 生成类型、nullable Score、retry child 跟踪、模型身份聚合和审计快照已同步。
- Pi v1 bridge 支持旧 peer 可选字段、LF/CRLF framing、有界 stdout/stderr、进程树清理和超时回收；不宣称未实现的实时能力。

验证：`uv run pytest -q -m "not live"`（620 passed，10 skipped）；`pnpm --dir apps/web test`（101 passed）；`pnpm --dir apps/web build`；`pnpm --dir apps/web exec tsc --noEmit`；`uv run pytest -q tests/protocol/test_pi_bridge_hardening.py`（14 passed）；OpenAPI live schema 与 `api/openapi.json` 一致。真实 Provider、Celery broker、PostgreSQL 服务和付费 live smoke 仍需由操作者在目标环境显式执行。


## M1：原生 Agent 与通用评分（2026-09-19，分支 codex/m1-native-agent-evaluation）

- [x] M1-T01 契约：FrozenObservation / MetricResult / EvaluatorSpec / InvocationRecord；
  Score 多指标复合键（trial_id 预留 M3）；canonical JSON 拒绝 NaN/Infinity；旧 Observation 读取兼容。
- [x] M1-T02 确定性评分器 agent-deterministic@1：exact/contains/regex（子进程可终止）/
  json-schema/file-exists/file-content/exit-code/tool-call/no-forbidden-write；
  缺证据与异常不映射成通过；逐指标隔离。
- [x] M1-T03 多指标 ScoringPass：三 store 复合键 + migration 0004（SQLite 原地升级）；
  rescore 新 pass、旧 pass 不变、GET 零副作用。
- [x] M1-T04 builtin-agent@1 后端：发布 ModelProfile 快照构造请求、双显式模式、
  native 创建期拒绝、Case 级 workspace/历史隔离。
- [x] M1-T05 消息/工具循环：完整 assistant/tool 历史、call_id 去重、参数 schema、
  预算（enforced/observed/unknown）与具体停止原因。
- [x] M1-T06 受控 workspace：相对路径/symlink/TOCTOU/设备文件/配额；快照与残留报告。
- [x] M1-T07 调用日志（migration 0005）、取消、崩溃恢复 needs_review、second-chance 排除。
- [x] M1-T08 冻结采集：evidence_hash、coverage 降级、值形状脱敏（事件/SSE/摘要）。
- [x] M1-T09 用户链路：/api/v1/agent-tasks + CLI agent-tasks + Web Agent 套件
  （操作/监控/结果/并列阅读，历史 pass 切换、取消、retry 子 Run）。
- [x] M1-T10 集成：本地假 HTTP + 真实 API/Worker/SQLite 三组代表任务闭环；
  真实 Docker（一次性 PG 容器 + alembic 0001→0005）19 passed；
  Direct LLM v1 / Replay 回归不变。
- [x] review 三轮修复：fcf4508 18 项 + a8629d5 10 项全部关闭（含单次调用期限
  可靠结算、workspace 目录链归属校验、公共响应全面脱敏、schema 可终止校验、
  禁写"写入后恢复"轨迹判定）；反例测试见
  `tests/runtime/test_review_round{2,3}_fixes.py`。
- [ ] live（真实 subject 模型）：待操作者授权与预算；M1-Supported 未满足。

验证详情与逐包命令见 [docs/verification/M1.md](verification/M1.md)。
已知边界：workspace 为宿主本地目录 + 策略护栏（非容器隔离）；token 流式不在 M1。
