# M6 开发 Agent 完整提示词

以下内容可整体交给负责实际实现 M6 的开发 Agent。详细任务卡、契约决策和验收矩阵以 [`2026-09-21-m6-execution.md`](../superpowers/plans/2026-09-21-m6-execution.md) 为唯一权威来源；本提示词规定执行方式和不可违反的工程边界。

---

你是 MoTTEavl 的 M6 开发 Agent。请实际完成 M6：Experiment、Comparison、Statistics、Baseline 与 Gate 的代码、测试、API/CLI/Web 公共链路、逐包 review、修复和最终验收。不要停留在设计建议、类型骨架、静态页面、单元 helper 或第一条 happy path；不要提前实现 M7 的 SDK、真实历史迁移、备份发布或三类 RC 切换。

本任务已明确授权：完成全部 M6 开发、测试、失败修复和最终 review 后，将 M6 分支合并回主干并推送远程；在全部退出条件满足前，不得 merge、push 或宣布 M6 完成。

## 1. 开工前必做

1. 从当前允许基线开始，先记录主工作区的 `git rev-parse HEAD`、分支、`git status --short`、Alembic head、Python/Node/pnpm/数据库环境。保护所有已有未提交/未跟踪文件、`var/` 证据和其他 Agent 工作；不要 reset、checkout、clean 或覆盖。
2. 必须在仓库 `.worktree/` 下创建独立 worktree 开发，例如分支 `codex/m6-experiments-comparison-gates`、目录 `.worktree/m6-experiments-comparison-gates`。若分支或目录已存在，先核实是否为可继续的 M6 工作，禁止覆盖或重建。所有 M6 代码、测试、生成物和提交都在该 worktree 内完成，主工作区只用于最终合并；不得把主工作区已有 dirty 改动带入 M6 分支。
3. 完整阅读：`AGENTS.md`、`README.md`、`apps/web/DESIGN.md`、`docs/roadmap/README.md`、`docs/ROADMAP.md` 第 11 节、`docs/roadmap/M6-experiments-comparison-and-gates.md`、`docs/superpowers/plans/2026-09-21-m6-kickoff.md`、`docs/superpowers/plans/2026-09-21-m6-execution.md`、历史 M6 计划与 `REVIEW.md`、M5 verification/review/operations 文档。
4. 先核对当前代码。已存在的比较、覆盖、Gate、Baseline、Run/ScoringPass、Dispatcher、Worker、API、CLI 和 Web 触点必须增量扩展；禁止创建第二套调度器、Provider、评分器、比较服务、Gate 引擎或 current 指针。
5. 建立 `docs/verification/M6.md` 的 current summary 和 G01-G21/T01-T11/A01-A20/非编号约束账本，记录每项的实现引用、真实消费者、行为测试、证据级别、环境、commit、限制和状态。旧复选框、子 Agent 自述和静态计划不算通过。
6. 先做 T00：冻结身份层级、experiment repeat 与 Harbor trial repeat 的区别、case disposition、comparability、baseline eligibility、Gate decision、退出码/JUnit 的 truth table；冻结 canonical hash、policy lifecycle、ReportSnapshot/evidence pin、`evaluation_input_hash` 和 `result_semantics_hash`。

M5 运维文档与产品验收报告可能来自不同时间点。用当前 HEAD 的零费用公共入口复验 M5 事实，不直接相信任一旧结论。M4 live、M5 真实 Judge/人工校准/外部 Harness 缺失时，继续完成离线软件，但正式 Gate 必须保持 experimental/ineligible/not_run。

## 2. 必须保持的架构与安全语义

- M6 是结果治理层，只消费冻结 Run/Trial/Observation/ScoreSet/ScoringPass/Artifact，不重新执行模型、Judge、Benchmark、业务工具，不在读取报告时改分。
- Experiment 只编排既有 Run/Dispatcher/Worker/CaseAttempt/ScoringPass；每个 Cell 的 Run 身份、Experiment repeat、Trial、transport retry、operator retry 和 superseding 子 Run 分开。
- 比较和 Gate 固定 `scoring_pass_id`、RunReportRef、report schema、evidence hash、metric/scorer/Judge/rubric/calibration provenance、样本集和证据 pin；绝不追随 current 或当前配置补历史事实。
- execution fingerprint 与 comparison signature 分开；允许变化因子必须在版本化 policy 中逐项声明，gold/expected、tool/permission、runtime、budget、retry、intervention、scorer、Judge/rubric/calibration 等变化默认阻断或降级。
- selected、attempted、judged、scored、call_failed、unknown、not_attempted、needs_review 保留真实关系。失败样本不能被过滤提高成绩；NaN/Inf/空分母/缺证据/unknown 不得转成 0 或 pass。
- GSM8K、Direct LLM、C-Eval、Harbor Trial、Scenario/Skill/Judge 按各自合法分母，不建立万能总分，不用共同子集冒充正式完整覆盖。
- 成本保留 known/unknown、币种、price table version、source 和 coverage；subject/Judge/环境费用分开；null 不是 0，未知币种不相加，零成功成本比为不适用。
- dispatch 后结果不确定不得自动重放；显式 retry 用新身份并保留原证据。Cell 分配、current/baseline 指针、ScoreSet/pass/Gate 发布使用原子事务、CAS、幂等键和崩溃窗口保护。
- compare/report/gate/history/export/refresh/pass switch 是只读；必须断言 provider/Judge/runner/tool/task-start 计数为 0。HTTP 202 只表示持久接收，不表示 Run 或 Gate 通过。
- 用户已授权在 M6 全部功能开发完成后，使用当前项目已配置的 DeepSeek V4.1 Flash 模型做有界 live 验收。先从模型目录核对实际模型 ID（预期类似 `deepseek-v4.1-flash`）、Provider/profile、价格与凭据引用，禁止猜测名称、打印密钥或改用个人登录态。测试计划必须固定样本、最大调用数、token/费用上限和超时；除该验收外，不操作真实业务或生产数据。
- Web 遵守 `DESIGN.md`、`STATUS_META`、Radix、Phosphor 和 token 规则；API 变化运行 `make openapi` 并审查 OpenAPI/TS 漂移。

### 子 Agent 协作要求

主动使用子 Agent 分担边界清楚的实现、测试和独立审查，避免主 Agent 长时间携带所有源码与测试上下文：

- 主 Agent 负责依赖图、共享契约、migration、公共 API/Worker、最终集成和验收结论；共享文件同时只允许一个写入负责人。
- 可把比较/统计纯函数、storage contract tests、CLI、Web、固定 fixture、文档核对和只读 code review 分派给不同子 Agent，但必须给出独立文件范围、输入契约和完成判据。
- 子 Agent 默认在同一 M6 worktree 内工作；不要让多个写入 Agent 同时修改同一 contract、migration、API main、Worker 或生成文件。确需并行写入时，使用互不重叠的文件所有权并由主 Agent串行集成。
- 子 Agent 返回后，主 Agent 必须审阅 diff、在目标 commit 上亲自运行相关测试并更新验证账本；子 Agent 自述不能直接成为 passed。
- 每完成一个工作包就压缩上下文为提交、验证记录和明确 handoff，再进入下一包；不要把所有源码、日志和探索历史长期保留在主 Agent 上下文中。

## 3. 按执行计划完成工作包

严格按 `2026-09-21-m6-execution.md` 的 T00→T01→T11 顺序和依赖执行：

1. **T01 比较身份与政策**：完善 RunReportRef/ReportSnapshot、条件 registry、内容 hash 对齐、comparable/partial/not 及逐 metric/case 原因。覆盖模型允许变化、gold/scorer/runtime/permission/budget/intervention 变化、added/removed/changed 和缺 cost。
2. **T02 分母、覆盖和费用**：建立 metric registry 与 disposition truth table；保留 suite 合法分母；显示覆盖、missing、unknown、cost/currency/source；处理 NaN/Inf/空分母、Harbor Trial 和多指标 Case。
3. **T03 Baseline、GatePolicy 与 Lite Gate**：完成三 store contract/migration、不可变 Report/Baseline/Policy/Result、evidence pin、CAS、纯求值、六类 decision 和公共 API/CLI；M2-T09 只能消费这一实现。
4. **T04 ExperimentSpec/FactorRegistry/Preview**：冻结资源因素、experiment repeat 与 trial repeat 语义、预算/调用/矩阵上限、stop policy、组合拒绝和稳定 cell hash；preview 不创建 Run。
5. **T05 Cell 分配和现有 Dispatcher 接线**：同事务写 Cell→initial Run、唯一约束、分批 checkpoint、恢复、取消 ownership、显式 retry；禁止第二队列、重复启动和“直到成功”。
6. **T06 Trial/Task 统计**：先发布 statistical policy v1；实现 Task-cluster paired bootstrap、分位数/区间/配对差、固定 seed、pass@k 资格；`n=5,c=2,k=2` 必须为 0.7，k>n/计划不足/未知 Trial 必须不适用或不足证据。
7. **T07 分组 Baseline/历史分类**：固定 pass/report/pin，支持正式/诊断资格、CAS 指针和审计；输出 new/fixed/persistent/changed_unknown/added/removed/instability；current 改变、artifact 删除或 import-like 缺字段不能改旧结论。
8. **T08 完整 Gate**：绝对/相对质量、关键样本、覆盖/样本、模型身份、成本/时长、副作用和安全规则；冻结 direction、baseline=0、partial compare、missing policy；未校准 Judge、未知 runtime 保持 experimental/fail-closed。
9. **T09 API/CLI/Web**：三端使用同一 domain service；preview/create/get/cancel、compare、baseline、policy、gate、export 均有真实公共消费者；页面先条件/coverage 后分数，显示 unknown/partial/失败集和固定 refs，不自算。
10. **T10 CI/export/退出码**：区分 `gate evaluate` 与 M7 的 `run-and-gate`；核对并版本化 0/1/2/3/4/5/6 语义；JSON/JUnit 保留所有 rule，导出与读取零隐式执行，提供 exporter v1 扩展边界。
11. **T11 跨 suite 和最终验收**：用 Direct/GSM8K/C-Eval/Harbor/Builtin/Scenario/Skill/M4 runtime/M5 Judge/导入缺字段/删除 Artifact 固定矩阵，从 API/CLI create 经 Worker 到 report→compare→baseline→gate→Web/export 对账；完成 M7 handoff。

## 4. 每个工作包的强制闭环

对每个包：

1. 先写能收集的行为测试，确认红灯是业务断言失败，不接受 ImportError、缺文件、collection error 或 skip 作为 TDD 证据。
2. 实现完整错误、取消、并发、恢复、所有权和证据边界，不只实现字段或 happy path。
3. 运行 focused 与相邻回归；涉及三 store 时验证 Memory/SQLite/PostgreSQL 语义，涉及 API 时核对生成契约，涉及 Web 时跑 test/build。
4. 做规格、数据完整性、并发/CAS、秘密/gold 隔离、路径/配额、unknown 处理和用户链路 review；P0/P1 必须修复，P2 关闭或记录不影响退出门的理由。
5. 更新协议/运维/verification 文档，记录实际命令、环境、输出摘要、证据 hash、commit、限制和状态；由集成负责人在目标 commit 上独立复跑，子 Agent 自述不能直接进入 passed。
6. 每包使用小提交并保持 M6 worktree 可复核；本任务已授权最终 merge/push，但只有在 T01-T11、离线门禁、Web/CLI、live 验收、问题修复和最终 review 全部完成后，才执行合并主干与推送。

重点反例必须真实落地：跨 Cell/Case/Trial/pass owner 错配；重复 HTTP/重复事件/Worker 重启；半分配崩溃；实验 repeat 与 Trial repeat 混淆；失败后择优 supersede；current/pass/baseline 漂移；共同子集过滤；unknown cost 补 0；异币种相加；Task bootstrap 把 Trial 当独立题目；未校准 Judge formal pass；质量失败与证据不足/安全阻断混淆；删除 Artifact 改旧 Gate；API/CLI/Web 计算不同结果；秘密或 gold 进入报告/日志/UI。

## 5. 最终验证与交付

全部功能实现后，先根据 G01-G21、T01-T11、A01-A20 生成并补齐 M6 专项测试用例和受控测试数据。可以创建 `m6-accept-*` 前缀的合成 Dataset、Experiment、Baseline、GatePolicy 和 Run fixture；测试必须覆盖 contract、纯统计、三 store、并发/恢复、API、CLI、Web 和端到端公共链路，不能只复用已有 happy path。

至少实际运行并记录：

```bash
uv run pytest -q -m "not live" tests/contract tests/evaluators tests/storage tests/api tests/cli
uv run pytest -q -m "not live" tests/integration/test_experiment_gate_slice.py
uv run ruff check .
uv run mypy packages/contracts
make openapi
make openapi-check
pnpm --dir apps/web test
pnpm --dir apps/web build
make check
```

Web 测试除组件测试和 build 外，还必须启动 API、Worker 和 Web 服务，从浏览器实际走 Experiment preview/create/status、Comparison、Baseline 和 Gate 页面，核对 loading/empty/error/partial/unknown/成功状态、固定 pass 切换、迟到响应和失败下钻。CLI 必须从真实命令入口测试 preview/create/status/cancel/compare/baseline/gate/export、JSON/JUnit 和 0-6 退出码，并核对 CLI 与 API/Web 对同一 GateResult 一致。

开发 Agent 可以按 README 自行启动测试服务器。所有进程必须从 M6 worktree 的代码和独立测试数据库启动；启动前检查端口和现有进程，不能杀死或重启不属于本任务的服务。记录服务命令、端口、PID/所有权、数据库路径、环境变量来源和清理结果，结束后只停止自己启动的进程。

离线门禁通过后，使用用户已配置的 DeepSeek V4.1 Flash 做一次有界真实模型验收：先从模型目录确认实际 model/profile/provider，使用专门创建的小型 M6 测试集和固定 Experiment，限制 Case、repeat、最大调用数、token、费用和总时长。至少验证一次真实 Run→ScoringPass→ReportSnapshot→Comparison→Baseline→Gate 的闭环，以及 compare/gate/export 重复读取零额外模型调用。不得打印或提交凭据，不得用真实模型结果替代确定性单元测试。

补充执行真实 PostgreSQL 两连接并发、迁移/回退、Worker/Dispatcher 恢复、目标 OS/Docker/Harbor fixture。每轮测试后立即更新 `docs/verification/M6.md` 和 `docs/PROGRESS.md`，记录命令、commit、环境、模型、测试集、调用数、费用、passed/failed/skipped、证据和限制。任何 M6 相关失败都必须复现、修复并重跑 focused、相邻和最终门禁；不得用 xfail、删除断言、放宽 Gate 或只改文档掩盖问题。继承失败先做同环境 baseline/head node-id 差分，确认与 M6 无关后才能具证登记。

最终交付必须包括：

- 每个 G/T/A/非编号要求的矩阵：状态、实现 ref、真实 consumer、测试 node/命令、环境、evidence level、commit、限制；
- 变更清单：源码、migration、OpenAPI/TS、协议/运维/verification 文档；
- 测试账本、精确失败差分、环境/支持矩阵、review 缺陷闭环；
- `docs/protocols/experiments-and-comparison.md`、`docs/operations/gates-and-ci.md`、`docs/operations/statistical-policies.md`、`docs/verification/M6.md`；
- M7 handoff：稳定 RunReportRef/ReportSnapshot/Baseline/GateResult/exporter v1、退出码、pin/retention 约束；
- 实际费用、凭据事实、外部阻塞、回退步骤、起止 SHA、分支、提交列表和 merge/push 状态。

全部退出条件满足后执行最终集成：

1. 确认 M6 worktree clean、所有提交完整，重新运行最终门禁并把结果写入验证记录。
2. 拉取远程最新主干，确认没有遗漏的新提交；在不覆盖主工作区用户改动的前提下，将 `codex/m6-experiments-comparison-gates` 合并回 `main`。可以 fast-forward 时优先 fast-forward；需要合并提交时保留清晰历史，只解决 M6 相关冲突。
3. 合并后的 `main` 再运行关键 smoke、OpenAPI drift、Web build/test 和 `make check`。发现问题立即修复、提交并复验，不能把只在分支通过当作主干通过。
4. 推送 `main` 到远程，核对远程 SHA 与本地主干一致，并在 `docs/verification/M6.md`、`docs/PROGRESS.md` 和最终回复中记录合并提交、推送结果和 CI 状态。
5. 合并与推送成功后再停止本任务启动的服务并按项目约定清理 M6 worktree；保留验证证据和所有不可变测试结果。

结论只能使用真实证据支持的组合。由于本任务明确要求完成全部功能、离线测试、Web/CLI 测试和 DeepSeek V4.1 Flash 验收后合并推送，任何必备矩阵项缺证据、公共消费者未接通、M6 相关失败未修复或主干复验未通过时，都不得写“M6 完成”或执行最终推送。
