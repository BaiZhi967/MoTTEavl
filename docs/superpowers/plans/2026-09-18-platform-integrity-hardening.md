# 平台完整性与执行架构修复计划

日期：2026-09-18  
基线：`2d76107099855b013ccea0f881d8daeb894688f4`  
分支：`fix/platform-integrity-hardening`

## 1. 目标

本轮把当前可运行原型收紧为可并发创建、可安全恢复、可审计扩展的执行平台，优先消除静默覆盖、重复调用、假成功和协议漂移。

完成后必须满足：

1. Run 创建使用不透明 UUID，创建只能 INSERT；更新使用 revision/status CAS，冲突显式失败。
2. 默认部署只允许一个执行进程；所有持久 Run 通过同一 Dispatcher 和 Backend Registry 执行。
3. 外部调用存在“可能送达但未落库”窗口时进入人工确认状态，不自动承诺 exactly-once。
4. Run、CaseRun、TraceEvent、Score、ResolvedManifest、RunReport 和执行错误使用同一组公共 Contract。
5. 执行后端、Provider 协议和 Benchmark 插件是三个独立扩展点。
6. 模型身份区分 requested/reported/resolved，并保存可复核证据和策略结论。
7. 重评分、资源发布和交互命令均采用追加式、可追溯语义。
8. 旧 `run-N`、旧 benchmark 快照和既有 SQLite/PostgreSQL 数据保持可读。

## 2. 非目标与诚实能力边界

- 本轮不把 echo Pi bridge 宣称为真实 Pi Agent。Pi、Builtin Agent、Claude/Codex Harness 在 Backend 未接通前必须明确返回 unsupported。
- 本轮不实现多 Worker 水平扩展。owner/heartbeat/lease/fencing token 只预留契约；默认先用进程互斥强制单执行器。
- 本轮不承诺外部 Provider exactly-once。没有 Provider 幂等键或查询接口时，调用结果只能标为确定、失败或不确定。
- 本轮不动态加载第三方 Python/Web 插件。先建立显式内置注册表和测试插件扩展点。

## 3. 核心不变量

### 3.1 Run 聚合

- 新 ID 为 `run-<uuid4 hex>`；ID 仅用于身份，不承担排序语义。
- `revision` 从 1 开始，每次持久状态修改递增。
- `create()` 只允许插入；`update()` 必须携带 expected revision；`transition()` 还必须携带 expected status。
- Run 状态、对应 TraceEvent、CaseAttempt 结果和 ScoringPass 在同一数据库事务中提交。
- `retry` 创建新 Run 时一次写入 `parent_run_id`，不允许创建后补写。

### 3.2 单执行器与恢复

- SQLite 使用数据库旁的 OS 文件锁；PostgreSQL 使用 session advisory lock。
- 锁必须在 `requeue_interrupted()` 之前取得，第二个执行进程 fail fast。
- API 只创建/排队持久 Run；CLI inline replay 和 Celery 必须使用同一 Dispatcher 与执行锁。
- claim 产生 execution token；本轮 token 用于进程内断言，后续升级为持久 fencing token。

### 3.3 外部调用不确定性

`CaseAttempt` 状态：`prepared -> dispatching -> succeeded|failed`，崩溃恢复时残留 `dispatching` 转为 `indeterminate`。

- 调用前持久化 attempt。
- 调用完成后原子写 CaseRun、TraceEvent 和 attempt 终态。
- 存在 indeterminate attempt 的 Run 进入 `needs_review`，默认不重新调用。
- Replay 等确定性 Backend 可声明 `safe_to_repeat=true`，由恢复策略显式处理。

### 3.4 追加式评分

- 首次评分也是 `ScoringPass`。
- 每次 rescore 创建新 pass，旧 Score 永不删除。
- Run 保存 `current_scoring_pass_id`；报告可显式选择 pass，默认当前 pass。
- 未来 Baseline 必须绑定 `(run_id, scoring_pass_id)`。

## 4. 目标架构

```text
API / CLI
  -> RunApplicationService + RunStore UnitOfWork
  -> durable queued Run
  -> Worker Dispatcher
       -> ExecutionBackendRegistry
            -> DirectLLMBackend -> ProviderRegistry -> HTTP/Replay adapter
            -> ReplayBackend
            -> ExternalBenchmarkBackend
       -> BenchmarkPluginRegistry -> score + aggregate + report
  -> immutable ScoringPass / TraceEvent / RunReport view
```

### 4.1 ResolvedManifest v2

```json
{
  "schema_version": 2,
  "execution": {
    "backend_id": "direct-llm",
    "backend_version": "1",
    "config_hash": "sha256:...",
    "capabilities": {"interactive": false, "safe_to_repeat": false}
  },
  "evaluation": {
    "benchmark_id": "gsm8k",
    "benchmark_version": "1",
    "adapter_id": "gsm8k-official-jsonl",
    "adapter_version": "1",
    "scorer_id": "final-decimal",
    "scorer_version": "gsm8k-final-decimal-v1"
  },
  "provider": {},
  "resource_snapshots": {}
}
```

旧 `benchmark_provenance` 通过兼容解析器投影为 `evaluation`；新 Run 同时保留必要兼容字段一个发布周期。

### 4.2 模型身份

每个 Provider envelope 增加：

- `requested_model`
- `reported_model`
- `resolved_model_identity`
- `identity_evidence`
- `identity_policy_result`

策略：`report_only`、`require_reported`、`require_match`。结论：`exact_match`、`alias_match`、`unreported`、`mismatch`、`not_evaluated`。别名映射必须来自版本化 ModelProfile，不做通用前缀裁剪。

## 5. 分阶段实现

### Phase A：Run 写入完整性

1. 新增 UUID ID 工厂。
2. Run repository 增加 create/update/transition CAS；保留只读兼容，不再由业务调用 blind save。
3. SQLite/PG/InMemory 统一冲突异常。
4. queued/transition 事件与 Run 修改同事务。
5. 增加并发创建、重复 ID、stale revision、cancel-vs-finish 测试。

### Phase B：单执行器与不确定 attempt

1. 增加 WorkerExecutionLock，并在恢复前获取。
2. 删除无 Provider 时伪造 `{"case_id": ...}` 的成功路径。
3. API/CLI/Celery 统一 Dispatcher；未注册 backend fail closed。
4. 新增 CaseAttempt repository 和 `needs_review` 状态。
5. 注入调用前、调用后落库前、结果已落库事件未落库等故障测试。
6. Ctrl-C 测试增加真实子进程/阻塞调用覆盖；Pi 未接通前只报告 unavailable。

### Phase C：公共 Contract 与 API

1. 重建核心 Pydantic Contract，使用 Enum、default_factory 和 schema version。
2. API 核心 Run 端点使用请求/响应模型；错误统一为 `ErrorEnvelope`。
3. TraceEvent 采用稳定 envelope + payload，旧平铺事件只读兼容。
4. 生成 OpenAPI 和 TypeScript；Web client 从生成类型导出核心别名。
5. 增加实际响应校验和 OpenAPI 非空 schema 测试。

### Phase D：Backend 与 Benchmark 插件

1. 新增 ExecutionBackendSpec/Registry，Provider Registry 保持协议层职责。
2. 接入 DirectLLMBackend、ReplayBackend、ExternalBenchmarkBackend。
3. 新增 BenchmarkPluginSpec/Registry，迁入 GSM8K 和 Direct LLM。
4. 未知显式 suite/plugin 拒绝；仅 suite 缺失的旧运行兼容 GSM8K。
5. GSM8K/Direct LLM 专用端点严格校验资源 suite，错误返回 `SUITE_MISMATCH`。
6. 加测试专用 TinyBenchmark，证明不改 RunService 即可接入。
7. Agent/Skill/Harness/Pi 字段无法解析到 backend 时创建期拒绝，不退化为 Provider Run。

### Phase E：身份与审计语义

1. Provider AdapterSpec 增加实现版本并写入 manifest/envelope。
2. BaseHTTPProvider 保存 reported model 并执行身份策略。
3. ModelProfile 增加 identity policy/aliases/hash；Run 保存 profile 快照。
4. 普通 retry 复用快照；profile_stale retry 必须从 requested manifest 重新解析。
5. 新增 ScoringPass/ScoreSet；报告绑定 pass。
6. 版本化 Dataset/Scenario/PriceTable 改为 insert-only；Model 草稿可改、发布后不可覆盖；ProviderConnection 保持可变并递增 generation。
7. 新增 RunCommand repository。未接通命令消费者时 `/messages` 返回 501；非交互 Backend 返回 409。

### Phase F：Web、文档与兼容清理

1. `STATUS_META` 登记 `needs_review`。
2. Run 页面展示 execution backend、evaluation/scorer version、模型身份结论和 scoring pass。
3. Agent/Harness 目录增加 protocol-ready/execution-ready，Pi 显示协议桩而非已接通。
4. 更新操作文档、兼容矩阵、进度记录和迁移说明。
5. 保留旧字段读取测试，禁止新写路径继续产生 v1 数据。

## 6. 数据迁移

- PostgreSQL 新增 Alembic `0002_platform_integrity`。
- SQLite 构造器执行幂等 schema upgrade；新表使用 `CREATE TABLE IF NOT EXISTS`，必要列通过 `PRAGMA table_info` 检测。
- 旧 runs 在读取时补 `revision=0`、`schema_version=1`；第一次合法更新升级为 v2。
- 旧 scores 作为 legacy current view；第一次读取/重评分时可投影为 `legacy-initial` ScoringPass，不删除旧表。
- 旧事件保持可读，API 输出通过兼容适配器转换为标准 TraceEvent。

## 7. 验收矩阵

### 数据库与并发

- SQLite 和 PostgreSQL：并发创建 100 个 Run，无重复、无覆盖。
- duplicate create 报 conflict；stale revision/status update 报 conflict。
- 两个 Worker：第二个启动失败，不能 requeue 健康 Worker 的 Run。
- cancel/fail/complete 竞争只有一个合法终态。

### 崩溃与费用

- crash before dispatch：可安全恢复。
- crash during/after dispatch before persistence：Run 为 needs_review，不自动调用。
- persisted CaseRun：恢复跳过调用。
- Provider 支持幂等键时记录并复用稳定 key。

### Contract 与插件

- 核心 API 实际 JSON 全部通过 Contract。
- OpenAPI 核心响应不存在 `{}` schema。
- TinyBenchmark 只新增插件注册与测试代码，不修改 RunService。
- wrong-suite endpoint、unknown explicit suite、unsupported backend 均 fail closed。

### 审计

- 连续两次 rescore 后保留三个 ScoringPass。
- 报告指定旧 pass 时结果稳定。
- published resource 不可覆盖；Provider generation 可追踪。
- RunCommand 状态转换和不支持响应准确。
- 模型身份覆盖 exact/alias/unreported/mismatch/namespaced 五类。

### 门禁

```bash
make check
uv run pytest -q -m "not live"
pnpm --dir apps/web test
pnpm --dir apps/web build
docker compose -f infra/docker-compose.yml config -q
```

## 8. 提交与 PR 组织

计划提交：

1. `docs(plan): define platform integrity hardening`
2. `fix(storage): make run creation and transitions conflict-safe`
3. `fix(worker): enforce single executor and persist uncertain attempts`
4. `feat(contracts): align runtime and API core schemas`
5. `feat(execution): register backends and benchmark plugins`
6. `feat(provider): preserve and enforce model identity evidence`
7. `feat(audit): append scoring passes resources and run commands`
8. `feat(web): surface execution and audit truth`
9. `docs(ops): document migration recovery and capability limits`

最终 PR 必须包含迁移说明、行为变化、测试证据、已知限制和回滚方式。
