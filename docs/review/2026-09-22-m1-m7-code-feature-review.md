# MoTTEavl M1-M7 代码与功能开发审查报告

- 审查日期：2026-09-22（UTC+08:00）
- 审查基线：`main` @ `01d347681d5ff88ee2bfd9d83cc015f6d2b7cef3`
- 审查范围：总路线、M1-M7 阶段规划与执行计划、当前主干实现、自动化测试、Web/CLI 真实链路
- 判定原则：`implementation_complete`、`offline_verified`、`live_verified`、`stable_supported`、`cutover_ready` 分层判定；缺少外部证据时保持 unknown/not_run
- 配套文档：[详细测试点](2026-09-22-m1-m7-test-points.md)、[实际测试报告](2026-09-22-m1-m7-test-report.md)

> 修复状态更新：本报告第 4 节保留审查时的原始 finding；修复后的 superseding 状态见第 9 节。

## 1. 执行摘要

**结论：M1-M7 已形成覆盖面很广的实现，但不能判定“所有功能已完成”，更不能判定为 stable supported 或 cutover ready。**

当前可以确认：

1. M1-M7 的主要领域对象和执行链路均有实质代码，不是只有路线文档或空壳。
2. M1 Builtin Agent 与 Direct LLM 已在本轮使用 `deepseek-v4.1-flash` 完成真实模型调用、Worker 执行、评分、事件与 Web 下钻。
3. Web 286 项组件测试、production build、ruff、mypy、compileall、OpenAPI 漂移检查通过。
4. M7 SDK、CLI 双模式、迁移、备份/恢复、GC、安全边界和 release/cutover 均已有实现及大量自动化测试。

但发布判断必须被以下事实否决：

- 存在 Provider 密钥随跨 origin redirect 泄漏的 CRITICAL 安全问题。
- GC 引用遍历不完整，可能删除仍被 CaseRun/Attempt/Invocation/ExternalJob 引用的冻结证据。
- ArtifactStore 读取/删除可通过 symlink 或未经校验的删除路径逃逸 artifact root。
- Origin 校验只比较 hostname，忽略 scheme/port，同主机不可信站点可发起写请求。
- 一致备份的维护屏障不等待在途 Worker，也没有互斥 owner，不能保证快照时点一致。
- Judge Web 与 API 契约完全错位，页面真实请求稳定返回 405。
- Windows 下 Harness 目录端点通过 Uvicorn 真实启动后返回 500。
- 终态 SSE 和 server CLI 的事件读取在事件数超过 500 时静默截断。
- M2/M3/M4/M5/M7 仍有真实外部环境、官方数据、Docker、PG 备份恢复、人工校准和 cutover 证据缺口。

推荐的准确状态表述是：

> M1-M7 的多数实现层与离线层已落地，M1/M6 有有界真实模型证据；当前仍存在发布阻断级安全/数据完整性缺陷、产品入口断链和外部验收债务。M7 为 external_pending，不满足 stable_supported 或 cutover_ready。

## 2. 审查方法与证据

- 逐份阅读 `docs/ROADMAP.md`、`docs/PROGRESS.md`、`docs/roadmap/M1-*.md` 至 `M7-*.md`。
- 对照 `docs/superpowers/plans/2026-09-19-m1-m7/`、M5/M6/M7 专项执行计划及 `docs/verification/`。
- 静态审查 API、Worker、Provider、Agent、Sandbox、Storage、SDK、CLI、Web 及测试代码。
- 执行 CI 同口径门禁、Web 测试/build、OpenAPI check 与 Compose 检查。
- 使用 browser-skill 真实巡检 M1-M7 产品入口，并创建/执行/下钻真实 Run。
- 使用 CLI local/server 两种 transport 创建和查询真实 Run。
- 所有外部能力结论均区分本轮实测、历史账本和未执行项。

## 3. 阶段完成度

| 阶段 | 代码实现 | 本轮/历史验证 | 未完成或不满足项 | 审查结论 |
|---|---|---|---|---|
| M1 原生 Agent/评分 | Builtin Agent、Observation、多 evaluator、artifact、事件、预算、取消、API/CLI/Web 均存在 | 本轮 DeepSeek native-tool 单题真实通过；2 次工具调用、2 项指标通过、产物正确 | 原路线 Supported 要求双模型、取消与费用证据；持久 event sink 失败可被吞，预算异常值与最后一步取消仍有缺口 | Core 可用，Supported 未完全满足 |
| M2 LLM Benchmark/C-Eval | ExternalJob、Catalog/Profile、parser、OpenCompass adapter、M6-Lite 均存在 | 假 Runner/固定 Runner已有记录 | 官方数据/许可、full Profile、真实模型与完整 PG 并发未收口；PG import 并发非原子 | 实现较完整，正式验收部分完成 |
| M3 Harbor/Terminal-Bench | TaskIdentity、Trial、Harbor job/parser/verifier/API/Web 均存在 | 历史 Docker oracle/PG 有证据 | 本机无 Docker；真实 Agent 小批次、完整上游集、平台变体未验；共享 sandbox 并发缺陷影响稳定性 | 离线核心完成，外部支持未完成 |
| M4 Pi/外部 Harness | Pi、Claude/Codex、supervisor、command consumer、Inspect 均有实现 | 历史 Linux/PG CI；本轮目录页巡检 | Pi/Claude/Codex/app-server 真实任务与审批/取消仍 live_pending；Provider redirect 与 invocation 持久证据缺陷 | implementation_complete / live_pending |
| M5 Scenario/Skill/Judge | Workflow、Fixture、Skill、Judge/ScoringJob 均有实质实现 | 历史定向验收较多 | Judge Web/API 断链；30 条人工校准未做；PG scoring recovery 并发错误；三臂/人工修订/校准公共入口不足 | 产品端到端未完成 |
| M6 Experiment/Compare/Baseline/Gate | 主要服务、API、CLI、Web 和统计库均存在 | 历史 offline_verified + bounded live | 旧 GSM8K/Direct LLM compare 绕过统一比较；统计无生产消费者；Gate/coverage/failed-run 边界和 PG validation/parity 有缺口 | 离线主链可用，发布门禁仍需修复 |
| M7 SDK/迁移/发布 | typed SDK、SSE、CLI、pytest、import、backup/restore、GC、安全、release/cutover 均存在 | 大量专项测试与 Linux CI 记录；本轮 CLI local/server 与 Web 实测 | CRITICAL/HIGH 安全与数据完整性问题；Compose/真实 PG backup/旧导出/三替代场景/cutover 未执行 | 功能面落地，但不可稳定发布/切换 |

## 4. 代码审查 Findings

### CRITICAL

#### C-01 Provider 凭据可随 redirect 泄漏

- `packages/provider-runtime/motte_provider/transport.py:54` 使用默认 `urllib.request.urlopen`。
- `transport.py:92-104` 和 `174-180` 在请求上放入 `Authorization` 或 `x-api-key` 后直接交给会自动跟随 redirect 的 opener。
- 未限制跨 origin、跨端口或 HTTPS -> HTTP 降级；普通 JSON POST 与 SSE 均受影响。
- 审查中的双本地 origin 复现显示，302 后第二 origin 收到了合成的 `Authorization: Bearer TOPSECRET`。
- 现有 redirect 测试只覆盖 dataset source，不覆盖 Provider transport。

影响：恶意或被接管的 Provider 端点可通过 redirect 窃取真实模型凭据。此项必须在任何发布前修复。

#### C-02 GC 会漏掉子实体引用并删除冻结证据

- `packages/storage/motte_storage/gc.py:73-88` 仅尝试无参 `repo.list()`；CaseRun、Invocation、Attempt、ExternalJob 实际使用 `list_for_run()`/`jobs_for_run()`。
- 参数形状不符产生的 `TypeError` 被吞掉。
- `gc.py:130-173` 由不完整引用集生成删除计划，`212-234` 执行删除。
- 当前测试只覆盖 run manifest/baseline 引用，没有覆盖仅存在于子实体中的 artifact 引用。

影响：即使 Run 为 `needs_review` 或被 baseline pin，关键审计证据仍可能被错误 GC。

### HIGH

#### H-01 ArtifactStore 读取/删除可逃逸 root

- `packages/storage/motte_storage/artifacts.py:26-30` 对 read 只检查输入中的绝对路径/`..`，resolve 后没有重新验证 containment。
- `artifacts.py:32-33` 的 delete 完全没有 containment 校验。
- root 内 symlink 或恶意 artifact id 可读取/删除 root 外文件。

#### H-02 Origin 写边界忽略 scheme 和 port

- `apps/api/app/security.py:99-102` 只要 origin hostname 在 allowed_hosts，就与 exact allowed_origins 做 OR 放行。
- 当 allowlist 是 `http://localhost:3080` 时，`http://localhost:9999` 与 `https://localhost:4443` 仍可发写请求；审查已用 TestClient 复现。
- `tests/security/test_single_user_boundary.py:30-98` 没有同 host 异端口/异协议反例。

#### H-03 Agent/Scenario 的持久证据失败可被吞并误报 complete

- `motte_agent/builtin_react.py:657-668` 与 `motte_agent/pi.py:459-465` 吞掉 event sink 异常。
- `motte_sdk/agent_backend.py:591-614` 只看内存 events 与 workspace/artifact errors，未校验持久 event refs，即可设置 coverage complete。
- Pi/Scenario 的 prepared -> dispatching/settled 持久化错误也可被吞后继续真实动作。

影响：执行已经发生，但恢复和审计所需持久证据可能不存在，违反 M1/M4 的核心证据合同。

#### H-04 PostgreSQL ExternalJob 导入并发不原子

- `motte_storage/pg_audit_store.py:769-818` 未对 job 行做 `FOR UPDATE`。
- 两个不同 record 可同时读取相同 checkpoint，各自成功插入后都写 `N+1`，造成计数丢更新；同 key race 可暴露裸 UniqueViolation。
- SQLite 在 `external_jobs.py:309-375` 使用 `BEGIN IMMEDIATE`，后端语义不一致。

#### H-05 PostgreSQL scoring recovery 可把 prepared 错标 indeterminate

- `motte_storage/scoring_jobs.py:1694-1746` 先无锁列 id，再逐行锁。
- 两个 recoverer 可同时选中 prepared；第一个改 queued 后，第二个把已 queued 记录走 catch-all 标为 indeterminate。

#### H-06 维护屏障不能保证一致备份

- `motte_storage/maintenance.py:88-107` 对已有 active 当作成功重入，且任何调用者均可清除，没有 owner/lease。
- Worker 只在 claim 前检查维护状态（`apps/worker/motte_worker/runtime.py:232-261`），在途任务继续写。
- 备份先做 DB snapshot，随后从 live store 获取 artifact refs/counts（`maintenance.py:395-410`），manifest 可能混合不同时点。
- GC 也复用同一非互斥 flag，重叠操作可提前解除另一方屏障。

#### H-07 DockerSandbox 并发任务共用并互删 workspace

- 默认策略固定 `workspace='workspace'`（`motte_sandbox/policy.py:41-44`）。
- `motte_sandbox/docker.py:52-62` 使用固定 `base/workspace`，存在就 rmtree；并发运行可删除另一容器正在使用的目录。
- `docker.py:198-212` 采集 artifact 时也未拒绝 symlink。

#### H-08 终态 SSE 与 server CLI 在 500 事件后静默截断

- `apps/api/app/main.py:991-1006` 每轮只发送 `pending[:500]`，看到终态后立即 return，没有排空剩余事件。
- snapshot 端点 `main.py:1016-1035` 最大 500 且不返回 has_more/next cursor。
- server CLI `motte_cli/runops.py:173-207` 只请求一页；local 则返回全部。
- 审查构造 526 个事件时，SSE/CLI 仅得到前 500 个。

#### H-09 Judge Web 与 API 契约完全断裂

- Web `apps/web/src/api/client.ts:1559-1588` 假定 GET JudgeSpec 目录及 `/judges/jobs*`、calibration 路由。
- API `apps/api/app/main.py:1167-1222` 实际只有 POST `/judges`、GET `/judges/{job_id}`、POST cancel。
- 浏览器实测 GET `/api/v1/judges` 返回 405，Judge 页面显示能力不可用。
- `JudgesPage.test.tsx` 只 mock client，因此 9 项组件测试全绿仍漏掉此缺陷。

#### H-10 Windows Uvicorn 下 Harness 目录返回 500

- `apps/api/app/main.py:1781-1789` 在请求协程内并发 inspect Claude/Codex。
- `motte_harness/process.py:35` 调用 `asyncio.create_subprocess_exec`。
- 本轮在 Windows 真实 `make dev` 下 GET `/api/v1/harnesses` 返回 500，堆栈为 event loop `NotImplementedError`；TestClient 测试未覆盖 Uvicorn 的事件循环差异。

#### H-11 Provider 会自动重放不确定 POST

- `transport.py:133-155` 对 408/429/5xx、Timeout、URLError、OSError 默认重试，默认重试次数为 2。
- 首个请求可能已经被服务端接收并产生费用/副作用，只是客户端在读响应时超时；审查已复现相同 body 被再次发送。
- 这与 M1/M4 的 indeterminate/no-auto-replay 约束冲突；只有具备 provider 幂等键或明确安全的请求才可自动重放。

#### H-12 Native tool 第二轮 history 编码错误

- `anthropic_messages.py:187-194` 与 `openai_responses.py:143-150` 只读取 `call['function']`。
- canonical tool call 实际是 `{id,name,arguments}` 的 flat shape（`normalization.py:7`）。
- 第二轮请求可发送 `name=None` 和空 arguments，导致多步 Agent 在特定 Provider adapter 下失败。

#### H-13 Malformed 200 响应绕过稳定错误证据

- Transport 对任意可解析 JSON 都视为成功；下游可把空 choices 当空成功输出，或抛裸 `AttributeError`。
- 失败不一定形成 `ProviderCallError`/provider call evidence，破坏错误分类与审计合同。

#### H-14 Anthropic stream adapter 跨调用共享可变状态

- stream adapter 在实例字段上保存 usage/stop reason；并发 generator 交错可让调用 A 读到调用 B 的 usage/终止原因。
- 审查已构造交错调用复现；状态应归属于单次 generator/request context。

#### H-15 Sandbox disk/output 限制并未真实强制

- `docker.py:91-93` 只限制容器 `/tmp`，但可写 host bind `/workspace` 不受 disk_mb 限制。
- logs 和 artifact 在切片/统计前会完整 materialize/read，缺少 artifact 总数/总字节上限，可耗尽宿主磁盘或内存。

#### H-16 ToolRegistry 的 mock/replay 仍执行真实 handler

- `motte_sandbox/tools.py:5-14` 仅 mode 精确等于 deny 时拒绝，其余 mode（含 replay、mock 和拼写错误）都会调用真实 handler。
- 审查已复现 replay/typo 产生真实副作用，违反 M5 的 deny-priority/fail-closed 要求。

#### H-17 ProcessRunner 在 task cancellation 时泄漏子进程

- `motte_harness/process.py:35-43` 只在 TimeoutError 清理，没有在 `asyncio.CancelledError`/finally 中终止 child。
- 审查取消一个 sleep child 后确认 PID 仍存活。

#### H-18 Direct LLM JSON evaluator 可被深层 JSON 触发 RecursionError

- `direct_llm_v2.py:417-468,562-574` 对深层 JSON 做递归 compare/canonicalization，没有深度预算。
- 约 2000 层数组可令未捕获 `RecursionError` 逃出单题稳定失败边界，形成拒绝服务。

### MEDIUM

1. **远程 Bearer Web 不可用**：API token 开启后要求 Authorization（`security.py:109-121`），Web request 没有 token 通道（`client.ts:136-140`），原生 EventSource 也不能加 header（`client.ts:694-724`）。
2. **旧 Compare 违反统一事实源**：Direct LLM/GSM8K 在浏览器端重算比较并把 unknown token 当 0（`DirectLlmCompare.tsx:24-95`、`Gsm8kCompare.tsx:18-105`）。
3. **CLI local/server report shape 不一致**：本轮同一 Direct LLM Run 的 server report 含 aggregation/cost/usage/benchmark，local report 缺失；核心分数一致但机器契约不是“仅 transport 不同”。
4. **CLI 模式环境变量 fail-open**：`motte_cli/remote.py:66-80` 不校验 `MOTTE_CLI_MODE`，拼错 `serer` 会进入 local。
5. **CLI 非法 JSON 错误不一致**：local 路径可打印完整 JSONDecodeError traceback，server 路径返回单行 `RUN_SPEC_INVALID`。
6. **PG begin/submit 幂等与 SQLite 不一致**：ExternalJob/ScoringJob 的同 key 并发或安全重试可在 PG 变为 conflict/UniqueViolation。
7. **PG M6 repository 绕过 canonical validation**：`pg_m6.py` 直接持久化 payload，Memory/SQLite 会调用 validator。
8. **SSE hook 终态不主动 close**：`useRunEvents.ts:72-97` 只设局部 closed，EventSource 到组件卸载才关闭。
9. **Run 总览/loading 与轮询竞态**：初始空数组即显示“暂无运行”，3 秒轮询可乱序覆盖新响应。
10. **BatchMonitor 吞异常并误报进度**：GET Run 失败后可永久保留 queued；failed/cancelled 且 cases 缺失时可错误显示 N/N。
11. **移动端布局不可用**：`index.css:78-97` 固定 sidebar，panel 最小宽 320（`183-189`），无 media query；窄屏仍常驻完整侧栏并裁剪主工作区。
12. **API/Web 类型治理不足**：M6/M7 多个公开路由用 raw dict 且无 response_model，Web 手写重复类型；Judge 契约事故表明 OpenAPI 门禁覆盖不到这些语义。
13. **Builtin Agent 取消/预算边界**：最后一次模型返回后未复查 cancel；NaN/负数/静默 int 截断可绕过 budget 比较。
14. **AGENT_CATALOG 文案与能力陈旧**：`apps/api/app/main.py:92-99` 仍称 builtin-react 未接 ExecutionBackend 并标 execution_ready=false，与本轮真实运行相矛盾。

## 5. 代码质量评价

### 优点

- 领域合同、版本、hash、revision、ScoringPass 与 evidence 边界总体清晰。
- 大量路径坚持 deterministic/offline-first，普通测试不会无意触发付费调用。
- Provider/Backend/Evaluator/Storage 分层明确，多数外部依赖均有 fixture/fake 路径。
- API、CLI、Web 已覆盖大量正向与边界用例；Web token discipline 和设计治理有自动化门禁。
- M7 对 SDK、CLI、迁移、备份、GC、安全和发布流程的覆盖面明显提升。

### 主要质量问题

- **异常吞噬过多**：关键 evidence、dispatch、settle、Web polling 失败被降级为继续运行或静默状态，破坏“未知保持未知”。
- **多后端语义不一致**：SQLite/Memory 有原子/validator 行为，PG 版本缺少锁、gap 幂等和 canonical validation。
- **集成测试层不足**：Web 大量 mock client；TestClient 无法覆盖真实 Uvicorn/Windows event loop；OpenAPI 不能覆盖手写 raw dict 契约。
- **共享模块过大**：`apps/api/app/main.py` 约 4948 行、`apps/web/src/api/client.ts` 约 1927 行，资源、运行、迁移、M6/M7 契约耦合在少数文件中。
- **安全与路径边界不一致**：dataset source 已禁止 redirect，但 Provider transport 未复用；Artifact put 有 containment，read/delete 没有同一 helper。
- **状态文档漂移**：M5/M6/M7 同一文件内同时存在 planned/not_run 和 complete/pass，审计者必须人工判断哪段 supersede 哪段。

## 6. 路线与文档一致性

1. `docs/roadmap/README.md` 与 `docs/PROGRESS.md` 对 M7 planned/implemented 的表述冲突。
2. `docs/verification/M7.md` 顶部和重复工作包仍写 planned/not_run，末尾却写 implementation_complete/offline_verified/external_pending。
3. M7 的三个真实替代场景被 readiness 单元测试标为 pass，但实际 Docker/Harbor/cutover 仍 blocked/not_run，应标 partial/offline。
4. “相对基线零新增失败”不等于路线要求的全绿门禁；本轮 Windows 全量仍有大规模失败。
5. M2 fixed Runner、M4 completion、M5/M6 终态均分散在后续文档，主账本未完全回填。
6. 原 stage roadmap 的未勾选复选框是需求快照，不应直接统计为当前完成度；需要机器可读的 requirement -> verification 映射。

## 7. 发布前优先级

### P0：立即修复并新增反例

1. Provider redirect：禁止跨 origin、禁止 HTTPS 降级、重定向时移除凭据；JSON/SSE 都测。
2. ArtifactStore：所有 put/read/delete 使用同一 resolved containment helper，拒绝 symlink 逃逸。
3. GC：显式遍历每类 repository 的真实 list API；对 pinned/needs_review 子实体 artifact 做删除反例。
4. Origin：严格比较 scheme + host + effective port，exact allowlist 优先。
5. Maintenance/backup：引入 owner/lease、互斥和在途 Worker drain/watermark，再生成同一时点 manifest。

### P1：功能与一致性

1. 修复 Judge Web/API 契约，并增加真实 create_app + HTTP 的前后端契约测试。
2. 修复终态 SSE/CLI 分页，返回 next/has_more，终态前排空全部事件。
3. 修复 PG ExternalJob/ScoringJob 锁与幂等；补双连接并发反例。
4. 为 DockerSandbox 分配 run/case 唯一 workspace，拒绝采集 symlink。
5. 修复 Windows Harness 探测；不要让目录页因一个 subprocess 探测 500。
6. 旧 Compare 改用服务端统一比较，unknown 不得补 0。

### P2：发布工程

- Web Bearer/SSE 认证方案、移动端导航、loading/error/竞态状态。
- CLI local/server 输出与错误契约完全对齐，非法 env fail-closed。
- 将公共 API 的 raw dict 改为 Pydantic request/response model，并让 Web 使用生成 schema。
- 完成 Docker Compose、真实 PG backup/restore、真实旧导出、M2/M3/M4/M5 外部 live 和三替代场景。

## 8. 最终判定

- **所有功能已开发完成：否。**
- **代码层是否大面积落地：是。**
- **离线验证是否充分：较充分，但存在测试盲区和平台失败族。**
- **是否可声明 stable_supported：否。**
- **是否可执行生产 cutover：否。**
- **下一里程碑建议：先完成 P0 安全/证据/GC/备份修复，再谈 RC 或切换。**

## 9. 修复后状态（superseding addendum）

本轮按原 finding 逐项修复并补充回归。以下状态替代第 4、7、8 节对已修复缺陷的当前判断；原文保留用于追踪发现依据。

| Finding | 修复后状态 | 核心证据 |
|---|---|---|
| C-01 Provider redirect 泄密 | `resolved_offline` | JSON/SSE redirect 限制为同 origin，拒绝 HTTPS 降级；跨 origin 与降级反例通过 |
| C-02 GC 漏引用 | `resolved_offline` | 显式遍历 Run 子仓库，子实体 artifact 引用和 `{id}` 形状纳入保留集；删除反例通过 |
| H-01/H-02 Artifact/Origin | `resolved_offline` | resolved containment、symlink/drive/`..` 拒绝；Origin 严格比较 scheme/host/effective port |
| H-03 Agent/Scenario 证据 | `resolved_offline` | 持久化错误不再吞掉；prepared/dispatching/settled 与 incomplete/quarantine 回归通过 |
| H-04/H-05 PostgreSQL 并发 | `resolved_offline` | ExternalJob 行锁/幂等冲突、ScoringJob recovery 锁与状态复核已实现；无 DSN，真实双连接仍 `external_pending` |
| H-06 maintenance/backup | `resolved_offline` | owner lease、互斥清除和同一 snapshot manifest 已实现；真实 PG backup/restore 仍 `external_pending` |
| H-07/H-15/H-16 Sandbox/Tool | `resolved_offline` | 唯一 staging、tmpfs、流式有界采集、artifact 限额、deny-by-default；Docker daemon 不可用，live 仍 pending |
| H-08 SSE/CLI 500 截断 | `resolved_offline` | snapshot 返回 `next_after/has_more`，API/SSE/Web/CLI 循环排空并拒绝不前进 cursor |
| H-09 Judge 契约 | `resolved_offline`，部分 browser 验证 | API 发布 JudgeSpec/calibration 目录；Web 改用真实 request shape、授权/幂等键；真实页面读取到 6 个 spec |
| H-10 Windows Harness | `resolved_offline` | asyncio subprocess 不可用时回退同步 Popen，单 harness 失败隔离；真实 Uvicorn API 曾成功启动 |
| H-11-H-14 Provider 行为 | `resolved_offline` | 无幂等键不重放 POST、200 shape 校验、canonical tool history、stream per-call state 回归通过 |
| H-17 ProcessRunner cancellation | `resolved_offline` | cancellation 终止并等待 child tree；Windows 外部 Job token 核验、整树中断和恢复链路通过 |
| H-18 深层 JSON | `resolved_offline` | 迭代深度预算 128，稳定返回 `max_depth_exceeded` |

同时完成关键 MEDIUM 项：API stable error envelope、Harness 单项隔离、Builtin Agent 能力目录、CLI mode fail-closed、local/server report 共用 canonical builder、maintenance owner CLI、Web Bearer + authenticated fetch-SSE、compare stale request 取消、busy/error 状态和移动端导航。Judge 的零副作用预检现在携带由发布 spec 预算推导的有界 preview authorisation，避免服务端 `_compile()` 以 422 拒绝，同时最终提交仍需要显式 UI 确认和幂等键。Judge 目录复用提交期 provider snapshot resolver，只暴露 enabled 且支持 completion 的资源；终态 `settled/failed/cancelled/indeterminate` 作业可显式重置为下一次请求。

Windows 全量失败族还修复了三项跨平台根因：`TrustedDir` 的 Windows containment backend、Harbor frozen copy 的二进制写入，以及 Runner 环境缺失 `SystemRoot` 导致 Winsock 初始化失败。外部 Job 取消竞态现在不会在终态后继续 transition 或抛后台线程异常。

更新后的发布判断：**已审查的 CRITICAL/HIGH 与关键 MEDIUM 缺陷均有实现和回归证据，但仍不能声明 `stable_supported` 或 `cutover_ready`**。剩余阻断是完整非 live 门禁的 14 项环境相关失败（13 项本机 symlink privilege、1 项 Docker cleanup 观测）、Docker/Harbor live、真实 PostgreSQL 并发与 backup/restore、Judge 人工校准、真实外部 Harness/官方数据和正式 cutover 演练；详见实际测试报告。
