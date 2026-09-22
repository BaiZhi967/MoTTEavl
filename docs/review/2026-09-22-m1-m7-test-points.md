# MoTTEavl M1–M7 详细测试点文档

- 编制日期：2026-09-22（UTC+08:00）
- 适用基线：`main` @ `01d347681d5ff88ee2bfd9d83cc015f6d2b7cef3`（M1–M7 当前主干；完成度必须按实现/离线/live/stable/cutover 分层判定）
- 配套文档：《M1–M7 代码与功能开发审查报告》（`docs/review/2026-09-22-m1-m7-code-feature-review.md`）与《M1–M7 实际测试报告》（`docs/review/2026-09-22-m1-m7-test-report.md`）
- 编制依据：`docs/ROADMAP.md` 总路线、`docs/roadmap/M1–M7` 七份阶段规划（含全部 G/T/A 编号）、各阶段验证记录与多轮 review 反例、本次代码审查发现

## 使用说明

**测试层级**

| 层级 | 缩写 | 含义 |
|---|---|---|
| 自动化 | A | 已有 pytest/vitest 自动化用例（标注代表文件；执行回归即可） |
| 手工-API | H-API | curl / OpenAPI 客户端直接调 API |
| 手工-CLI | H-CLI | `uv run python -m motte_cli …` |
| 手工-Web | H-Web | 浏览器操作 `apps/web` |
| 集成/环境 | I | 需真实 SQLite/PG/Docker/子进程环境 |
| live（付费） | L | 真实模型/Runner，需显式授权与预算，目前多数 not_run |

**优先级**：P0 = 发布阻断/核心正确性；P1 = 重要功能；P2 = 一般回归；P3 = 低频边界。

**环境提示**（superseding 实测）：修复后 Windows 完整 `uv run pytest -q -m "not live"` 为 `2860 passed / 14 failed / 97 skipped / 1 deselected`（19m16s）。剩余 14 项已逐条分类：13 项因本机缺 Windows symlink privilege，在夹具创建链接时以 `WinError 1314` 终止；1 项因 Docker 不可用使 Harbor cleanup 保守返回 `unknown`。最终修复合并回归另为 `92 passed / 1 skipped`。全量门禁仍按 FAIL，不把环境阻断写成通过；Linux CI（含真实 PG service）也仍是正式门禁。付费 live 测试必须显式启动。

---

## 第 0 部分：全局横切测试点（所有阶段共用）

| ID | 测试点 | 方法 | 优先级 | 预期/备注 |
|---|---|---|---|---|
| G-001 | 离线门禁全绿：`uv run pytest -q -m "not live"` + `ruff check .` + `mypy packages/contracts` + `pnpm --dir apps/web test` + `build` + `make check` | I | P0 | Linux/CI 为权威；Windows 需与基线做精确 node-id 差分（新增失败=0） |
| G-002 | 主干 CI（Linux + 真实 PG）全绿，含 3 项 M5 遗留失败的当前状态确认 | I | P0 | `test_fixture_lifecycle` / `test_scoring_jobs[PG]` / `test_trials_downgrade_guard[PG]` |
| G-003 | OpenAPI 漂移门禁：`make openapi` 后 `make openapi-check` 零 diff；`schema.d.ts` 同步 | A | P0 | 已有 CI 门禁；注意手写路径字符串不被覆盖（见 M5-TP） |
| G-004 | 零付费默认：普通测试/GET/report/compare/gate/export 不产生任何模型/Runner 付费调用（opener 调用计数=0） | A | P0 | 既有断言模式，新功能沿用 |
| G-005 | 凭据不泄漏：API key 不出现在 envelope/Run/CaseRun/TraceEvent/SSE/CLI/日志/工件/导出任何位置 | A+H-API | P0 | 多处既有断言；新增链路必须扩展 |
| G-006 | 三端一致性：同一 Run 在 API/CLI/Web 的状态、分数、事件序列一致 | A+H-* | P1 | M6 四新页已走统一服务；GSM8K/DirectLlm 旧 compare 页是已知例外 |
| G-007 | 迁移链健康：alembic 线性链校验、空库 upgrade→幂等复跑→downgrade→re-upgrade（SQLite+PG） | I | P0 | 0014 已登记；新 migration 必须入链 |
| G-008 | 取消语义全局：取消阻断后续调用、终态不复活、迟到数据仅审计 | A | P0 | 各套件共用 RunService 语义 |
| G-009 | 崩溃恢复：Worker kill 后重启，interrupted Run 回收/续跑/needs_review 不重放 | A | P0 | 子进程级测试已有 |
| G-010 | 历史不可变：rescore 追加新 pass、旧 pass/已发布资源不变、GET 零副作用 | A | P0 | |
| G-011 | 中文 UI 状态唯一来源：徽章/时间线/过滤下拉全部从 `STATUS_META` 派生；样式只用 `var(--…)` token | H-Web | P2 | AGENTS.md 设计宪法门禁 |
| G-012 | SSE 通用：`?after=`/Last-Event-ID 断线恢复、心跳、终态关闭、严格 envelope | A+H-API | P1 | |
| G-013 | compose config 通过（有 docker 的环境补 build/up） | I | P1 | 本机无 docker 时如实 not_run |
| G-014 | Provider redirect 凭据边界：同 origin 可按策略跳转；跨 host/port、HTTPS→HTTP 必须拒绝或剥离 Authorization/x-api-key；POST 与 SSE 均覆盖 | A（双本地 origin） | P0 | 当前 CRITICAL，禁止真实密钥参与测试 |
| G-015 | Provider 不确定 POST：服务端已收 body 后客户端 Timeout/断线，不得无幂等键自动重放；费用/副作用保持 indeterminate | A | P0 | 覆盖 408/429/5xx/Timeout/URLError/OSError |
| G-016 | Origin 严格同源：scheme、hostname、effective port 全相同才允许；同 host 异端口/异协议 403；exact allowlist 可显式放行 | A+H-API | P0 | 当前 hostname-only 可绕过 |
| G-017 | ArtifactStore put/read/delete 统一 containment：absolute、..、root 内 symlink、父目录 swap 均不得读删 root 外文件 | A | P0 | delete 也必须 fail-closed |
| G-018 | GC 完整引用图：Run/CaseRun/Attempt/Invocation/ExternalJob/ScoringPass/Baseline/pin/needs_review 任一引用均阻止删除 | A+I | P0 | dry-run 与 apply 均验证；引用仓库 API 不得静默跳过 |
| G-019 | Maintenance/backup/GC 互斥：owner/lease、重复 begin、错误 owner end、并发 backup+GC、异常释放均受控 | A+I | P0 | 不允许先结束者提前解除另一操作屏障 |
| G-020 | 一致备份等待在途 Worker drain/watermark；DB snapshot、artifact refs、counts 和 manifest 必须来自同一逻辑时点 | I（真实多进程+PG） | P0 | 恢复后逐 hash/计数对账 |
| G-021 | 证据 sink 故障注入：event/invocation prepare/dispatch/settle 任一步落库失败时 coverage 不得 complete，真实副作用必须 needs_review/indeterminate | A+I | P0 | Builtin/Pi/Scenario 全覆盖 |
| G-022 | SSE >500 事件：live stream 在终态前排空；snapshot 提供 next/has_more；SDK/Web/server CLI 最终序列完整且 last event 为终态 | A+H-* | P0 | 526/1001 事件边界 |
| G-023 | PostgreSQL ExternalJob/ScoringJob 双连接并发：checkpoint 不丢更新、同 key 同 fingerprint 幂等、不同 fingerprint 稳定 conflict、prepared recovery 不误标 indeterminate | I（真实 PG） | P0 | 与 SQLite/Memory 逐字段对账 |
| G-024 | DockerSandbox 双实例并发 workspace 唯一；一方 prepare/cleanup 不影响另一方；artifact symlink 拒绝 | I（Docker） | P0 | 需要真实并发容器 |
| G-025 | Sandbox 资源强制：/workspace 写入受 disk_mb 限制；日志/工件按流式上限采集，数量和总字节超限稳定失败且不耗尽宿主 | I（Docker） | P0 | /tmp 限制不能替代 workspace 限制 |
| G-026 | ToolRegistry deny-priority：deny/mock/replay/未知/拼写错误模式均不得意外执行真实 handler；仅明确 execute 模式可有副作用 | A | P0 | 每种模式用副作用计数器断言 |
| G-027 | Native tool 第二轮 history：OpenAI Responses/Anthropic flat canonical call 的 id/name/arguments 完整 round-trip | A | P0 | 至少两轮 read→write 工具链 |
| G-028 | Provider malformed 200 与并发 stream：空/错 choices 转稳定 ProviderCallError；并发调用 usage/stop reason 不串扰 | A | P0 | 不允许裸 AttributeError |
| G-029 | ProcessRunner task cancellation 必须终止 child/process group，等待后 PID 不存活、清理证据完整 | A+I | P0 | 同时覆盖 timeout 与 CancelledError |
| G-030 | Direct LLM JSON 深度/大小预算：超深数组/对象稳定映射 evaluator error，不抛 RecursionError 或拖垮 Worker | A | P0 | 100/1000/2000 层边界 |
| G-031 | Web/API 真契约测试：使用真实 create_app/OpenAPI HTTP，不 mock client；覆盖 Judge、Harness、Runtime command 409 envelope | A+I | P0 | Judge GET/submit/get/cancel/calibration 路由逐项一致 |
| G-032 | Web Bearer 模式：普通 fetch 与 SSE 均可认证；401/403 提示明确；不在 URL、日志、localStorage 明文泄漏 token | A+H-Web | P0 | 远程部署强制 |
| G-033 | Web 响应式/可访问性：375/768/1440 无内容裁剪和横向不可达；导航可折叠；键盘焦点、aria、axe 基线 | H-Web+视觉回归 | P1 | 当前固定 sidebar/panel 不满足 375px |
| G-034 | Web loading/error/竞态：首屏 loading 不误报 empty；迟到轮询不覆盖新响应；BatchMonitor/getRun 失败可见且不伪造 N/N | A+H-Web | P1 | 慢请求/断网/恢复矩阵 |

---

## 第 1 部分：M1 原生 Agent 与通用评分

### 1.1 契约与评分器（M1-T01/T02/T03）

| ID | 测试点 | 方法 | 优先级 | 预期/备注 |
|---|---|---|---|---|
| M1-001 | FrozenObservation/MetricResult/InvocationRecord 契约校验：字段不变量、canonical JSON 拒绝 NaN/Infinity、旧 Observation 读取兼容 | A（`tests/contract/test_observation_v1.py`） | P0 | |
| M1-002 | 九类评分器逐类行为：exact/contains/regex/json-schema/file-exists/file-content/exit-code/tool-call/no-forbidden-write 的通过/失败判定 | A（`tests/evaluators/test_observation_metrics.py`） | P0 | |
| M1-003 | 缺证据/无期望/异常不映射为通过；非 scored 状态不得携带 passed/value（契约层） | A | P0 | G12 核心 |
| M1-004 | 逐指标异常隔离：单个评分器崩溃不影响其他指标产出 | A | P0 | |
| M1-005 | regex/json-schema 子进程可终止（灾难回溯/超限输入按 `input_too_large` 拒绝而非挂死） | A | P1 | |
| M1-006 | 多指标 ScoringPass：复合键落库（migration 0004）、rescore 新 pass、旧 pass 不变、GET 零副作用、离线重评分零模型调用 | A（`tests/storage` + `tests/sdk`） | P0 | |
| M1-007 | 远程 $ref JSON Schema 阻断（无网络解析） | A | P1 | |

### 1.2 Agent 后端与消息循环（M1-T04/T05）

| ID | 测试点 | 方法 | 优先级 | 预期/备注 |
|---|---|---|---|---|
| M1-008 | builtin-agent@1 注册可用；`/api/v1/agents` 目录返回与事实一致（当前文案过时，见 M1-032） | H-API | P1 | 修复后验证 execution_ready=true |
| M1-009 | native-tool 模式在模型 `supports_tools=false` 时创建期拒绝（422，付费调用=0） | A+H-API | P0 | |
| M1-010 | manifest 冻结：模式/prompt 版本/工具名集合/预算/模型快照进入 frozen manifest | A | P0 | |
| M1-011 | Case 级 workspace 与消息历史隔离：同 Run 跨 Case 无串扰（同路径名文件不互见） | A（A12 场景） | P0 | |
| M1-012 | 完整 assistant/tool 历史：工具错误回灌后恢复（A02）、未知工具拒绝执行（A03）、重复 call_id 拒绝二次执行 | A | P0 | |
| M1-013 | 预算生效：时长/步数/工具次数/token/费用各自触顶有具体停止原因；无 usage 不虚构硬预算（A13） | A | P0 | |
| M1-014 | 单次调用期限可靠结算：超时后迟到线程结果只丢弃不二次结算 | A（review 轮反例） | P1 | |

### 1.3 Workspace、调用日志与采集（M1-T06/T07/T08）

| ID | 测试点 | 方法 | 优先级 | 预期/备注 |
|---|---|---|---|---|
| M1-015 | workspace 路径安全：相对路径校验、symlink/TOCTOU 拒绝（POSIX）、设备文件拒绝、读写配额、快照与残留报告 | A（POSIX；Windows 便携分支单独验证） | P0 | Windows 下 symlink 用例 skip——需补重解析点拒绝测试 |
| M1-016 | cleanup 恒执行：成功/失败/超时/策略违规路径都清理；目录链归属复核（swap 攻击防护） | A（`test_review_round3/4/5_fixes.py`） | P0 | |
| M1-017 | 调用日志三态迁移：prepared→dispatching→settled + revision CAS；崩溃后 dispatching → needs_review（A09）；agent 禁 second-chance | A（migration 0005 + interruption 测试） | P0 | |
| M1-018 | 取消：模型调用中取消阻断后续 case、进程清理结果可查、取消原因记录 | A（A08） | P0 | |
| M1-019 | 冻结采集：evidence_hash 稳定、coverage 降级显式、事件上限 500 + 截断标记、值形状脱敏（事件/SSE/摘要层） | A | P0 | |
| M1-020 | 模型输入隔离：prompt 不含 gold/隐藏断言/评分规则/本机凭据 | A | P0 | |

### 1.4 用户链路与集成（M1-T09/T10）

| ID | 测试点 | 方法 | 优先级 | 预期/备注 |
|---|---|---|---|---|
| M1-021 | Agent 套件四页（操作/监控/结果/并列阅读）：创建/取消/retry 子 Run/历史 pass 切换/工件阅读 | H-Web + A（`AgentPages.test.tsx` 15 用例） | P1 | |
| M1-022 | CLI agent-tasks 子命令组与 API 同一事实源 | H-CLI | P1 | |
| M1-023 | 假 HTTP + 真实 API/Worker/SQLite 三组任务闭环（正常产物/可恢复错误/越权预算耗尽）+ 旧套件（Direct LLM v1/Replay）回归不变（A15） | A（`test_native_agent_slice.py`） | P0 | |
| M1-024 | 真实 Docker（一次性 PG + alembic 0001→0005）集成 | I | P1 | 历史记录 19 passed |
| M1-025 | live（真实 subject 模型）：完整任务/取消/预算触顶/needs_review 各一组，记录费用与证据 | L | P0 | **partial**：本轮 DeepSeek native-tool `acc-001` 正常任务通过；取消/预算触顶/needs_review、第二模型和费用对账仍未执行，M1-Supported 未满足 |
| M1-026 | 轨迹截断不误判禁写（"写入后恢复"轨迹判定，review 反例） | A | P1 | |
| M1-027 | 磁盘/配额失败时清理证据完整（A07） | A | P1 | |
| M1-028 | 单评分器异常时其余指标仍可用（A10） | A | P1 | |
| M1-029 | `var/agent-workspaces`/`var/artifacts` 相对 CWD 行为：从非仓库根启动时不静默写别处 | H-CLI | P2 | 审查发现默认值相对路径 |
| M1-030 | usage 字段（prompt_tokens 等）不被键名脱敏误伤（可观测性） | H-API | P2 | 审查发现 redact 过度 |
| M1-031 | Windows 便携 workspace 分支：junction/重解析点拒绝、无 fd 保护下越权写拒绝 | I（Windows） | P2 | 当前无测试覆盖 |
| M1-032 | `/api/v1/agents` 目录文案修复后回归（execution_ready 与注册表一致） | H-API | P2 | 当前文案过时（P2 债务） |

---

## 第 2 部分：M2 LLM Benchmark 与 C-Eval

### 2.1 Job 协议、句柄与持久化（M2-T01/T02/T03）

| ID | 测试点 | 方法 | 优先级 | 预期/备注 |
|---|---|---|---|---|
| M2-001 | JobSpec/Handle 契约：占位值拒绝、execution mode 冲突拒绝、一 Run 一 Job（不逐题重启 Runner） | A（`tests/contract/test_external_job.py`） | P0 | |
| M2-002 | launch_token：argv 注入、PID+token 双因子所有权、跨会话恢复核验（可验证→观察不重启；不可验证→indeterminate） | A（`test_external_job_lifecycle.py`，POSIX） | P0 | A07 场景 |
| M2-003 | 进程治理：TERM→宽限→KILL 进程组；非零退出保留部分结果（A08）；可信完成标记 `.motte-job-complete` | A | P0 | |
| M2-004 | 持久化幂等：导入三段键 `(job_id, source_record_key, parser_version)`；同键同内容 no-op、异内容 conflict 进账本不覆盖（A06） | A（`test_external_job_store.py`） | P0 | |
| M2-005 | 终态写入 CAS：cancelled 保护不复活；迟到结果仅审计（`import_late_results`） | A | P0 | |
| M2-006 | 崩溃恢复：checkpoint 先行、frozen-artifact 恢复保留 evidence 引用、恢复后采集幂等（A09） | A | P0 | |
| M2-007 | 取消：持久取消请求、观察循环消费、取消终态不复活（A09） | A | P0 | |
| M2-008 | 证据冻结链：字节→内容寻址 Artifact→解析；超预算 `EVIDENCE_INCOMPLETE` 在导入/评分前拒绝；解析前后 hash 不一致 `EVIDENCE_INCONSISTENT` | A | P0 | |
| M2-009 | 空输出/仅聚合不伪造样本级结果（A04：`EXTERNAL_EMPTY_RESULTS`）；部分结果保留 partial disposition（A05） | A | P0 | |

### 2.2 数据、Runner 与 Parser（M2-T04/T05/T06）

| ID | 测试点 | 方法 | 优先级 | 预期/备注 |
|---|---|---|---|---|
| M2-010 | Catalog 四态：registered/prepared/runnable/verified + blockers 原因；未备即拒且 0 调用（A01） | A+H-API | P0 | |
| M2-011 | 来源治理 fail-closed：官方路径无受信核验器/许可证据即 `OFFICIAL_APPROVAL_UNVERIFIED`；逐行 has_gold；无 gold 不冒充（A12） | A | P0 | |
| M2-012 | 数据集身份：同 revision 事务级幂等/冲突；fingerprint 伪造 fail closed | A | P0 | |
| M2-013 | 凭据边界：配置只收 `env:` 引用、原值拒绝；模型构造时才解析 env；`configs/*.py` dump 不进永久 Artifact | A | P0 | |
| M2-014 | Runner 配置：参数 allowlist + ceiling、few-shot 分区校验、retry 三分、原生配置钉 0.4.2 | A | P0 | |
| M2-015 | 上下文预检：静态预检（无模型探针）0 调用；逐题渲染取 max 的上下文预算 | A | P1 | |
| M2-016 | Parser 对照：PARSER_VERSION @2、52 学科→四类+Hard 断言、两段式结论优先提取逐字迁移、legacy+时间戳双布局、多实验拒绝、多模型拒绝 | A（`test_ceval_parser_parity.py`，5 用例） | P0 | fixture 为合成内容 |
| M2-017 | infer-only 不造准确率；discrepancy 不覆盖；selected-case accuracy/coverage/unscored 分母正确 | A | P0 | |
| M2-018 | 新旧 Parser 双口径：runner-native 指标与平台 diagnostic 指标分开存储命名显示（A11）；`observation_gaps=["request-identity","usage-and-cost"]` 如实登记 | A+H-Web | P1 | |
| M2-019 | 冻结预算：单文件 8 MiB/总量 64 MiB 超限拒绝（fail-closed 不崩溃） | A | P1 | full Profile 真实数据未校验过 |
| M2-020 | 实验目录指针：多候选 latest-mtime 误选风险（混入无关目录时行为） | A/H | P3 | 审查发现启发式弱点 |

### 2.3 公共入口、Web 与 M6-Lite（M2-T07/T08/T09）

| ID | 测试点 | 方法 | 优先级 | 预期/备注 |
|---|---|---|---|---|
| M2-021 | 受控 adapter 注册：`var/runner/adapters.json` 三处（API/Worker/CLI）一致；无配置时 `RUNNER_NOT_CONNECTED` fail-closed | A+H-CLI | P0 | |
| M2-022 | 请求/提交代号异步保护（防双击重复提交） | H-Web + A | P1 | |
| M2-023 | Web 五页（请求/提交/题目/过程/结果）：scope 常显、native/diagnostic 双栏、not_attempted 不消失、gate-blocked 逐规则 | H-Web + A（`ExternalBenchmarkPages.test.tsx`） | P1 | |
| M2-024 | 禁用外部 adapter 不影响 Direct/GSM8K/Replay 与历史读取（A15） | A（`test_ceval_external_job.py`） | P0 | |
| M2-025 | M6-Lite：同集同 Profile 覆盖与阈值判断；口径差异返回具体原因（A10）；同一 Gate 实现（无私有引擎） | A（`test_m2_review_e2e_sample.py`：selected=4/attempted=3/correct=2/coverage=0.75/Gate 不通过） | P0 | |
| M2-026 | CMMLU 独立链路：67 学科、独立 Profile 身份、无四类聚合；ceval 语义不放松反例 | A（`test_cmmlu_external_profile.py` + `test_cmmlu_independent_chain.py`） | P1 | |
| M2-027 | e2e 样例数值与 roadmap fixture 逐项对齐 | A | P1 | |
| M2-028 | native 指标 UI 只显示前 8 条的边界确认（52 学科全量对账走 checkpoint/artifact） | H-Web | P3 | 已知 Lite 信息密度 |

### 2.4 live 与环境（M2-T10/T11）

| ID | 测试点 | 方法 | 优先级 | 预期/备注 |
|---|---|---|---|---|
| M2-029 | 真实固定 Runner 0.4.2 + 本地确定性 HTTP 端点回归（12 passed 历史记录复跑） | I | P1 | macOS arm64 验证过；Linux/CUDA 需重验 |
| M2-030 | 真实模型 smoke / full Profile（1346 题）：冻结预算、费用、样本级报告 | L | P0 | **当前 blocked/not_run** |
| M2-031 | 官方数据获取 + 许可核对（受信核验器部署后） | L/I | P0 | 获取器未实现，人工路径 |
| M2-032 | CMMLU 真实 Runner/模型验收（不继承 C-Eval 通过状态） | L | P1 | not_run |
| M2-033 | PG 并发语义（同键并发导入、冲突账本） | I（`MOTTE_PG_DSN` 门控） | P1 | 本机 skip，CI 覆盖 |
| M2-034 | Windows 降级路径或显式声明（`trusted.py` 修复后回归全量） | I | P1 | 当前 POSIX-only（审查 P1 发现） |
| M2-035 | `M2.md` 收尾清单回填后与 R4-fixes 文档一致性核对 | 文档 | P2 | 文档漂移 |

---

## 第 3 部分：M3 Harbor 与 Terminal-Bench

### 3.1 身份、计划与存储（M3-T01/T02）

| ID | 测试点 | 方法 | 优先级 | 预期/备注 |
|---|---|---|---|---|
| M3-001 | TaskIdentity 稳定：source_id+dataset_revision+规范化路径+内容 hash；同 basename 不同来源不误并（A02）；反序列化重算比对（篡改即错） | A（`tests/benchmarks` identity 集，POSIX） | P0 | |
| M3-002 | 任务准备只读：拒路径越界、拒未钉住 revision、准备限额、不执行任意仓库脚本 | A | P0 | |
| M3-003 | Trial=计划内重复：repeat_index 只来自事前计划；传输/操作员 retry 不产生 Trial（专项测试） | A | P0 | |
| M3-004 | Trial 存储：identical/conflict/conflict 保留先写；占位可被真实证据替换、真实证据间不覆盖；终态后到达走 terminal-import 只增不改 | A | P0 | |
| M3-005 | migration 0008：降级有数据即具名拒绝；**既有部署旧库就地升级**（缺列补齐后可结算——F-01 教训回归） | A/I | P0 | 曾漏测：测试必须覆盖"既有库"而非只建新库 |
| M3-006 | PG 对齐：ON CONFLICT 原子首写 + FOR UPDATE | I（PG 门控） | P1 | |

### 3.2 配置、Parser 与环境（M3-T03/T04/T05）

| ID | 测试点 | 方法 | 优先级 | 预期/备注 |
|---|---|---|---|---|
| M3-007 | 原生配置 allowlist：execution_option_plan 逐字段 map-or-reject；原生 retry 整体拒绝不叠加；environment_build_sec 显式拒绝；effective==requested 断言 | A | P0 | |
| M3-008 | 凭据仅 `env:` 引用；Job 缺期限拒启动 | A | P0 | |
| M3-009 | reward 四态：有效通过/有效失败（reward=0）/缺失（证据不足）/畸形（协议错误）；**Verifier 异常先于 reward 判定**（A04）；双来源矛盾=协议错误 | A | P0 | |
| M3-010 | 只有 result.json 汇总无原始 reward 文件=证据不足；不报质量通过（A03：进程正常退出但任务失败） | A | P0 | |
| M3-011 | 同名 Task（HARBOR_TASK_NAME_AMBIGUOUS）拒合并；路径优先归属；unmapped/unplanned 显式登记；全计划 Trial 都有行（含零产出，A08） | A | P0 | |
| M3-012 | usage unknown 不填 0（A13 费用不可观察为 null） | A | P0 | |
| M3-013 | 环境 preflight fail-closed：model_calls=0/task_starts=0、网络/Verifier 可见性未验证即拒、自定义 Profile 指纹 | A | P0 | |
| M3-014 | Compose 深检：间接资源/external/driver_opts 拒绝、宿主来源含 `$` 一律拒、env_file 仅字面 KEY=value | A（18 用例） | P0 | |
| M3-015 | 恶意任务包：凭据目录/宿主资源/Docker socket 不可达（A13） | A+I | P0 | |

### 3.3 执行、清理与评分（M3-T06/T07/T08）

| ID | 测试点 | 方法 | 优先级 | 预期/备注 |
|---|---|---|---|---|
| M3-016 | Harbor adapter：prepare 零执行 + 物化不可变任务副本；导入键 `task_key#repeat_index`；外层文件 TrustedDir 受控读取；二进制走 binary_sink | A（POSIX） | P0 | |
| M3-017 | Runner 桥：启动前定位文件、TERM/INT handler、`--validate-config` 零执行、缺凭据/漂移退出码 5、同名 Job 目录只复用不重跑 | A+I | P0 | |
| M3-018 | 取消清理：容器冲突先行独立判定、拒冲突 stop、daemon 不可达报 unknown；容器状态写入资源账本；只清理本 Run 资源（A09） | A+I（Docker oracle） | P0 | |
| M3-019 | 崩溃恢复只观察不重启（wrapper 崩溃恢复 never restarts） | A | P0 | |
| M3-020 | 证据 hash 冲突不覆盖（A10）；轨迹截断诚实降级（A11） | A | P0 | |
| M3-021 | 评分：质量/覆盖分母分开（3 Trial=1 通过/1 有效失败/1 Verifier 错误 → 1/2、2/3、门禁不放行）；first-trial 按冻结 repeat 序不顺延；per_success_basis 成本 | A（`test_harbor_metrics.py`） | P0 | roadmap §8 合成样例 |
| M3-022 | `valid_trial_pass_rate`/`valid_trial_coverage` 注册进 Gate 指标注册表 | A | P1 | |
| M3-023 | 重评分不重跑环境（A15）；配置变化改 manifest 不改历史（A14） | A | P1 | |

### 3.4 工作区与验收（M3-T09/T10）

| ID | 测试点 | 方法 | 优先级 | 预期/备注 |
|---|---|---|---|---|
| M3-024 | API：prepare/preflight/创建（DTO 校验先于 repository）、Task/Trial 下钻、Trial 详情（内容脱敏身份保留）、工件字节/内容（文本默认脱敏、二进制默认拒 `ARTIFACT_RAW_EXPORT_DISABLED`） | A+H-API | P0 | |
| M3-025 | Web 五页：Task 列表/Trial 切换/轨迹/工件/评分下钻（32 用例） | H-Web + A | P1 | |
| M3-026 | 层 2 真实 Docker oracle 校准（pass-fail-2x2/errors-timeout fixture）在目标部署环境复跑 | I | P1 | 仅开发方 macOS 执行过 |
| M3-027 | 层 3 真实模型小批次 + 上游任务集（89 题 terminal-bench 2.0 / 10 题 sample） | L+I | P0 | **当前 blocked/not_run** |
| M3-028 | 兼容矩阵按 dataset×agent×environment 逐条与实跑记录一致（claude-code CLI 2.0.30 真实兼容性核验） | I/L | P1 | 从未独立核验 |
| M3-029 | linux/amd64、受限网络、Verifier 可见性变体 | I | P2 | not_run |
| M3-030 | Windows 原生（`trusted.py` 修复后）：identity/compose/frozen 全量回归 | I | P1 | 当前 90 项同因失败 |

---

## 第 4 部分：M4 Pi 与外部 Harness

### 4.1 Runtime 契约与兼容（M4-T01/T02）

| ID | 测试点 | 方法 | 优先级 | 预期/备注 |
|---|---|---|---|---|
| M4-001 | RuntimeDefinition 契约：config_schema allowlist、model/tool_control 分支校验（not-enforced 需显式确认）、interactive 一致性、RuntimeVersion 仅 published 不可变 | A | P0 | |
| M4-002 | 未知 runtime 设置字段拒绝；预算拒 bool/NaN/inf/负数；credential_refs 仅档案名 | A | P0 | |
| M4-003 | 三态分层：未安装/版本漂移/缺认证三因可区分且创建前失败（A01）；execution_ready 永不由 --version 推出 | A | P0 | |
| M4-004 | 兼容清单缺失/损坏 fail closed；零成本真实探测 | A | P0 | |
| M4-005 | spawn 前版本门：漂移即拒（RUNTIME_VERSION_DRIFT） | A | P0 | |
| M4-006 | compatibility.json 与实际 argv 一致性（当前 6 个隔离 flag 未登记——修复后回归） | A/文档 | P2 | 审查发现漂移 |

### 4.2 Pi bridge 与平台 Run（M4-T03/T04/T05）

| ID | 测试点 | 方法 | 优先级 | 预期/备注 |
|---|---|---|---|---|
| M4-007 | bridge 协议 v2：帧边界/UTF-8/超长行/stderr 64KB 脱敏/身份/seq 单调/未知事件 fail closed/取消杀树 | A（`test_pi_bridge_hardening.py` 14 用例 + bridge selftest） | P0 | |
| M4-008 | Pi 工具沙箱：相对路径 only/`..`/盘符/symlink 逐组件校验、配额写前检查；无 SDK 禁 echo（A02） | A | P0 | |
| M4-009 | 预算执行前阻断（非事后通知）：max_steps/max_tool_calls | A | P0 | |
| M4-010 | usage 诚实：原生计量只在字段可辨时累加，scripted 恒不伪造 | A | P0 | |
| M4-011 | 每 CaseAttempt 全新 session/operation/workspace（A03 无交叉污染）；显式续跑才绑旧 session | A | P0 | |
| M4-012 | 停止未确认 → RUNTIME_STOP_UNCONFIRMED 隔离并保留工作区（先于产物冻结） | A（`test_target_stop_confirmation.py`） | P0 | |
| M4-013 | supervisor：双管道并行有界消费无死锁（大 stderr 慢消费者，A07）；单行/累计/idle/total 全有界 | A（`test_supervisor_safety.py`，Windows Job Object 用例） | P0 | |
| M4-014 | Windows 进程所有权：挂起→Job Object→resume 无 PID 竞态；所有权失败绝不枚举后代 | A | P0 | |
| M4-015 | 原生 interrupt（CTRL_BREAK/组 SIGINT）→宽限→psutil 杀树；残留检测 + PID 复用双因子核对 | A | P0 | |
| M4-016 | session 恢复只 observe/needs_review、replayed 恒 false；Worker 崩溃不重放（A09） | A（`test_runtime_recovery.py`） | P0 | |
| M4-017 | 无终态不自动重放；缺 final/产物不自动通过（A08）；退出 0 ≠ 质量通过 | A | P0 | |
| M4-018 | flaky 用例稳定性：appserver approve/reject 与 batch-stdin EOF 在混合运行时稳定通过 | A | P1 | 审查发现 3 例时序敏感 |

### 4.3 Claude/Codex batch 与交互命令（M4-T06/T07/T10）

| ID | 测试点 | 方法 | 优先级 | 预期/备注 |
|---|---|---|---|---|
| M4-019 | Claude batch：pinned argv、JSON 解析未知 schema/subtype→insufficient 不假成功、usage/cost 只取原生回报 | A（`test_cli_run_backend.py` fake binary） | P0 | |
| M4-020 | 非零退出/截断/非 exited 让终态离开 final；原始 stdout/stderr 冻结+raw_ref | A | P0 | |
| M4-021 | Codex batch：exec --json、官方顶层 type 形态、终态后事件/跨 thread/重复 turn→insufficient、cost 恒 unknown 不填 0 | A | P0 | |
| M4-022 | 每次执行独立临时 native home；strict 政策拒绝 partial 可复现性；秘密值从输出/产物/argv/快照逐层替换 | A（`test_cli_evidence_safety.py`） | P0 | |
| M4-023 | Git 快照禁外部 diff/textconv/fsmonitor；filter 配置存在即放弃 worktree hash | A | P1 | |
| M4-024 | motte-settings.json 不进 Case 产物快照（before/after diff 只含 agent 实际修改） | A | P1 | 审查发现污染（修复后回归） |
| M4-025 | RunCommand 消费：claim CAS→steer/interrupt/approval respond；写边界异常→delivery_unknown 不重发；原提案漂移/已完成的原批准拒绝（A12）；重复命令/过期批准去重拒绝 | A（`test_appserver_worker.py` 8 用例 + `test_command_delivery.py`） | P0 | |
| M4-026 | 命令持久化语义：202=仅持久接收、六态投递展示、不确定保留去重键不重发 | H-Web + A（`RuntimeCommands`） | P1 | |
| M4-027 | 清理未确认/结果不可证→隔离（G10）；审批帧含凭据材料即拒绝持久化 | A | P0 | |
| M4-028 | codex-app-server@1 历史版本 not-implemented 显式拒绝 | A | P2 | |

### 4.4 Inspect 导入与工作区（M4-T08/T11/T09）

| ID | 测试点 | 方法 | 优先级 | 预期/备注 |
|---|---|---|---|---|
| M4-029 | Inspect 导入：官方 EvalLog v2 单对象、.eval 二进制不支持提示、AGGREGATE_ONLY 提示 --full、未知顶层字段降 coverage、无执行路径 | A（`test_inspect_v2_identity.py` 9 用例） | P1 | |
| M4-030 | 同身份异内容 IMPORT_IDENTITY_CONFLICT；needs_review→completed 原子收口；中断后同上传可续完；retry/rescore 对导入 Run 拒绝 | A | P1 | |
| M4-031 | API：/runtimes 目录+publish+readiness（零成本）、runtime_profiles、messages（409/501/202）、commands/sessions 视图、inspect import | A+H-API | P1 | |
| M4-032 | Web Harness 页：目录/发布/创建表单（not-enforced 工具显式确认）/monitor；干预进审计并改变比较条件（A11） | H-Web | P1 | |
| M4-033 | 关闭单后端后历史 Run 可读（A15）；分后端独立证据 | A（`test_external_runtime_slice.py` 30 用例） | P0 | |
| M4-034 | live：Pi 真实端点、Claude/Codex 真实小任务+取消+非零退出、app-server 真实审批 | L | P0 | **当前 live_pending**（`m4-live-acceptance.md` 方案待执行） |
| M4-035 | Pi platform-controlled 传输（provider 字段）真实桥接 | L | P1 | 仅 runner-configured 有离线证据 |
| M4-036 | Windows make check 全量绿（或精确差分新增=0） | I | P1 | |

---

## 第 5 部分：M5 场景、Skill 与 Judge

### 5.1 Workflow 与 Fixture（M5-T01/T02/T03/T04）

| ID | 测试点 | 方法 | 优先级 | 预期/备注 |
|---|---|---|---|---|
| M5-001 | WorkflowSpec 契约：有界顺序/when/expect/checkpoint/branch/loop/终止条件；WorkflowVersion 发布不可变 | A（`test_workflow_spec.py`） | P0 | |
| M5-002 | 旧 DSL 只读转换：ast 白名单、恶意表达式 `LEGACY_EXPRESSION_*` 具名拒绝不 eval（A05）、`runs_executed=0`、拒 setup/cleanup/user_simulator/checker | A（`test_workflow_conversion.py`） | P0 | |
| M5-003 | Fixture：prepare/snapshot/reset/cleanup 所有权与失败证据（A03）；隔离初始状态（JSON/文件/临时 DB） | A | P0 | 注意 CI Linux 1 项遗留失败 |
| M5-004 | PrivateTruth：隐藏字段递归投影（任意层级）；gold/隐藏断言不进被测模型可见空间（A14） | A | P0 | |
| M5-005 | 多轮上下文：同 Case 保留、跨 Case 无状态/工作区串扰（A08 同业务 ID 两 Case 隔离） | A | P0 | |
| M5-006 | 步骤期限真实中断受控执行（非返回后检查）；loop 不退/Target 阻塞时全局预算生效（A06） | A（`test_target_stop_confirmation.py`） | P0 | |
| M5-007 | 停止未确认保留现场（interrupted 清理语义）；逐实例移交清理 | A | P0 | |
| M5-008 | 七类过程评分器：tool-arguments/tool-order/state-equals/state-delta/no-side-effect/goal/response-policy 逐 kind 字段白名单 | A | P0 | |
| M5-009 | 终态正确但过程违规必须失败（A02）；确认后取消全达标（A01） | A（`test_business_regression_slice.py` 33 项） | P0 | 七类边界全覆盖 |

### 5.2 Skill（M5-T06/T07/T08）

| ID | 测试点 | 方法 | 优先级 | 预期/备注 |
|---|---|---|---|---|
| M5-010 | SkillVersion：内容/资源 hash/依赖/注入顺序/权限声明固定；导入安全（PATH_TRAVERSAL/SYMLINK_RESOURCE/ZIP_BOMB 拒绝，A11） | A | P0 | |
| M5-011 | 发布幂等：同内容 201/幂等、异内容 409；服务端固定内容 hash | A+H-API | P0 | |
| M5-012 | 注入：权限交集不扩展、deny 优先（A09）；mock 缺失 `SCENARIO_TOOL_MOCK_MISSING` 具名拒绝不回退 real；注入冻结进 resource_snapshots、渲染进 system | A | P0 | |
| M5-013 | 纯指令 Skill 无 entrypoint 静态通过不谎称已执行（A10）；三类（纯指令/带资源/可执行）验证范围诚实标注 | A | P1 | |
| M5-014 | 三臂对照：条件漂移 `ABLATION_CONDITION_DRIFT` 阻断归因（A12）；三组冻结配置、独立初态、真实行为改变、成本 unknown、预算差异阻断、取消臂、臂身份不符拒绝 | A（`test_skill_ablation.py` 11 项） | P1 | **无公共入口**——入口补齐后补 H-API/H-CLI 用例 |
| M5-015 | Web Skill 页：版本列表/注入预览/A-B 提示；发布表单（当前缺失，补齐后回归） | H-Web | P2 | |

### 5.3 Judge 与校准（M5-T09/T10）

| ID | 测试点 | 方法 | 优先级 | 预期/备注 |
|---|---|---|---|---|
| M5-016 | JudgeSpec：固定 profile/rubric/prompt/参数/证据版本；`"tools": []` 与被测模型隔离；purpose=judge 的 invocation 归属 | A | P0 | |
| M5-017 | 未授权零调用（A13）；未知费用不冒充硬上限 | A | P0 | |
| M5-018 | 唯一编译路径：预检=提交同公式；唯一调用计划（pair/order/repeat 稳定 call_id，无笛卡尔扩展） | A | P0 | |
| M5-019 | 逐调用原子额度预留；崩溃窗口 dispatching 标不确定绝不重发（A16）；请求结果未落库转待复核不重复付费 | A | P0 | |
| M5-020 | 畸形/超时/伪引用不判通过（A15）；证据归属三重校验（OBSERVATION_RUN/CASE_MISMATCH/FOREIGN_EVIDENCE_REFUSED） | A | P0 | |
| M5-021 | 追加式评分：`previous_pass_id`、重复评分逐行追加；GET 与默认 rescore 零 Judge 调用（A18） | A | P0 | |
| M5-022 | 不确定 Provider 错误费用恢复为 None（不保留 0.0） | A | P1 | F17 回归 |
| M5-023 | Worker 接线：先 Judge 后 Run 领取、恢复同锁同轮 | A | P0 | |
| M5-024 | Judge API：preflight/submit(202)/get/history/cancel；single-mode 限制（pairwise 库内） | A+H-API | P0 | |
| M5-025 | 人工修订：CAS（expected_current_pass_id 比对）、只追加新 pass 不改历史（A17） | A | P1 | 无公共入口——补齐后补用例 |
| M5-026 | 校准：CalibrationSet/Report/资格 covers()；换序/分歧统计 | A（`test_judge_calibration.py`） | P1 | |
| M5-027 | **真实人工校准 ≥30 条**：配对顺序测试、分歧复核、版本化修订 | L+人工 | P0 | **当前 not_run**——Judge 保持 experimental、M6 正式门禁 fail-closed 的根因 |
| M5-028 | Web Judge 页与真实 API 对齐（F-07 修复后：6 条路径、表单模型 run_id+mode+spec+authorisation） | H-Web | P1 | 当前页面永久"能力不可用" |

### 5.4 公共 Run 与回归（M5-T05/T11/T12）

| ID | 测试点 | 方法 | 优先级 | 预期/备注 |
|---|---|---|---|---|
| M5-029 | scenario backend 注册可用（SCENARIO_BACKEND_AVAILABLE=True）；归属校验拒外来证据 | A（`test_scenario_run_backend.py`） | P0 | |
| M5-030 | 步骤证据端点 `GET /api/v1/runs/{id}/steps`；Web 场景页逐步状态 | A+H-Web | P1 | |
| M5-031 | 副作用后崩溃 → needs_review 零重放（A07） | A | P0 | |
| M5-032 | 既有 Direct/GSM8K/Agent/Harbor 契约与失败恢复不退化（G22） | A（全量回归） | P0 | |
| M5-033 | CLI：scenario/workflow/skill/fixture/judge 命令组 | H-CLI | P1 | |
| M5-034 | 真实部署：场景 Run 结算、Fixture 业务断言、Skill 发布、Judge 预检（验收报告附录 B 复测） | I | P1 | |
| M5-035 | 主干 CI 3 项遗留失败修复后回归（fixture portable/sandbox、PG ScoringJob owner、PG trials downgrade） | I | P0 | 审查 P1 发现 |
| M5-036 | `M5.md` 终态回填后与 HEAD 一致 | 文档 | P2 | |

---

## 第 6 部分：M6 实验、比较与门禁

### 6.1 比较身份与覆盖（M6-T01/T02）

| ID | 测试点 | 方法 | 优先级 | 预期/备注 |
|---|---|---|---|---|
| M6-001 | 比较条件 24 因子白名单：仅模型差异可比（A01）；改名内容变不误匹配（A02）；scorer 变更不可比（A06） | A | P0 | |
| M6-002 | 三级结论 + 逐项原因（结构/指标级分离）；case 内容 hash 对齐 | A | P0 | |
| M6-003 | disposition 真值表：6 类互斥、9 键 counts、契约层不变量（attempted+not_attempted==selected）；全显示不静默过滤（A04 no_expectation 保留） | A | P0 | |
| M6-004 | 分母注册表：8 指标/6 分母各套件合法分母不被统一改写（A05 NaN/空分母拒绝） | A | P0 | |
| M6-005 | 覆盖：缺难样本阻断（A03）；失败/未知样本不静默提分 | A | P0 | |
| M6-006 | 费用三态：known/null 分开、unknown 不当 0（A14）；多币种契约（装配层当前 USD 单口径） | A | P1 | |

### 6.2 Gate 引擎与 Baseline（M6-T03/T07/T08）

| ID | 测试点 | 方法 | 优先级 | 预期/备注 |
|---|---|---|---|---|
| M6-007 | 六类决策语义 + 冻结优先级 + 退出码 0/1/3/5/5/6 | A（21 用例） | P0 | |
| M6-008 | experimental_evidence 正式 fail-closed（Judge 未校准边界） | A | P0 | |
| M6-009 | diagnostic_skip 不得 pass；completed ≠ pass（A17） | A | P0 | |
| M6-010 | NaN/Inf/缺失指标 → insufficient | A | P0 | |
| M6-011 | A19 防漂移：score_sets_digest 掺入 evidence hash；证据变化 ⇒ 新 gate_result_id append-only | A（回归测试在位） | P0 | |
| M6-012 | 重复求值确定且零模型调用（A18）；conclusion 等价性可验证；审计字段不进 hash | A | P0 | |
| M6-013 | Baseline：固定 Run+Pass+Policy；默认指针 CAS + 审计；不漂移（A09） | A | P0 | |
| M6-014 | 回归八类分类逐项可查；added/removed 独立（A10）；instability 不挑最好 | A | P1 | |
| M6-015 | **覆盖防线引擎强制化**（修复后）：政策无 coverage 规则时正式 Gate 不得放行 coverage=2%/selected=100 的快照 | A | P0 | 审查 P1 发现（当前是政策选项） |
| M6-016 | **执行错误阻断**（修复后）：candidate failed 且未显式 requires_successful_run 时正式 Gate 语义 | A | P1 | 当前 opt-in（需产品决策+回归） |
| M6-017 | **全 warn 政策**绕过一切质量规则的发布期告警/lint | A | P1 | |
| M6-018 | 分组 baseline 按 cell 条件匹配（修复后）：多 cell 实验取匹配 entry | A | P1 | 当前只取第一个/None |
| M6-019 | migration 0014：7 表、降级有数据即具名拒绝 | A/I | P0 | |

### 6.3 Experiment 编排与统计（M6-T04/T05/T06）

| ID | 测试点 | 方法 | 优先级 | 预期/备注 |
|---|---|---|---|---|
| M6-020 | ExperimentSpec：因素白名单+护栏；StopPolicy 禁 retry-until-success；矩阵预览零创建 | A | P0 | |
| M6-021 | Cell 分配：并发唯一（A07）、崩溃恢复（A08）、deterministic_run_id、取消只作用自有 Run（A20） | A | P0 | |
| M6-022 | 多 cell 矩阵非法 cell 行为（当前：单 cell 失败不阻断其余排队——规划要求预检期拒绝，修复后回归） | A | P1 | 审查发现偏差 |
| M6-023 | pass@k：n=5,c=2,k=2→0.7；k>n→not_applicable；资格制（计划不足/transport retry 不计 n——当前仅 docstring 契约） | A | P1 | **接线后**补报告/导出消费用例 |
| M6-024 | Task 聚类 bootstrap：seed 确定性、分位数线性插值 | A | P1 | 同上（当前零生产消费者） |
| M6-025 | paired_difference / binary_interval（Wald 独立性免责） | A | P2 | 同上 |
| M6-026 | statistical_policy_ref 参数化（修复后：政策升版反映进 GateResult hash） | A | P2 | 当前硬编码 @1 |

### 6.4 三端入口与导出（M6-T09/T10/T11）

| ID | 测试点 | 方法 | 优先级 | 预期/备注 |
|---|---|---|---|---|
| M6-027 | API 21 条路由：comparisons/gates/baselines/default/gate-policies/versioned-gate/export/regressions/report-snapshot/experiments×6 | A+H-API | P0 | |
| M6-028 | CLI 5 命令组：evaluate 真实退出码（0/1/2/5 实测历史） | H-CLI | P0 | |
| M6-029 | Web 四页：实验/比较/基线（CAS 冲突）/门禁；13 组件用例 | H-Web + A | P1 | |
| M6-030 | exporter v1：JSON/JUnit；JUnit properties 含 decision/exit_code/gate_result_id/conclusion_hash；insufficient/不可比/空集不导出为假通过 | A | P0 | |
| M6-031 | 端到端 slice：Experiment→Cell→Run→比较→Gate→export 闭环（3 用例） | A（`test_experiment_gate_slice.py`） | P0 | |
| M6-032 | 三端同一服务装配（API 与 CLI 同一 ComparisonService/ExperimentService） | A | P0 | |
| M6-033 | GSM8K/DirectLlm 旧 compare 页迁移统一服务（债务修复后回归；当前前端自算） | H-Web | P2 | |
| M6-034 | PG 三件套（gate/baseline/experiment store）真实 PG 并发 | I（CI PG） | P0 | 本机 skipif 如实登记 |
| M6-035 | live：DeepSeek V4.1 Flash 有界验收复现（4 调用/316 tokens/4 对；重复 compare/gate/export 零新增调用） | L | P1 | 历史记录存在 |
| M6-036 | 跨 suite fixture：C-Eval/Harbor/M4/M5 侧实验矩阵（当前间接覆盖——补齐后回归） | A/I | P2 | |

---

## 第 7 部分：M7 SDK、迁移与发布（implementation_complete / offline_verified / external_pending，需按项复验）

> M7 已合入当前 main，验证账本结论为 `implementation_complete + offline_verified + external_pending`。以下测试点既是回归清单，也是本轮审查缺陷的修复验收清单。自动化通过不替代真实 Compose/PG/旧数据/恢复/替代场景/cutover 证据；当前不得声明 stable_supported 或 cutover_ready。

### 7.1 SDK 客户端（M7-T01/T02，G01–G05）

| ID | 测试点 | 方法 | 优先级 | 对应 |
|---|---|---|---|---|
| M7-001 | SDK 类型/错误：typed response、error 保留 error code/request ID/retryable/安全 detail；能力缺失 typed unsupported | A（`tests/sdk/test_client_contract.py`） | P0 | G01 |
| M7-002 | `import motte_sdk` 零副作用（不启动 DB/Worker/Provider） | A | P0 | G01 |
| M7-003 | 方法域覆盖：Run create/get/list/cancel/retry、事件游标、指定 pass 报告/Artifact 元数据、rescore、Experiment/Comparison/Baseline/Gate/导出 | A | P0 | G02 |
| M7-004 | 幂等键：同 key 同 body 得同一 Run、不同 body 返回 conflict（服务端权威实现，CLI/Web/SDK 共用） | A | P0 | G03/A03 |
| M7-005 | 重试矩阵：422/auth/config/permission 不重试；网络/超时/5xx 分类重试；付费 POST 不无条件重复 | A | P0 | G03/A02 |
| M7-006 | wait_for_run：总期限+本地取消、超时不暗中 cancel、needs_review 终态不自动 retry | A（`tests/sdk/test_wait_and_events.py`） | P0 | G02 |
| M7-007 | SSE 断线恢复：Last-Event-ID/after 游标、心跳、稳定 event ID 去重、gap/partial 显式（不可补不假装完整）、终态与最后事件一致 | A+H-API | P0 | G05/A05 |
| M7-008 | SDK 与 Web `useRunEvents.ts` 同一语义 | A | P1 | G05 |

### 7.2 CLI 与 pytest/导出（M7-T03/T04，G06–G07）

| ID | 测试点 | 方法 | 优先级 | 对应 |
|---|---|---|---|---|
| M7-009 | CLI local/server 显式模式：server 只走 SDK HTTP 不碰服务端 DB；断连非零退出且本地 DB/provider/task-start 计数=0 | A（`tests/cli/test_remote_parity.py`） | P0 | G04/A04 |
| M7-010 | 同参数 local/server 校验一致、错误一致、stdout JSON/JSONL 稳定单行可解析 | A | P0 | G02 |
| M7-011 | 退出码语义与 M6 冻结一致；**退出码 4（用户取消）落地** | A+H-CLI | P0 | G07 |
| M7-012 | pytest 插件：安装后普通 pytest 零模型/Benchmark/Run 调用；显式 marker/fixture 才启用 | A（`tests/sdk/test_pytest_and_exports.py`） | P0 | G06/A06 |
| M7-013 | Trace decorator/context manager：callable 只执行一次（嵌套 span/异常/脱敏/截断/采样/部分 flush 失败，上报失败不重跑业务） | A | P0 | G06 |
| M7-014 | exporter v1 扩展：quality_fail→failure、基础设施错误→error、合法不适用→skipped；insufficient/不可比/空集不得假通过；JSON 保留全部 rule/reason/hash | A | P0 | G07/A07 |

### 7.3 打包与干净安装（M7-T05，G08–G09）

| ID | 测试点 | 方法 | 优先级 | 对应 |
|---|---|---|---|---|
| M7-015 | wheel 干净安装：checkout 外干净 venv import/调用/读资源，无源码/cwd/PYTHONPATH 依赖 | A（`tests/packaging/test_clean_install.py`） | P0 | G08/A01 |
| M7-016 | 依赖隔离：SDK 不隐式拉 Worker/Runner/DB 依赖；extras 边界、lock/wheelhouse/provenance | A | P0 | G09 |
| M7-017 | Web 构建产物打包进发布件；不同 cwd 运行 | A+I | P1 | G08 |
| M7-018 | Compose 真实 build/up/health/migrate 一次性服务（不停在 config） | I | P0 | G08 |

### 7.4 历史迁移（M7-T06/T07/T08，G10–G14）

| ID | 测试点 | 方法 | 优先级 | 对应 |
|---|---|---|---|---|
| M7-019 | ImportManifest 四契约（ImportManifest/SourceIdentity/MappingRecord/ImportReport）校验 | A | P0 | G10 |
| M7-020 | 来源受控：拒路径穿越/symlink 逃逸/压缩炸弹/超配额/不允许 URL；API 不接受任意宿主路径 | A | P0 | A11 |
| M7-021 | dry-run：目标 DB 零修改；输出 created/reused/rejected/conflicted/unknown fields/missing artifacts/凭据重绑清单/counts/hash | A（`tests/migration/test_import_plan.py`） | P0 | G10/A08 |
| M7-022 | apply 前源 hash 复验：dry-run 后源变化阻断旧计划 | A | P0 | A08 |
| M7-023 | 幂等重跑：同源同内容 reused、同 ID 异内容 conflict 不覆盖 | A | P0 | A09 |
| M7-024 | 凭据只迁引用：生成重绑清单；不复制密钥/会话/本机配置目录 | A | P0 | G11 |
| M7-025 | 导入次序与追溯：配置→数据→场景/Skill→历史 Run/Trial/Trace/Artifact/Score/Baseline/Gate；旧 ID/raw schema/mapping version 可查 | A（`tests/migration/test_legacy_import.py`） | P0 | G12 |
| M7-026 | imported origin 不可分发：Worker/Dispatcher 不能 claim；重跑须新建 Run | A | P0 | G12 |
| M7-027 | 缺失字段 unknown：legacy summary=.5 不伪造两题逐样本分；缺 gold/模型身份/价格/pass 保持 unknown，不从当前配置反推 | A | P0 | G13/A10 |
| M7-028 | 不迁进行中 Job；不可表达 DSL/权限/断言不发布为可执行资源 | A | P0 | G12 |
| M7-029 | artifact 先暂存 hash 校验再关联；hash 不匹配拒绝 | A | P0 | G14 |
| M7-030 | checkpoint/resume：半批崩溃恢复只处理未提交单元、不重复关联或收费 | A（`tests/migration/test_import_resume_rollback.py`） | P0 | G14/A12 |
| M7-031 | rollback 只撤销本批创建且未被引用对象或标 inactive；共享 Artifact 回退保护 | A | P0 | G14/A13 |
| M7-032 | apply/resume/rollback 需 operator intent + import id + manifest hash + 审计 | A | P1 | G14 |
| M7-033 | 核对报告：数量/引用/hash/原始与归一化分数/状态 | A | P1 | G10 |
| M7-034 | 真实旧平台导出导入（源 counts/hash/license） | L+I | P0 | 外部验收 |

### 7.5 备份恢复与升级（M7-T09，G15–G16）

| ID | 测试点 | 方法 | 优先级 | 对应 |
|---|---|---|---|---|
| M7-035 | 维护窗口：发布模型/评分/GC/Artifact 写入被阻断或纳入一致快照协议 | A+I | P0 | G15/A14 |
| M7-036 | SQLite online backup / PG 一致快照；两连接写入阻断 | I | P0 | G15 |
| M7-037 | 备份一致性：按 DB 快照列不可变 Artifact 清单逐文件复制校验（非扫当前目录） | A（`tests/integration/test_backup_restore_consistency.py`） | P0 | G15 |
| M7-038 | 恢复校验：缺文件/额外文件/损坏 manifest/版本不兼容均不标完整成功 | A | P0 | G15/A15 |
| M7-039 | staging 恢复默认新 DB/root，先校验再只读检查；queued/running/needs_review 不自动付费（A16） | A+I | P0 | G15 |
| M7-040 | 恢复核对：Run 数/历史 pass 数/baseline 引用/抽样 Artifact hash/资源版本/凭据待绑定 | A | P0 | G15 |
| M7-041 | 升级演练：空库安装、上一支持版本→当前、migration failure 恢复、旧应用读未知状态明确不兼容（不误当 queued） | A+I | P0 | G16/A19 |
| M7-042 | migration 只沿 head 追加不改历史；schema downgrade 先声明删除哪些证据 | I | P0 | G16 |
| M7-043 | 秘密凭据默认不进普通备份；单独秘密备份显式选择 | A | P1 | G15 |

### 7.6 安全、retention 与供应链（M7-T10，G17–G19）

| ID | 测试点 | 方法 | 优先级 | 对应 |
|---|---|---|---|---|
| M7-044 | 默认 loopback 但校验 Origin/Host/CSRF/CORS/DNS rebinding：不可信网页请求本地任务接口被拒（不 create Run/不改凭据） | A（`tests/security/test_single_user_boundary.py`） | P0 | G17/A17 |
| M7-045 | 恶意 redirect 不泄漏认证；内网模型端点显式允许列表与凭据绑定 | A | P0 | G17 |
| M7-046 | 远程部署：显式单用户认证 + TLS；裸执行接口不暴露 | I | P0 | G17 |
| M7-047 | 凭据接口只返回引用/掩码 | A+H-API | P0 | G17 |
| M7-048 | 全链路脱敏回归：日志/错误/SSE/Trace/导出/Provider payload/Artifact/Web | A | P0 | G17 |
| M7-049 | retention 分类与 pin：Baseline/固定 ScoringPass/needs_review/import audit 优先保护；被 pin 的过期证据保留并说明原因 | A | P0 | G18/A18 |
| M7-050 | GC 默认 dry-run、apply 显式、删除写 tombstone（hash/大小/原因/时间）；与采集/评分/备份互斥或锁协议 | A | P0 | G18 |
| M7-051 | 缺已删证据时 rescore/compare 返回不足原因 | A | P1 | G18 |
| M7-052 | 供应链：固定依赖/镜像/lock、secret scan、dependency audit、license 清单、构建 provenance；失败阻断发布 | I | P0 | G19 |
| M7-053 | 公开仓库只含合成或授权脱敏 fixture（数据许可检查） | I | P0 | G19 |

### 7.7 RC、支持矩阵与 cutover（M7-T11/T12，G20–G23）

| ID | 测试点 | 方法 | 优先级 | 对应 |
|---|---|---|---|---|
| M7-054 | RC 固定：commit/lock/wheel/Web artifact/Docker image+provenance/migration head/exporter 与 API schema 版本/release notes | I | P0 | G22 |
| M7-055 | 支持矩阵 `docs/release/support-matrix.md`：按 OS/运行方式/存储/Runner/Provider/操作五级登记 tested/supported/experimental/blocked/not_run；skip/not_run 不计 stable | 文档+I | P0 | G20/A20 |
| M7-056 | release smoke：clean install→migration→health→synthetic Run→report/Gate 真实执行 | A（`tests/integration/test_release_smoke.py`） | P0 | G16 |
| M7-057 | Web/API/CLI 一致性（含旧 compare 页债务处置的登记） | I | P1 | G22 |
| M7-058 | 三类替代场景①：C-Eval 相同数据/Profile 双模型完整执行、样本报告、可比性与覆盖门禁可复核 | L+I | P0 | G21/A21 |
| M7-059 | 三类替代场景②：固定 Terminal-Bench 版本经 Harbor，Task/Trial/Verifier/Artifact/取消/失败完整可复核（Docker/Harbor 缺失则 blocked 不伪造） | L+I | P0 | G21/A21 |
| M7-060 | 三类替代场景③：多轮 Scenario + Skill fixture/A-B，固定评分版本与回归 Gate 进 CI（Judge 未校准保持 experimental） | L+I | P0 | G21/A21 |
| M7-061 | cutover readiness：缺任何必备证据不输出 ready | A（`tests/integration/test_cutover_readiness.py`） | P0 | G23/A21 |
| M7-062 | 切换七步记录：RC 固定→停旧新增→旧库只读备份+dry-run→分批 apply 核对→三场景+恢复演练→单路进入+回退手册→发布矩阵/迁移报告/未迁移功能清单（已替代/已迁移/后置/不迁移） | 流程+文档 | P0 | G23 |
| M7-063 | 回退窗口验证：失败时回到已验证配置；不把进行中 Run 反向迁回；不以"不报错"替代结果和成本核对 | I | P0 | G23 |
| M7-064 | 无持续双写/双调度/双库同步（架构红线） | A+I | P0 | G23 |
| M7-065 | M7 最低验证命令组全绿（execution plan §6：sdk/cli/migration/security/maintenance + 集成三件 + ruff/mypy/openapi/web/check） | I | P0 | 门禁 |
| M7-066 | M6 遗留边界处置登记：真实 PG、Docker/Harbor、Judge 校准、旧 compare 债务全部进支持矩阵，不自动标 stable | 文档 | P0 | 输入边界 |

---

## 附录 A：测试环境与工具要求

| 环境 | 用途 | 说明 |
|---|---|---|
| Linux（CI 同款：ubuntu + uv + pnpm + PG service） | 权威全量回归 | `make check`；Windows 环境失败族在此不复现 |
| Windows 10/11 | 便携分支、Job Object、重解析点专项 | 全量需与基线 node-id 差分 |
| Docker（宿主） | Sandbox live、Harbor oracle/真实、Compose build/up | 当前审查机无 docker（如实 not_run） |
| PostgreSQL 16（`MOTTE_PG_DSN`） | PG 专项（migrations/并发/三件套） | CI 有 service；本机 skipif |
| 真实 Provider key（环境变量，永不入库） | live smoke/验收 | 显式授权+预算；记录费用与证据 |
| OpenCompass 0.4.2 固定环境 | C-Eval/CMMLU 真实 Runner | macOS arm64 已验；Linux 需重验 |
| Harbor 0.23.0（lock 固定）+ terminal-bench 2.0 任务集 | M3 层 2/3 | 上游任务集需授权清单 |

## 附录 B：当前 not_run 的 live/外部测试点汇总（发布阻断项）

| ID | 内容 | 阻断原因 |
|---|---|---|
| M1-025 | 真实 subject 模型 Agent 完整验收 | 单个 DeepSeek 正常 case 已通过；第二模型、取消、预算触顶、needs_review、费用对账仍待授权/预算 |
| M2-030/031/032 | 真实模型/full Profile、官方数据+许可、CMMLU 真实 | 付费授权；受信核验器未部署 |
| M3-027/028 | 真实模型小批次、上游 89 题任务集、claude-code 兼容核验 | 任务清单/模型/预算授权 |
| M4-034/035 | Pi 真实端点、Claude/Codex 真实任务、app-server 真实审批 | live_pending（方案已有） |
| M5-027 | Judge ≥30 条真实人工校准 | 标注资料+费用授权 |
| M7-058~060 | 三类替代场景（cutover 前置） | P0 安全/数据完整性缺陷已修复并离线回归；仍依赖 Docker/Harbor、官方数据/Provider、Scenario/Skill 固定 live 验收 |

> 以上各项在完成前，平台不得宣称 stable_supported；各阶段验证记录已按此口径登记，本测试点文档与其保持一致。
