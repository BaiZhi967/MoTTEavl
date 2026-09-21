# 协议：SDK、CLI、历史迁移、备份恢复与发布（M7 T00 冻结）

> 状态：**frozen@1**（2026-09-22，T00 冻结；后续修订必须递增版本并记录原因）。
> 本文冻结 M7 所有客户端、迁移、备份、安全和发布契约的**权威语义**。实现以本文为准；
> 与本文冲突的实现是缺陷。M6 既有契约（ReportRef/ScoringPass/Baseline/Gate/退出码 0–6/
> exporter v1）不重复定义，M7 只做外部消费与扩展。

## 0. 冻结事实基线（T00 审计）

- 分支 `codex/m7-sdk-migration-release`，worktree `.worktree/m7-sdk-migration-release`，
  起始 HEAD `9940821`（= main，M6 merge `aee31a9` 之后）。
- Alembic head `0014_experiments_and_gates`（PG 链）；SQLite 用 `create_and_upgrade` 内联 schema。
- 环境：Windows/Git Bash，uv Python 3.12，Node 24.18 / pnpm 9.15.0；**本机无 docker、
  无 MOTTE_PG_DSN**（真实 Docker/PG 证据登记 blocked/not_run，不伪造）。
- 代码事实：`import motte_sdk` 零副作用；无 HTTP client；`POST /runs` 无幂等键；
  SSE 有 cursor/心跳/终态关闭但无 gap 语义；CLI 全部直连本地 DB；包均无 build-system；
  无导入账本；备份无一致性屏障/hash 清单；GC 仅按 mtime、无 pin 保护、无 tombstone；
  API 无 Origin/Host/CSRF/auth 防护。以上即 M7 缺口清单。

## 1. SDK 公共契约

### 1.1 Error envelope

SDK 解析服务端两种既有错误形状，统一映射为 typed 异常，不发明第三种线上形状：

- 应用错误：`{"error": {"code": str, "message": str, ...extras}}`；
- FastAPI 422：`{"detail": [{loc, msg, type, ...}]}`；HTTPException：`{"detail": str|dict}`。

SDK 异常层次（`motte_sdk.client_errors`）：

```
MotteClientError                      # 基类：携带 request_id | None、http_status | None
├─ TransportError                     # 网络层：连不上/DNS/读超时（可安全重试的查询另见 §1.4）
│  ├─ ConnectError / ReadTimeout / NetworkError
├─ ApiError                           # 服务端返回了错误 envelope/detail
│  ├─ ValidationError (422)           # 请求配置错误——永不自动重试
│  ├─ AuthenticationError (401/403)
│  ├─ PermissionDeniedError (403)
│  ├─ NotFoundError (404)
│  ├─ ConflictError (409)             # 含 REQUEST_KEY_CONFLICT / RUN_CONFLICT
│  ├─ RateLimitedError (429)          # 尊重 Retry-After
│  ├─ ServerError (5xx)
│  └─ MaintenanceError (503, code=MAINTENANCE_MODE)
└─ UnsupportedCapabilityError         # 能力协商失败（§1.2），携带 missing: list[str]
```

分类规则：先按 HTTP status，再按 `error.code` 细化（code 优先于 status 生成子类信息）。
`retryable` **不是服务端字段**（当前服务端不返回），由 SDK 按
`isinstance(err, (TransportError, ServerError, RateLimitedError, MaintenanceError))` 推导；
422/auth/permission/unsupported 恒不可重试。错误 `str()` 不包含请求 body（防凭据/prompt 泄漏）。

### 1.2 Capability handshake 与版本兼容

- 新端点 `GET /api/v1/capabilities` →
  `{"name":"motteavl","api_version":"v1","app_version":str, "features":{...bool}, "limits":{...}}`。
  `features` 当前冻结键：`idempotent_run_create`、`sse_cursor`、`sse_gap`、
  `events_snapshot`、`maintenance_mode`、`gate_export`。
- SDK 构造 `MotteClient(base_url)` 时**惰性**握手（首次请求前 `GET /capabilities`，缓存至进程结束）。
  `api_version != "v1"` → 立即 `MotteClientError`；调用某方法所需的 feature 键缺失 →
  `UnsupportedCapabilityError(missing=[...])`，**不得**降级猜测。
- 未知新增 feature 键忽略（前向兼容）；未知响应字段保留在 typed response 的
  `extra: dict` 中不丢弃（后向兼容）。
- SDK 版本策略：`motte_sdk.__version__` 语义化；minor 版本只加方法/字段，不删。

### 1.3 Canonical request hash 与幂等键

- **Canonical hash**：`sha256(canonical_json(body 去掉 request_key 字段))`，
  复用 `motte_contracts.identity.canonical_sha256`（排序键、紧凑分隔符、ensure_ascii=False）。
- **幂等键**：创建类请求可选 `request_key`（body 字段，与 ScoringJob/Experiment 同模式）。
  服务端是唯一权威：`POST /api/v1/runs` 携带 `request_key` 时，
  - 同 key + 同 canonical hash → 返回**同一 Run**（幂等重放，200/202 皆可，响应带 `idempotent_replay: true`）；
  - 同 key + 不同 hash → `409 REQUEST_KEY_CONFLICT`（携带 incoming/stored hash）；
  - 注册表持久化（新表 `motte_request_keys`，SQLite+PG），跨进程重启仍生效。
- SDK **不得**自行猜测"看起来重复"来决定是否重放；只能整键重送原 body。
- 该绑定在 API 公共入口实现，CLI/Web/SDK 共用（见 §6 API 变更）。

### 1.4 Retry matrix / timeout / cancel

| 请求类别 | GET 查询 | POST 创建（带 request_key） | POST 创建（无 key） | cancel/rescore/retry | SSE 流 |
|---|---|---|---|---|---|
| 网络失败/超时/5xx/429/503 | 自动重试（默认 3 次指数退避 + 抖动，尊重 Retry-After） | 同左；重送**原 body 原 key**，由服务端幂等收口 | **不自动重试**（可能重复付费）→ 抛 TransportError 由调用方决定 | 同左（不自动重试） | 断开后由调用方决定重连；SDK 的 `stream_events` 提供自动重连+去重（§2） |
| 422/auth/403/404/409/unsupported | 永不重试 | 永不重试 | 永不重试 | 永不重试 | — |

Timeout 三段：`connect_timeout`（默认 5s）、`read_timeout`（默认 30s）、`overall_deadline`
（单次调用总期限，默认 120s，wait/streams 另有独立 deadline）。任一超时抛对应 typed 异常。
Cancel：所有阻塞调用接受可选 `threading.Event` 类 `cancel_event`；触发后停止等待并在
合理边界抛 `OperationCancelled`。**wait 超时只停止等待，绝不暗中 cancel 远端 Run。**

### 1.5 SDK 包边界

`import motte_sdk` 零副作用（不建 DB、不启 Worker/Provider、不读环境凭据）。
HTTP client 的依赖只有 `httpx`（新增 extras，见 §8）。执行类模块（service/dispatcher/…）
保持懒导入：`from motte_sdk import MotteClient` 不触发执行栈导入。

## 2. SSE 事件流与终态对账

- Event id = 持久 `seq`（`id: <seq>`），payload = `TraceEvent` envelope（schema_version 2）。
- Cursor：`?after=<seq>` 或 `Last-Event-ID` 头，服务端取 `max(after, Last-Event-ID)`；
  非法 Last-Event-ID 返回 400（不 500）。
- 心跳：`: ping` 注释行（既有）。
- **Gap 语义（新冻结）**：重连时若 `cursor+1 < 该 run 现存最小 seq` 且最小 seq>1，
  服务端先发一帧命名事件 `event: motte-gap`，data
  `{"type":"gap","after":cursor,"next_seq":min_seq,"partial":true}`，随后正常续传。
  客户端收到 gap 后将流标记 `partial=true`，并用持久查询补齐：
- **持久查询（新端点）**：`GET /api/v1/runs/{id}/events/snapshot?after=&limit=` →
  `{"events":[TraceEvent...],"last_seq":int|"null","run_status":str,"partial":bool}`。
  limit 默认/上限 500。SDK `stream_events` 断线重连流程：SSE 重连 → 见 gap →
  snapshot 补齐 → 继续流；snapshot 也补不出的段保持 partial 并在结果中如实上报。
- **去重**：客户端按 `seq` 去重（Set），乱序按 seq 排序后交付。
- **终态对账**：收到终态事件后，SDK 读取 `GET /runs/{id}` 与
  `GET /runs/{id}/report`（默认当前 pass），校验 run.status 终态一致、最后事件 seq ==
  流内最大 seq，不一致 → 结果标 `reconciliation_mismatch` 并附两侧事实，不假装完整。
- `needs_review` 是**需用户处理的终态**：wait 返回它，不自动 retry。
- 流大小上限：单次 poll 最多发送 500 事件（超出部分下轮继续，同一条流内分批）。
- stdout/stderr 纪律：SDK/CLI 机器数据只走 stdout（单行 JSON/JSONL）；诊断、重试日志、
  心跳提示只走 stderr。凭据、完整 prompt、敏感 payload 不写 SDK debug 日志（脱敏工具
  复用 `motte_trace.redaction`）。
- Web `useRunEvents` 与 SDK 使用**同一 seq/cursor/gap/terminal 语义**（§7）。

## 3. CLI local/server 路由

- 每个命令显式 `--mode local|server` 或全局 `MOTTE_CLI_MODE`（默认 `local`）；
  server 模式必填 `--api-url`（或 `MOTTE_API_URL`）。优先级：命令行 > 环境变量 > 默认。
- **server 模式只通过 SDK HTTP**：不得打开本地 DB、不得构造 Provider、不得启动任务。
  远端不可达/认证失败/超时 → 非零退出（exit 2 或语义码），stderr 单行 JSON 诊断；
  **禁止静默回退本地**。
- local 模式语义与现状一致（直连 DB）；`--db` 仍强制 SQLite。
- 覆盖命令（server/local 双实现）：`run create/list/get/cancel/retry/events/wait/report`、
  `pass list/report`、`experiment preview/create/status/cancel`、`compare`、`baseline *`、
  `gate *`、`regression`、`export`。仅 local：`backup/restore/cleanup-artifacts/credentials/
  doctor/benchmark download/import`（涉及宿主文件或凭据，不经远程执行）。
- stdout：成功输出单行 JSON（`--pretty` 可缩进）；失败诊断 `{"error":{code,message,...}}`
  到 stderr。退出码沿用 M6 冻结（0/1/2/3/5/6 + 4=用户取消）；local/server 同输入
  必须产出同错误码与同 JSON 键集（parity 测试固化）。
- 兼容：既有顶层子命令（`run`/`replay`/`experiment`/`compare`/`baseline`/`gate`/
  `regression`…）保留原参数并映射到新路由；破坏性变化必须先弃用标注一个版本。
- live 语义：真实模型/Judge/Harness 调用只能由显式动作触发（`live-smoke`、
  `model test`、Worker 执行）；server 模式 CLI 默认零付费调用。

## 4. pytest、Trace 与导出扩展

- **pytest 插件**（`motte_sdk.pytest_plugin`，extras `pytest` + entry point `pytest11`）
  完全 opt-in：只有用 `--motte-gate-report <path.json>`（或 ini `motte_gate_report`）
  指向一个**已存在的 M6 导出 JSON** 时才启用；它把 Gate 导出的 rule 结果映射为
  pytest 汇总报告（quality_fail→failure、execution_error→error、skipped_diagnostic/
  not_applicable→skipped、insufficient/not_comparable→error）。**普通 pytest 运行
  （未开启）零模型/零 Judge/零 Benchmark/零 Run 创建、零数据集扫描、零凭据读取。**
  真实执行 fixture 与报告读取分离：插件永不创建 Run。
- **Trace decorator/context manager**（`motte_sdk.tracing`）：包裹用户 callable，
  **只执行一次**；记录嵌套 span、异常、脱敏（`motte_trace.redaction`）、
  截断（单事件 payload 上限 64KiB，超出截断并标记 `truncated:true`）；
  flush（提交 span 到处理器）失败只记录 `flush_error`，**绝不重跑 callable**。
- **exporter v1 扩展（不另立版本主权）**：`motte_sdk/export.py` 保持
  `EXPORTER_VERSION="gate-exporter@1"`，M7 允许的 v1 内扩展：testsuite 增加属性、
  增加 `--out` 平行文件；**不改变** decision→exit_code/JUnit 映射、不重算分数、
  零模型/Judge/Runner 调用。`insufficient/not_comparable/空集` 导出为 `<error>` 或
  显式非通过状态，**永不导出为假通过**。

## 5. 历史迁移契约

### 5.1 数据模型（`motte_contracts.imports`）

- `ImportManifest`：`import_id`、`importer_version`、`transform_version`、
  `source_system`、`source_repository`、`source_revision`、`source_schema_version`、
  `exported_at`、`record_counts: dict[str,int]`、`artifact_manifest_sha256`、
  `scope: list[str]`、`content_sha256`（整个来源包的内容 hash，apply 前复验）。
- `SourceIdentity`：`source_system`、`source_record_type`、`source_id`、
  `source_version`、`content_hash`。
- `MappingRecord`：`mapping_key = sha256(source_system|record_type|source_id|
  source_version|transform_version)`、SourceIdentity、`target_type/target_id`、
  `status ∈ {planned,created,reused,conflicted,rejected,rolled_back}`、
  `diagnostics: list[str]`。
- `ImportReport`：`planned/created/reused/rejected/conflicted counts`、
  `missing_artifacts`、`unknown_fields`、`warnings`、`checkpoint`、
  `verification: {counts_match, hashes_match, references_ok}`。

### 5.2 来源包与安全边界

- 来源=操作者显式提供的受控目录（`manifest.json` + `records/<type>/<id>.json` +
  `artifacts/<sha256 前缀路径>`）。校验：拒绝绝对路径/`..`/symlink（含中间目录）、
  zip 解压总大小 ≤ 512MiB/文件数 ≤ 20k、单 record ≤ 4MiB、record 总数配额、
  `manifest.json` 声明的许可证字段必填且未知值→restricted、secret 形状字段
  （key/token/secret/password）拒绝或脱敏登记。API **不接受**任意宿主路径；
  服务端只读操作者声明的目录。
- **dry-run**：只读来源+目标元数据；输出 planned/reused/conflicted/rejected、
  unknown fields、missing artifacts、credential rebind 清单（只列引用名，不迁值）、
  counts/hash；对目标 DB/Artifact/credential/状态**零修改**。
- **apply 前复验**：重算来源包 `content_sha256`；与 dry-run 计划记录的 hash 不一致 →
  计划失效，必须重新 dry-run（不允许沿用旧批准）。
- 凭据只迁**引用**（`api_key_env` 名/`credentials profile` 名），输出 rebind 清单；
  永不复制 secret 值、会话或整个配置目录。

### 5.3 导入次序与历史保真

配置（ProviderConnection/ModelProfile 引用映射）→ Dataset/Case → Scenario/Skill
→ 历史 Run/Trial/Trace/Artifact/Score/Baseline/Gate。

- imported Run：`manifest.import_source = {system, record_id, content_hash, import_id}`、
  `origin:"imported"`、不可分发（`distributable:false`）；状态=旧系统记录的终态
  （completed/failed/cancelled），**永不 queued**；Dispatcher `_ready_for_claim`
  对 import_source 非空返回 False（双保险）。进行中旧 Job 不迁（dry-run 即 rejected：
  `in_flight_job_not_importable`）。
- 缺 reported model/gold/sample set/ScoringPass/price/license/scorer → 字段保持
  `unknown`（显式），**禁止**从当前 ModelProfile/PriceTable/ScoringPass 补写。
  旧系统只有聚合分（如 summary=0.5）→ 只存 legacy summary（`summary_score`），
  **不伪造**逐 case 分数。
- Artifact：先按声明 sha256 校验暂存（staging 目录），DB 引用提交后才正式关联；
  hash 不匹配 → 该 record rejected，不提交引用。
- 旧 baseline/gate 结论导入为 `origin:imported` 的历史结论，**不**自动成为新平台
  正式 Baseline/GateResult 参与资格（不进入 baseline 默认指针，不参与 gate 正式求值）。

### 5.4 checkpoint / resume / conflict / rollback

- 每条 MappingRecord 一个事务单元；单元提交后写 `motte_import_mappings` 行。
  crash/resume：按 mapping_key 跳过已提交单元，只处理未提交的；重复 apply 幂等。
- 同 mapping_key + 不同 source content_hash → 该单元 `conflicted`（停止该单元，
  保留诊断，不覆盖目标），批次继续其余单元。
- 对账：apply 完成后核对 planned vs created+reused+rejected+conflicted counts、
  artifact hash、引用完整性、原始/归一化分数与状态；生成只读 ImportReport。
- **rollback**：只撤销本 import_id 创建且未被**任何**其他资源（其他 run/baseline/
  import 批次）引用的对象；或整批标记 `inactive`（保留审计）。共享 Artifact、既有
  baseline/pass、跨批引用 → 阻断并输出诊断，不删除。apply/resume/rollback 均要求
  `--import-id` + 操作意图参数，写审计记录（operator、时间、manifest hash）。

## 6. API 变更（服务端，M7 新增）

1. `POST /runs` 幂等：`CreateRunRequest.request_key?: str`；注册表
   `motte_request_keys(request_key pk, canonical_hash, run_id, created_at)`（SQLite+PG，
   迁移 `0015_m7_sdk_support`）；replay 响应加 `idempotent_replay:true`；同 key 异 body
   → 409 `REQUEST_KEY_CONFLICT`。
2. `GET /api/v1/capabilities`（§1.2）。
3. SSE gap 帧 + `GET /runs/{id}/events/snapshot`（§2）+ 非法 Last-Event-ID → 400 +
   poll 批量上限 500。
4. Maintenance mode：`motte_meta` kv 表 + `GET /api/v1/maintenance`（状态）+
   API 写入口（run 创建/cancel/retry/rescore、资源发布、judge 提交、GC）在维护窗口内
   返回 503 `MAINTENANCE_MODE`；维护屏障由 CLI `motte backup --consistent` 先置位。
5. 安全中间件（§安全节）：Host allowlist + Origin 校验 + 可选 Bearer token + CORS。

## 7. Web 一致性

`useRunEvents` 升级：显式 cursor（重连带 `?after=`）、seq 去重集合、收到
`motte-gap` 命名事件→标记 partial→调用 snapshot 端点补齐、终态后关闭流并核对 run
状态。组件消费接口（events/status/partial）保持向后兼容。

## 8. 打包、依赖与 provenance

- 每个 workspace 包获得真实 `[build-system]`（hatchling）+ 真实依赖声明：
  `motte-contracts`→pydantic+cel-python（既有）；`motte-sdk`→contracts+httpx，
  extras `pytest=[pytest]`、`server=[fastapi,uvicorn]`（可选，仅为便捷，运行 API
  仍靠根部署）；`motte-cli`→sdk+storage；`motte-storage`→contracts+psycopg[postgresql]
  extras；`motte-eval`→contracts+jsonschema。**Runner/Worker 专属依赖（docker、celery、
  psutil、pyyaml）不进入 SDK/API 依赖闭包。**
- 交付物：`motte_sdk`/`motte_cli` wheel + Web 静态构建（`apps/web/dist`）；
  wheel 构建命令与 lock 固定记录在 release notes；clean venv 安装测试在
  checkout 外目录运行，断言无 cwd/PYTHONPATH 依赖、资源可读、fake API 可调。
- Docker 镜像：单阶段 python:3.12-slim + uv sync（既有）+ `HEALTHCHECK`；
  compose 保持 loopback 端口绑定。真实 build/up 证据按环境登记。

## 9. 备份、恢复与 GC

### 9.1 一致备份

- 屏障：备份前 `begin_maintenance`（DB kv 置位，API/Worker 写入口拒绝新写入并 503），
  完成后 `end_maintenance`。屏障覆盖：Run 创建/状态推进、ScoringJob claim、资源发布、
  Judge、Artifact 删除/GC。
- SQLite：`sqlite3` online backup（既有）+ 屏障；PG：`pg_dump --format=custom`
  （二进制存在才执行，否则 blocked 如实登记）。
- Artifact 一致性：**先**从 DB 快照确定不可变 Artifact 引用清单（扫描 runs/case_runs/
  invocations/external_job_records/baselines/import 账本中引用的 artifact id），
  逐文件复制 + sha256 校验；缺引用文件→备份**失败**（不标成功）；额外文件记 warning。
- Manifest v2：`{created_at, app_version, backend, schema_version|alembic_revision,
  database:{snapshot,bytes,sha256}, artifacts:{dir,files:[{path,bytes,sha256}]},
  counts:{runs,scoring_passes,baselines,gate_results}, maintenance:{started_at,ended_at},
  manifest_sha256}`。

### 9.2 staging 恢复

- 恢复**默认到新 staging 目录**（staging db + staging artifact root）；校验
  manifest hash、DB schema 版本、每文件 hash、引用完整性（被引用 artifact 存在）；
  任何缺失→`restore_incomplete`，不标成功。
- 恢复后 DB 置 `motte_meta.restored_from_backup=<manifest>`；WorkerLoop 启动时若该
  标志存在→**拒绝 claim 任何 run**（打印 `restore_guard_active`，需
  `motte restore-guard clear --yes` 显式解除）。queued/running/needs_review 保持原状态
  等显式决定，不自动付费执行。
- 覆盖现有运行环境必须 `--confirm-overwrite`（并要求先备份）。

### 9.3 Retention 与 GC

- 保留类：`pinned`（EvidencePin 引用/Baseline entry 引用/needs_review/import 审计
  引用→永不删）；`artifact`（TTL 天，默认 90）；`provider_payload`（原始请求/响应
  payload，TTL 默认 30）；`trace`（普通事件，TTL 默认 180）。
- `motte gc plan`（默认 dry-run）输出分类清单+保留原因；`--apply` 才删；
  删除写 tombstone（DB 表 `motte_gc_tombstones`：artifact_id、sha256、bytes、
  reason、gc_run_id、deleted_at），保留审计可查。
- GC apply 与采集/评分/备份互斥：必须持维护屏障执行。
- 缺失证据后：rescore/compare 的既有 fail-closed 语义不变（不降级为成功）。

## 10. 单用户安全边界

- 默认部署假设 loopback。Host 校验：默认 allowlist `localhost,127.0.0.1,[::1],
  testserver`（+ `MOTTE_ALLOWED_HOSTS` 扩展）；其他 Host → 400 `HOST_REJECTED`
  （防 DNS rebinding）。
- Origin/CSRF：对 POST/PUT/DELETE/PATCH，若带 `Origin` 头则其 host 必须在 allowlist
  （同源）或 `MOTTE_ALLOWED_ORIGINS`（显式 CORS 白名单）内；否则 403 `ORIGIN_REJECTED`。
  无 Origin（CLI/SDK/浏览器同源 GET）不受影响。认证用 `Authorization: Bearer`（非
  cookie），无 cookie 即无经典 CSRF 面。
- Auth：`MOTTE_API_TOKEN` 设置后所有 `/api/*` 要求 Bearer 匹配（`/health` 与
  `/api/v1/capabilities` 豁免，便于握手/探活）；未设置=本地信任模式（仅靠 Host/Origin）。
- 远程部署：文档要求反代 TLS + `MOTTE_API_TOKEN` + `MOTTE_ALLOWED_HOSTS`；执行类
  API 永不无认证裸奔公网。允许的内网 Provider endpoint 由既有 allowlist 机制管理；
  HTTP client 全局禁止重定向到不同 host 携带认证头（SDK httpx `follow_redirects=False`
  默认，重定向视为 TransportError 分类处理，不自动跟随泄漏）。
- 脱敏：凭据接口只返回掩码/引用（既有）；日志/SSE/Trace/导出/Artifact 路径复用
  `motte_trace.redaction`；错误响应不含请求 body。
- 供应链：CI 增加 wheel 构建 + 依赖审计（既有 pip-audit/pnpm audit/trivy config）+
  license 清单检查（合成 fixture only）；公开仓库不含真实凭据/真实响应。

## 11. 发布与切换状态机

- 状态：`implementation_complete → offline_verified → external_pending →
  stable_supported`；切换另有 `cutover_ready`。任何状态跃迁必须有支持矩阵证据行；
  skip/not_run 不计入 stable。
- RC 冻结：RC commit SHA、uv.lock、wheel、Web dist、Docker image、Alembic head、
  OpenAPI schema 版本、exporter 版本全部登记于 `docs/release/release-notes.md`。
- 支持矩阵（`docs/release/support-matrix.md`）：OS/运行方式 × SQLite/PG ×
  Runner/runtime × Provider/Benchmark × 操作（安装/执行/报告/迁移/备份/恢复），
  值 ∈ {tested, supported, experimental, blocked, not_run}，附证据链接。
- 升级演练：空库安装、上一支持版本→当前、migration failure→恢复、旧应用读新状态
  （明确报错不误判 queued）、备份后升级失败回滚。
- 切换（需单独授权）：冻结 RC 与旧参考 commit → 停旧平台新增/付费任务 → 存量终态
  确认 → 只读备份+dry-run → 分批 apply+对账 → 三类替代场景验收 → 新任务单路进入
  → 旧库只读保留 + 回退窗口。**不自动归档旧仓库、不反向迁移进行中 Run、不双写。**

## 12. 反例冻结（验收场景与测试一一对应）

A01–A21 的行为反例见执行计划 §5；本协议对应实现要点：
A02/A03（§1.3/1.4）、A04（§3）、A05（§2）、A06/A07（§4）、A08（§5.2）、A09（§5.4）、
A10（§5.3）、A11（§5.2）、A12/A13（§5.4）、A14–A16（§9）、A17（§10）、A18（§9.3）、
A19（§11 升级演练）、A20（§11 矩阵）、A21（§11 切换）。
