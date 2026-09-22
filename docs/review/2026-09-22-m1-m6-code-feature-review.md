# MoTTEavl M1–M6 代码与功能开发审查报告

- 审查日期：2026-09-22（UTC+08:00）
- 审查基线：`main` @ `9940821`（M6 合入并推送后的主干）
- 审查范围：M1–M6 全部目标（G）/工作包（T）/验收场景（A）的"规划 vs 代码"对照、代码质量分析、质量门禁实测
- **M7 处理**：M7 当前为 `planned / not_run`（分支 `codex/m7-sdk-migration-release` 相对 main 零提交，仅有规划文档），按审查约定**不审 M7 代码**；M7 所需测试点见配套文档《M1–M7 详细测试点文档》第 7 部分
- 审查方法：7 路并行深读（每阶段一路：路线图文档 + 验证记录 + 多轮 review 记录 + 实际代码逐包对照），另有总路线/M7 规划提炼一路；关键论断经主审抽查复核（AGENT_CATALOG 文案、`SCENARIO_BACKEND_AVAILABLE`、`trusted.py` POSIX fd、Judge 页路径失配、统计模块消费者、旧 compare 前端自算，共 6 项全部属实）

---

## 1. 总体结论

**M1–M6 的 71 个核心工作包在代码层全部有实质实现，与 137 个阶段目标、119 个验收场景的对照未发现"虚报完成"**：各阶段验证记录（`docs/verification/M1–M6.md`）的"部分/限制/not_run"标注与代码事实逐一吻合，多轮 review 修复记录（M1 三轮 28 项、M2 四轮 44 项、M3 三轮 46 项、M5 F01–F20 + 验收 F-01~F-11）抽查均能在当前代码定位到实现。

**尚未完成的部分集中在三类**（均已在文档中如实登记，非虚报）：

1. **外部 live 证据**：M1 真实模型 Agent、M2 真实模型/full Profile、M4 真实 Pi/Claude/Codex 小任务、M5 Judge ≥30 条人工校准样本，均为 `not_run / live_pending / blocked`——代码路径存在但从未在真实付费环境执行。
2. **"实现未接线"的半成品**：M6 统计模块（pass@k / Task 聚类 bootstrap / 区间估计）零生产消费者；M5 三臂对照、人工修订、校准报告只有库层入口，无 API/CLI/Web 消费者；M6 分组 baseline 未按 cell 条件匹配。
3. **平台性缺口**：`packages/benchmark-runtime/motte_benchmark/trusted.py:41` 的 fd 锚定读取是 POSIX-only，Windows 原生无法运行 M2/M3 采集链路（本机全量套件 236 failed 全属此族）；主干 CI 仍有 3 项 M5 遗留失败。

### 1.1 阶段完成度总览

| 阶段 | 规模（G/T/A） | 代码实现 | 离线验证 | live/外部验证 | 阶段结论 |
|---|---|---|---|---|---|
| M1 原生 Agent 与通用评分 | 18/10/15 | T01–T10 全部（Core 层） | ✅ 抽样复跑通过 | ❌ 真实模型 not_run | implementation_complete（live 待授权） |
| M2 LLM Benchmark 与 C-Eval | 17/11/15 | T01–T11 全部 | ✅（POSIX） | ⚠️ 真实 Runner 已本地确定性验证；真实模型/官方数据 blocked | implementation_complete（验收未收口） |
| M3 Harbor 与 Terminal-Bench | 18/10/15 | T01–T10 全部 | ✅（POSIX） | ⚠️ 层 2 Docker oracle 仅开发方 macOS；层 3 真实模型 blocked | implementation_complete |
| M4 Pi 与外部 Harness | 18/11/15 | T01–T08、T10、T11；T09 为 live 项 | ✅ | ❌ T09 live_pending（文档如实） | implementation_complete / live_pending |
| M5 场景、Skill 与 Judge | 22/12/18 | T01–T12 全部有实质实现 | ✅ 定向 ~366 项复跑全绿 | ❌ T10 真实人工校准 not_run（Judge 保持 experimental） | implementation_complete（G18 开放） |
| M6 实验、比较与门禁 | 21/11/20 | 全链路（含统计库） | ✅ 193 项精确吻合 | ✅ DeepSeek V4.1 Flash 有界 live（4 调用） | offline_verified + 有界 live |
| M7 SDK、迁移与发布 | 23/12/21 | **无代码（planned/not_run）** | — | — | 未开始（不在本次审查范围） |

> 状态词沿用仓库四维约定（规划/实现、协议、执行、验证）；"implementation_complete"指代码与公共消费者完成，不代表 stable。

### 1.2 质量门禁实测（本次审查环境：Windows 10 / Python 3.12 / Node 24）

| 门禁 | 结果 | 备注 |
|---|---|---|
| `uv run pytest -q -m "not live"` | **2465 passed / 236 failed / 79 skipped**（23:45） | 失败全部属于已登记的 Windows 环境失败族（见 §5.1），与 M6 验证记录登记的 232/233 族一致；**CI（Linux + 真实 PG）是权威全量门禁** |
| `uv run ruff check .` | ✅ All checks passed | |
| `uv run mypy packages/contracts` | ✅ 32 文件零错误 | |
| `pnpm --dir apps/web test` | ✅ 283 passed（21 文件） | |
| `pnpm --dir apps/web build` | ✅ exit 0 | 有 chunk >500 kB 警告（P3） |
| `make check`（compose config 步） | 未执行 | 本机无 docker，与历史记录一致 |

---

## 2. 逐阶段审查

### 2.1 M1：原生 Agent 与通用评分

**实现状态：T01–T10 全部实现（Core 层）；live（真实 subject 模型）not_run，M1-Supported 未满足（已如实登记）。**

| 工作包 | 状态 | 关键证据 |
|---|---|---|
| T01 契约 | ✅ | `packages/contracts/motte_contracts/evaluation.py:66-447`（FrozenObservation/MetricResult/InvocationRecord 全字段+不变量；canonical JSON 拒绝 NaN/Infinity；旧 Observation 读取兼容） |
| T02 九类确定性评分器 | ✅ | `packages/evaluators/motte_eval/observation.py:388-644`（exact/contains/regex/json-schema/file-exists/file-content/exit-code/tool-call/no-forbidden-write；regex/schema 子进程可终止；缺证据不映射通过） |
| T03 多指标 ScoringPass | ✅ | migration `0004_multi_metric_score_sets.py`；评分入口 `packages/sdk-python/motte_sdk/agent_tasks.py:261-333`（纯读冻结证据，零模型调用） |
| T04 AgentBackend | ✅ | `execution_backends.py:619-628` 注册 builtin-agent@1；native 创建期拒绝 `agent_tasks.py:133-144`（付费前） |
| T05 消息/工具循环 | ✅ | `packages/agent-runtime/motte_agent/builtin_react.py:262-391`（双模式完整 assistant/tool 历史；重复 call_id 拒绝 `:512-520`） |
| T06 受控 workspace | ✅ | `packages/sandbox/motte_sandbox/workspace.py`（路径校验/fd 链 O_NOFOLLOW/读写配额/cleanup 前归属复核；Windows 走便携分支） |
| T07 调用日志/取消/恢复 | ✅ | `packages/storage/motte_storage/invocations.py:28-32`（prepared→dispatching→settled + revision CAS）；migration 0005；quarantine→needs_review `service.py:1986-2019` |
| T08 冻结采集 | ✅ | `agent_backend.py:542-635`（evidence_hash、coverage 降级、事件上限 500 + 截断标记） |
| T09 API/CLI/UI | ✅ | `apps/api/app/main.py:4261-4560`；CLI `main.py:1440-1522`；Web `AgentPages.tsx`（四页，15 用例） |
| T10 集成证据 | ✅（Core） | `tests/integration/test_native_agent_slice.py`（假 HTTP + 真实 API/WorkerLoop/SQLite 三组任务闭环 + 旧套件回归）；真实 Docker PG 19 passed（历史记录） |

**验证声称 vs 代码事实差异**：
- `apps/api/app/main.py:65-80` 的 `AGENT_CATALOG` 仍写 builtin-react"尚未接入 ExecutionBackend"、`execution_ready: False`，与 G01 已接线的事实矛盾（五轮 review 均未发现；本次主审复核属实）。
- G05 口径弱于规划：完整 assistant/tool 对话历史不落持久证据（`agent_backend.py:390-407` 只存元数据/计数），监控页无法回看模型实际输出。
- 验证记录基于 macOS，未登记 Windows 差异（symlink 测试因特权跳过、便携链防护弱于 fd 链）。

### 2.2 M2：LLM Benchmark 与 C-Eval

**实现状态：T01–T11 代码层全部落地；live 分层验收按文档如实保留未收口（真实模型/full Profile/官方数据获取 blocked）。**

| 工作包 | 状态 | 关键证据 |
|---|---|---|
| T01 Job 协议 | ✅ | `motte_contracts/external_job.py:21-210`（JobSpec/Handle/ImportBatch/Adapter，占位值拒绝）；job 分派一次 `dispatcher.py:86-95` |
| T02 受控句柄 | ✅ | `packages/benchmark-runtime/motte_benchmark/process.py`（launch_token argv 注入、PID+token 双因子所有权、TERM→宽限→KILL、可信完成标记） |
| T03 持久化/幂等 | ✅ | `motte_storage/external_jobs.py:300-377`（幂等键 `(job_id, source_record_key, parser_version)`、冲突进账本、终态 CAS）；migration 0006 |
| T04 数据准备 | ✅（获取器降级） | `motte_sdk/benchmark_catalog.py:254-380`（checksum/许可/approval 治理、逐行 has_gold）；**官方受控下载获取器未实现**，需人工提供文件+受信核验器（已登记 blocked） |
| T05 Runner 配置/预检 | ✅ | `opencompass/config.py`（凭据仅 `env:` 引用、参数 allowlist、retry 三分）；上下文静态预检 `context_preflight.py:97-157` |
| T06 Parser 迁移 | ✅ | `opencompass/parser.py`（PARSER_VERSION @2、来源提交、52 学科断言、纯内存解析）；parity 文档 + fixture + `test_ceval_parser_parity.py` |
| T07 公共入口 | ✅ | adapter `opencompass/adapter.py:40-358`（证据白名单、fd 锚定读取、冻结 bundle 映射）；API/CLI/Worker 共用 `var/runner/adapters.json` 受控注册 |
| T08 Web 五页 | ✅ | `ExternalBenchmarkPages.tsx`（ceval/cmmlu 共用工厂：请求/提交代号、native/diagnostic 双栏、not_attempted 不消失、gate-blocked 逐规则） |
| T09 M6-Lite | ✅ | `motte_eval/comparison.py`/`coverage.py`/`gates.py`（与 M6 同一实现，无第二套 Gate 引擎） |
| T10 全链路验收 | ⚠️ | 假 Runner E2E + e2e 样例（数值与 roadmap fixture 逐项一致）；真实固定 Runner 0.4.2 + 本地确定性端点已验证（12 passed）**但 `M2.md:505-516` 收尾清单未回填仍写 not_run（文档漂移）** |
| T11 CMMLU | ✅ | `opencompass/cmmlu.py`（67 学科、独立 Profile 身份）；独立链路测试（含"ceval 语义不放松"反例） |

**主要差异/风险**：
- **Windows 平台不可用（P1）**：`trusted.py:41` fd 目录打开在 Windows 抛 PermissionError → M2 采集链路本机全部失败（实测 8+2 个测试失败同因）；文档只声明了"跨会话身份核验不可用"，声明不完整。
- G12（usage/cost 进证据）部分满足：runner-native 路径 `observation_gaps=["request-identity","usage-and-cost"]`，费用记 unknown 不补填（规划允许的如实边界）。
- 冻结预算偏紧：单文件 8 MiB/总量 64 MiB，full Profile（1346 题）未经真实数据校验。

### 2.3 M3：Harbor 与 Terminal-Bench

**实现状态：T01–T10 代码层全部落地并合入主干（`4e6d146` + 三轮修复均 reachable from main）；层 3 真实模型 blocked。**

| 工作包 | 状态 | 关键证据 |
|---|---|---|
| T01 TaskIdentity | ✅ | `motte_contracts/trial.py:87-139`（compute_task_key 含 source_id+revision+规范化路径+内容 hash；反序列化重算比对）；`harbor/tasks.py:81-188`（拒越界/拒未钉住 revision/内容 hash） |
| T02 Trial 存储 | ✅ | `motte_storage/trials.py`（identical/conflict/conflict 保留先写、占位可被真实证据替换）；PG `pg_audit_store.py:1029-1138`（ON CONFLICT + FOR UPDATE）；migration 0008（降级有数据即拒绝） |
| T03 原生配置 | ✅ | `harbor/config.py:308-458`（execution_option_plan 逐字段 map-or-reject；原生 retry 整体拒绝；凭据仅 env: 引用） |
| T04 Parser | ✅ | `harbor/parser.py:202-420`（reward 四态；**Verifier 异常先于 reward 判定** `:365-391`；双来源矛盾=协议错误；纯函数只吃 `dict[str,bytes]`） |
| T05 环境 preflight | ✅ | `harbor/environment.py:83-256`（fail-closed、model_calls=0）；Compose 深检 `harbor/compose.py`（932 行：宿主来源含 `$` 一律拒、env_file 仅字面 KEY=value） |
| T06 Harbor adapter | ✅ | `harbor/adapter.py:169-228`（prepare 零执行 + 物化不可变任务副本）；导入键 `task_key#repeat_index` |
| T07 取消/清理 | ✅ | `harbor/containers.py:128-165`（冲突先行判定）；恢复只观察不重启（有专项测试） |
| T08 评分 | ✅ | `motte_eval/harbor.py`（质量/覆盖分母分开、reward=0 有效失败、计划内零产出 Task 也有行、成本 unknown 保持 null） |
| T09 API/CLI/Web | ✅ | API `main.py:3159-3462`；Web 五页 `TerminalBenchPages.tsx`（本机 32 passed）；工件文本默认脱敏、二进制默认拒 |
| T10 校准与交付 | ⚠️ | 文档/脚本/脱敏 fixture 齐备；层 2 真实 Docker oracle 仅开发方 macOS 本机执行过，层 3 blocked |

**主要差异/风险**：
- **Windows 原生全链路不可运行（P1）**：同 M2 `trusted.py` 问题；sandbox 侧同款问题已在 `7450009` 修复，benchmark-runtime 未修（实测 identity/config/preflight 13 failed、compose/frozen 61 failed、job-fixture 16 failed，全部同因）。
- M3 完成时点曾漏一个真实缺陷：既有部署 SQLite 库缺 9 列（含 trials.repeat_index）导致任何 Run 无法结算——所有测试都新建库故未暴露，后被 2026-09-21 产品级验收 F-01 抓出，`dfc2bf0` 修复。属"完成声明遗漏、已被后续验收闭合"。
- 小型清理债：`adapter.py` `_location_payload` 重复定义（前者死代码）、`parser.py` json reward 重复解析、`config.py:95` `SUPPORTED_AGENTS` 死常量、mean-success `>=0.5` 阈值硬编码（`motte_eval/harbor.py:159`）。

### 2.4 M4：Pi 与外部 Harness

**实现状态：T01–T08、T10、T11 离线证据层面全部实现且质量高；T09（live）live_pending，文档如实。M4 合入点 `adf468a` 后相关路径零改动。**

| 工作包 | 状态 | 关键证据 |
|---|---|---|
| T01 Runtime 契约 | ✅ | `motte_contracts/runtime.py:141-244`（RuntimeDefinition config_schema allowlist/model_control/tool_control/interactive 一致性；RuntimeVersion 仅 published 不可变） |
| T02 版本兼容锁定 | ✅ | `motte_harness/compatibility.py:105-286`（三态分层、execution_ready 永不由 --version 推出、零成本真实探测）；版本门 spawn 前 --version |
| T03 真 Pi bridge | ✅ | `bridges/pi/bridge.mjs`（协议 v2、stderr 64KB 上限脱敏）；`session.mjs`（真 Agent + faux/http 双传输显式二分、API key 只经 env 名、max_steps/max_tool_calls 执行前阻断） |
| T04 Pi 接平台 Run | ✅ | `motte_sdk/pi_runtime.py:301-329`（每 CaseAttempt 全新 session/workspace）；停止未确认即 `RUNTIME_STOP_UNCONFIRMED` 隔离并保留工作区 `:446-464` |
| T05 公共 supervisor | ✅ | `motte_harness/supervisor.py:255-291`（**Windows CREATE_SUSPENDED→Job Object→NtResumeProcess 消除 PID 竞态**）；双管道并行有界读取；PID 复用双因子核对 |
| T06 Claude batch | ✅ | `motte_harness/claude.py`（`-p --output-format json` pinned）；parser 未知 schema→insufficient 不假成功；非零退出/截断让终态离开 final |
| T07 Codex batch | ✅ | `motte_harness/codex.py`（exec --json）；parser 官方顶层 type 形态、cost/model 恒 unknown 不填 0 |
| T08 用户工作区 | ✅ | API `main.py:1593-1755`（目录/publish/readiness/messages/commands/inspect）；CLI；Web `HarnessPages.tsx` + `RuntimeCommands.tsx`（六态投递语义） |
| T09 live 小任务 | ❌ live_pending | 仅离线 fixture/fake binary 证据；`m4-live-acceptance.md` 是方案非通过记录（如实） |
| T10 RunCommand 消费 | ✅ | `codex_app_server_runtime.py:284-474`（真实 JSON-RPC 消费者：claim CAS→steer/interrupt/approval respond；原提案漂移/已完成的原批准拒绝）；Worker 接线在位 |
| T11 Inspect 导入 | ✅ | `parsers/inspect.py`（官方 EvalLog v2、无执行路径）；同身份异内容 IMPORT_IDENTITY_CONFLICT；真实原生收据（inspect-ai 0.3.266 日志 + SHA256） |

**主要差异/风险**：
- **3 例 flaky 测试（P1）**：`test_appserver_worker.py` approve/reject 两用例与 `test_supervisor_safety.py` batch-stdin EOF 用例在混合运行时失败、单独运行通过——CI Linux 单 job 掩盖此隔离性缺口。
- **motte-settings.json 污染 Case 产物（P2）**：`cli_runtime.py:466-467` 在 before 快照之后把空 settings 写进 workspace，被当 agent 产物冻结，稀释"实际修改"语义。
- compatibility.json 的 batch_invocation 未登记实际附加的 6 个隔离 flag（`cli_runtime.py:474,485`），"唯一事实源"承诺打折扣。
- completion review 声称"移除过期兼容版本常量"与代码不符（`runtime_backends.py:445-467` HISTORICAL_APP_SERVER_V1 仍在并随 publish 落库 @1 retired 记录）。

### 2.5 M5：场景、Skill 与 Judge

**实现状态：T01–T12 代码层全部有实质实现；定向测试约 366 项在 HEAD 复跑全绿；G18（≥30 条真实人工校准）not_run，Judge 保持 experimental。任务背景给出的 5 个历史未完成项，4 个已由 R6–R9 与验收修复批次闭合：**

| 历史未完成项 | 当前状态 | 证据 |
|---|---|---|
| WorkerLoop/RunService/API 的 ScoringJob 接线 | ✅ 已闭合 | `apps/worker/motte_worker/runtime.py:54-61,203-264`（先 Judge 后 Run 领取、恢复同锁）；API 5 条路由 `main.py:967-1035` |
| Judge 路由 | ✅ 已闭合（限制：single-mode only，pairwise 留库内） | `main.py:957-995` |
| Skill 发布入口 | ✅ 已闭合 | `POST /api/v1/skills/versions` 201 + CLI `skill publish`（`main.py:2039-2099`） |
| scenario scoring 投影 / SCENARIO_BACKEND_AVAILABLE | ✅ 已闭合 | `scenario_backend.py:423` `= True`；`scenario_scores()` 挂入 RunService（`service.py:1365-1367`）——**主审复核属实** |
| T10 校准 | ⚠️ 代码已落地、真实数据缺 | `calibration.py`（1109 行：校准集/报告/资格/人工修订 CAS）；无 ≥30 真实样本，无 `judge calibrate` 公共入口 |

**逐包要点**：T01 契约与旧 DSL 只读转换（`conversion.py` ast 白名单、恶意表达式具名拒绝）✅；T02 Fixture（PrivateTruth 私有根、递归隐藏投影）✅；T03 引擎（受控进程 Target 复用 M4 SupervisedProcess、deadline 贯穿+真实中断）✅；T04 七类过程评分器 ✅；T05 公共 Run ✅；T06 Skill 版本（导入安全：PATH_TRAVERSAL/SYMLINK_RESOURCE/ZIP_BOMB）✅；T07 注入（权限交集+执行期唯一网关、mock 缺失具名拒绝不回退 real）✅；T08 三臂对照——**库层实现（`skill_ablation.py` + 11 项集成测试），无 API/CLI/Web 消费者** ⚠️；T09 Judge（唯一编译路径、唯一调用计划、逐调用原子额度预留、崩溃窗口不重发）✅；T11 用户入口大部分（Web Judge 页 F-07 故意保留失配）⚠️；T12 七类业务边界集成测试 33 项全绿 ✅。

**主要差异/风险**：
- **Web Judge 页 6 条 API 路径全部失配（P1，F-07 故意保留）**：`apps/web/src/api/client.ts:1526-1553` 调用的 `GET /api/v1/judges`、`/api/v1/judges/jobs` 等 6 条路径服务端不存在（只有 `POST /api/v1/judges`），页面永久显示"能力不可用"——**主审复核属实**。openapi-check 门禁不校验手写路径字符串，此类漂移可再次穿透。
- **主干 CI 3 项 M5 遗留失败未修（P1）**：`test_fixture_lifecycle`（Linux，`'portable' != 'sandbox'`）、`test_scoring_jobs[PG]`（`subject invocation owner must match the run`——直接命中 Judge 归属语义，需优先排查是否真实缺陷）、`test_trials_downgrade_guard[PG]`。
- `docs/verification/M5.md` 状态矩阵过时（仍写 in_progress/实现中），与 HEAD 代码不符；事实来源是 M5-review 与验收报告。
- 大文件集中：`judge.py` 1908 行、`scoring_jobs.py` 1870 行、`executor.py` 1285 行。
- GSM8K/DirectLlm 旧 compare 页前端自算 accuracy（`Gsm8kCompare.tsx:25`）——M6-G19 三端一致的例外（已登记债务）——**主审复核属实**。

### 2.6 M6：实验、比较与门禁

**实现状态：全链路实现并逐包验收（offline_verified + DeepSeek V4.1 Flash 有界 live）；193 项核心测试与本次实测精确吻合，验证记录诚实度高于常见水准。**

| 工作包 | 状态 | 关键证据 |
|---|---|---|
| T01 比较身份/政策 | ✅ | `motte_contracts/comparison.py:24-50`（24 因子白名单、三级结论）；`motte_eval/comparison.py:685-787`（结构/指标级原因分离） |
| T02 分母/覆盖/费用 | ✅ | `motte_contracts/metrics.py:105-156`（8 指标/6 分母注册表）；6 disposition 真值表 + 契约层不变量 |
| T03 Lite Gate+Baseline | ✅ | `motte_eval/gates.py:83-163`（lite）`:317-652`（Full）；三存储 + migration 0014（7 表，降级有数据即拒绝） |
| T04 Spec/预览 | ⚠️ 部分 | preview 零创建 ✅；但 preview 不校验资源值域，**多 cell 矩阵非法 cell 只落 failed、其余 cell 已排队**（`experiments.py:342-376` 单 cell 异常吞掉继续），与规划"不先排队一部分再发现错误"有偏差 |
| T05 Cell 分配/恢复 | ✅ | 状态机 + deterministic_run_id（崩溃/并发同 run_id）；request_key 注册表为进程内 dict（单进程假设，已登记） |
| T06 Trial 统计 | ⚠️ **实现未接线** | `motte_eval/statistics.py`（pass@k 实测 n=5,c=2,k=2→0.7；Task 聚类 bootstrap seed 确定性）公式正确，但 **packages/apps 无任何生产消费者**（grep 仅契约注册表与测试引用）——主审复核属实 |
| T07 分组 Baseline/回归 | ⚠️ 部分 | 8 类回归分类 ✅、baseline 指针 CAS+审计 ✅；**分组 entries 求值未按 cell 条件匹配**（`comparisons.py:1257-1259` 只取 cell_key=None 或第一个 entry） |
| T08 完整 Gate | ✅ | 六类决策+优先级+退出码；experimental_evidence 正式 fail-closed（Judge 未校准边界）；diagnostic_skip 不得 pass；A19 防漂移（score_sets_digest 掺入 evidence hash） |
| T09 API/CLI/Web | ✅ | 21 条路由、5 个 CLI 命令组、Web 四页，三端共用同一服务装配 |
| T10 CI/export/退出码 | ✅ | exporter v1（JSON/JUnit，JUnit properties 含 decision/exit_code/conclusion_hash）；退出码 0/1/3/5/5/6（4 属 M7 域） |
| T11 跨 suite/端到端 | ⚠️ 部分 | 端到端 3 用例 + live 闭环 ✅；C-Eval/Harbor/M4/M5 侧无本阶段专属实验矩阵 fixture（文档如实标注"间接覆盖"） |

**Gate 引擎强制力缺口（P1，对抗实测复现）**：
1. 政策只写 metric_threshold 时，coverage=2%、selected=100 的快照得 `pass`——覆盖防线是政策选项而非引擎强制（`motte_eval/gates.py:478-504`）；真实装配层有兜底（含 not_attempted 时 accuracy=None），但 critical_case-only 政策无任何覆盖防线。
2. `candidate_run_status='failed'` 且规则未显式设 `requires_successful_run=True` 时 gate 仍 `pass`（默认 False）。
3. severity=warn 规则永不贡献决策（全 warn 政策可绕过一切质量规则，发布期无告警）。

**其他**：statistical_policy_ref 在 `evaluate_gate_versioned` 中硬编码 `"statistical_policy@1"`（政策升版不反映进 GateResult hash）；Lite 与 Full 两套 Gate 语义长期并存（legacy 投影兼容）增加维护面。

---

## 3. 代码质量综合分析

### 3.1 架构与工程优点（跨阶段共性）

1. **fail-closed 纪律贯穿全链**：未知事件/字段/seq 回退/身份不匹配/版本漂移/停止未确认/证据缺失一律拒绝或隔离，未发现把缺失证据合成成功的路径。典型：M4 Pi 事件 allowlist + fail closed（`pi.py:490-540`）、M3 Verifier 异常优先于 reward（`parser.py:365-391`）、M6 insufficient 不放行。
2. **身份与幂等语义严密**：内容 hash 身份（TaskIdentity/数据集 fingerprint/SkillVersion/ComparisonPolicy）+ 三段幂等键 + 同键异内容 conflict 保留先写 + SQLite BEGIN IMMEDIATE / PG FOR UPDATE 的 CAS，跨三存储同语义。
3. **不可变历史与追加式评分**：ScoringPass 复合键、rescore 新 pass 旧 pass 不变、GET 零副作用、M6 A19 防漂移 hash——"已发布资源与终态评分不可覆盖"约束被持续执行。
4. **凭据边界纵深**：配置层只收 `env:` 引用、Authorization/x-api-key 永不持久化、审批帧含凭据即拒绝、证据白名单排除解析后含密钥的 dump、子进程环境白名单。
5. **Windows 进程所有权工程**（M4）：挂起→Job Object→NtResumeProcess 消除 PID 竞态、所有权失败时绝不枚举已运行进程后代——罕见地严谨。
6. **测试反例密度高**：M1 15 文件 + 40 个 review 反例、M2 14 文件 108 测试、对抗性场景（TOCTOU、迟到结果覆盖、双实例并发、越权工具）覆盖充分；断言具体到 reason 字符串。
7. **文档诚实度**：各验证记录的"部分/限制/not_run"标注与代码事实逐一吻合，未发现把 not_run 写成 passed 的虚报。

### 3.2 共性问题模式

1. **"库层完成、公共入口缺失"**：三臂对照、人工修订、校准报告、统计模块——功能在 packages/ 有实现有测试，但 API/CLI/Web 无消费者，用户不可达。
2. **文档与代码漂移**：AGENT_CATALOG 过时文案、M2.md 未回填 R4 结果、M5.md 状态矩阵过时、M4 compatibility.json argv 漂移——验证门禁（openapi-check）管不到手写字符串与散文。
3. **POSIX 假设残留**：`trusted.py` fd 锚定导致 Windows 本机 236 项失败；sandbox 侧已修（`7450009` 便携分支）而 benchmark-runtime 未跟进，同款问题两处不同状态。
4. **大文件/双路径维护成本**：`judge.py` 1908 行、`scoring_jobs.py` 1870 行、`main.py`（API 单文件承载全部路由，4600+ 行）；M1 builtin_react 两套期限逻辑；M6 Lite/Full 双 Gate。

### 3.3 重点发现清单（按严重度）

**P1（应尽快处理）**

| # | 发现 | 位置 | 影响 |
|---|---|---|---|
| 1 | `trusted.py` POSIX-only fd 锚定，Windows 原生 M2/M3 采集链路全不可用 | `packages/benchmark-runtime/motte_benchmark/trusted.py:41` | 本机无法全量回归（236 failed 族）；sandbox 同款已修，此处未修 |
| 2 | 主干 CI 3 项 M5 遗留失败，其中 PG ScoringJob owner mismatch 直接命中 Judge 归属语义 | `tests/scenario/test_fixture_lifecycle.py`、`tests/storage/test_scoring_jobs[PG]`、`test_trials_downgrade_guard[PG]` | 主干权威门禁非绿 |
| 3 | M6 Gate 覆盖防线非引擎强制 + 执行错误 opt-in + 全 warn 可绕过 | `motte_eval/gates.py:478-504,105,586-590` | 正式门禁可被政策配置意外放行低覆盖/失败 Run |
| 4 | Web Judge 页 6 条 API 路径失配（F-07 故意保留），且 openapi 门禁管不到手写路径 | `apps/web/src/api/client.ts:1526-1553` | 用户面 Judge 不可用；同类漂移可再穿透 |
| 5 | M4 3 例 flaky 测试（混合运行时失败、单独通过） | `test_appserver_worker.py`、`test_supervisor_safety.py` | 回归信号噪音，掩盖真实回归 |

**P2（计划内处理）**

| # | 发现 | 位置 | 影响 |
|---|---|---|---|
| 6 | M6 统计模块（pass@k/bootstrap/区间）零生产消费者 | `motte_eval/statistics.py` | T06 交付停在库+测试层；G10/G11"complete"口径偏宽 |
| 7 | M6 分组 baseline 未按 cell 条件匹配 | `comparisons.py:1257-1259` | 多 cell 实验的 baseline 求值取错 entry |
| 8 | AGENT_CATALOG 文案与事实矛盾 | `apps/api/app/main.py:65-80` | 公共目录端点误导调用方 |
| 9 | M1 完整模型对话历史不持久化（G05 口径差） | `agent_backend.py:390-407` | 排查模型行为只能靠事件计数 |
| 10 | 验证文档漂移：M2.md 未回填 R4、M5.md 状态过时 | `docs/verification/M2.md:505-516`、`M5.md` | 审计误读风险 |
| 11 | GSM8K/DirectLlm 旧 compare 页前端自算（M6-G19 例外） | `Gsm8kCompare.tsx:25`、`DirectLlmCompare.tsx:27-32` | 三端一致性债务（已登记） |
| 12 | M4 motte-settings.json 污染 Case 产物快照 | `cli_runtime.py:466-467` | before/after diff 稀释 |
| 13 | M5 三臂对照/人工修订/校准无公共入口 | `skill_ablation.py` 等 | 用户不可达（与 G21 口径有缺口） |
| 14 | live/外部证据整体未收口（M1/M2/M4/M5/Judge 校准/官方数据获取器） | 各验证记录 | stable 支持不可宣称（已如实登记） |

**P3（择机清理）**

| # | 发现 | 位置 |
|---|---|---|
| 15 | M3 死代码：`_location_payload` 重复定义、`SUPPORTED_AGENTS` 死常量、json reward 双重解析 | `adapter.py:260/395`、`config.py:95`、`parser.py:237-246` |
| 16 | `redact` 键名规则误伤 usage 字段（含 "token" 键整段替换） | `redaction.py:50,68` |
| 17 | regex/json-schema 评分器每指标 spawn 新进程，大指标量评测慢 | `observation.py:298-376` |
| 18 | workspace/artifact 根默认相对 CWD | `agent_backend.py:113,124` |
| 19 | mean-success `>=0.5` 阈值硬编码；statistical_policy_ref 硬编码 | `motte_eval/harbor.py:159`、`gates.py:329` |
| 20 | Web build chunk >500 kB；native 指标 UI `slice(0,8)` 截断；`_pi_installed_version` 只探测仓库内 node_modules | `ExternalBenchmarkPages.tsx:347`、`compatibility.py:89-102` |
| 21 | M4 退出码 4（用户取消）属 M7 域未实现；`codex_app_server_runtime.py:228` 直接下标 | `gates.py`、`codex_app_server_runtime.py:228` |

---

## 4. 遗留债务与风险汇总（优先级排序）

1. **live 验收债**（最大缺口，均如实登记）：M1 真实模型 Agent、M2 真实模型/full Profile + 官方数据获取器（受信核验器未部署）、M4 真实 Pi/Claude/Codex + app-server 真实审批、M5 Judge ≥30 人工校准（Judge 因此保持 experimental、正式门禁 fail-closed）。
2. **主干 CI 3 项 M5 遗留失败**——其中 PG ScoringJob owner mismatch 需优先判定是夹具问题还是 PG 事务路径真实缺陷。
3. **Windows 平台支持**：要么照抄 sandbox `7450009` 便携分支修 `trusted.py`，要么在 README 显式声明 POSIX-only 边界（当前声明不完整）。
4. **M6 Gate 引擎强制力**：建议在政策发布期加"正式政策必须含 coverage 规则/至少一条 block 级规则"的 lint。
5. **统计模块接线**：pass@k/bootstrap 接入报告/导出/compare；statistical_policy_ref 参数化。
6. **公共入口补全**：Web Judge 页重做（对齐真实 API）、三臂/校准/修订入口。
7. **文档回填**：M2.md R4 结果、M5.md 终态、AGENT_CATALOG、compatibility.json argv。
8. **架构债**：`scoring_jobs.py`/`judge.py`/`main.py` 拆分；M1 双期限逻辑合一；M6 Lite/Full Gate 收敛路线。

---

## 5. 附：审查边界与证据说明

### 5.1 Windows 本机 236 failed 的归因

失败族与 M6 验证记录登记的环境失败族一致（当时 232/233），根因三类：① `trusted.py:41` 在 Windows 对目录 `os.open` 抛 PermissionError（M2/M3 采集链路）；② symlink 创建需特权（M1 workspace 防护测试 skip 或失败）；③ 子进程存活/进程组语义差异。抽样失败文件（external_job_lifecycle、external_job_store、review_round3/4/5 workspace 系）均属此族。**CI（ubuntu + 真实 PG service）为权威全量门禁**；各阶段验证记录的 Linux/CI 结果（如 M6 全量绿）不在本次审查环境复现范围内。

### 5.2 审查覆盖声明

- M1–M6：7 路并行审查覆盖全部 G/T/A 编号（137 目标、77 工作包、119 验收场景逐项对照），关键论断主审抽查 6 项属实；定向测试由各路审查实测复跑（M1 89 passed、M2 核心集 POSIX 断言为文档+代码核对、M3 78 passed、M4 66+66+30 passed、M5 254+96+33+28 passed、M6 92+101 passed）。
- M7：仅规划提炼（G01–G23/T00–T12/A01–A21），未审代码（无代码可审）。
- 本报告未运行任何付费 live 评测；未修改任何代码。
