# MoTTEavl M1–M7 阶段规划索引与实施约束

日期：2026-09-19（UTC+08:00）  
状态：规划文档已写入专项分支；M1–M7 的功能目标均待实现/验收。  
规划分支：`docs/roadmap-m1-m7-20260919`  
实现基线：`a668d13ee5ea0c3613648f8992ecaa4a148d3855`  
旧平台参考：`BaiZhi967/llm_agent__evaluation_platform@b661bcdf83e1c3dfb8d6062ee78817d249e86a4c`

本目录把用户确认的[总路线](../ROADMAP.md)展开为七份独立阶段设计与实施工作包。每份文档包括具体模块范围、契约、工作包、目标复选框、失败/验收矩阵、API/CLI/Web 目标、验证要求和回退边界。这里描述的是待交付能力，不是本次已经实现的代码。

后续执行细化（2026-09-19）：[M1–M7 开发执行总计划](../superpowers/plans/2026-09-19-m1-m7/README.md)已按更新后的代码基线编排逐包任务、依赖、文件触点、测试反例、完整覆盖表与阶段 review；[计划审阅记录](../superpowers/plans/2026-09-19-m1-m7/REVIEW.md)说明当前实现差异和规划验证。本次范围仅为详细计划与计划 review，不改变以下功能目标的待实施状态。

M3 开工交接（2026-09-20）：[当前详细任务安排](../superpowers/plans/2026-09-20-m3-kickoff.md)及[开发 Agent 提示词](../prompts/M3-development-agent.md)。实现状态与证据边界以各阶段验证记录为准，原规划基线不代表最新代码状态。

M4 开工交接（2026-09-20）：[当前详细任务安排](../superpowers/plans/2026-09-20-m4-kickoff.md)及[开发 Agent 提示词](../prompts/M4-development-agent.md)。以合入 M3 第三轮修复的 `a3cdfbe` 为起点，M4 实现状态仍为 planned。

## 1. 文档入口

| 阶段 | 文档 | 核心产出 | 目标数 / 工作包数 / 验收场景数 |
|---|---|---|---|
| 总路线 | [docs/ROADMAP.md](../ROADMAP.md) | 产品范围、迁移取舍、顺序和稳定发布退出条件 | 保留原路线图 |
| M1 | [原生 Agent 与通用评分](M1-native-agent-and-evaluation.md) | 文件任务、受控工具、多指标评分、证据与取消 | 18 / 10 / 15 |
| M2 | [LLM Benchmark 与 C-Eval](M2-llm-benchmarks-and-ceval.md) | job-based 执行、Catalog/Profile、C-Eval 与 Parser 对照 | 17 / 11 / 15 |
| M3 | [Harbor 与 Terminal-Bench](M3-harbor-and-terminal-bench.md) | Task/Trial/Verifier/Artifact 与环境型任务 | 18 / 10 / 15 |
| M4 | [Pi 与外部 Harness](M4-pi-and-external-harnesses.md) | 真 Pi、Claude/Codex batch、可选交互命令 | 18 / 11 / 15 |
| M5 | [场景、Skill 与 Judge](M5-scenarios-skills-and-judges.md) | 主流程状态、Skill fixture/A-B、可校准 Judge | 22 / 12 / 18 |
| M6 | [实验、比较与门禁](M6-experiments-comparison-and-gates.md) | Experiment、比较条件、Baseline、统计和 CI Gate | 21 / 11 / 20 |
| M7 | [SDK、迁移与发布](M7-sdk-migration-and-release.md) | 客户端、旧数据、恢复、安全部署和主线切换 | 23 / 12 / 21 |

合计：137 个阶段目标、77 个工作包、119 个验收场景。工作包中 74 个属于阶段核心范围，3 个明确为追加/增强：M2-T11 CMMLU、M4-T10 交互命令、M4-T11 Inspect 日志导入。数量用于定位和覆盖核对，不用于冒充已经运行的测试数量。

### 总路线原件的处理

`docs/ROADMAP.md` 由上一轮附件 `MoTTEavl_Roadmap_2026-09-19.md` 原样保存，Git blob 为 `eef350618e48c35880c9321a4e20946479e0444e`。原文中的“未修改 GitHub 仓库”“拟议落位”描述它最初生成时的状态，不是本次提交后的状态。本目录就是后续细化和落库记录；不改写原始快照以掩盖规划演变。

M0 是已经合入的完整性修复基线，不另建一份“从零实现 M0”的计划。维护其回归属于所有阶段共通要求。任何后续新增问题都应关联具体交付任务，而不是把全部路线重新退回无限期基础建设。

## 2. 顺序与依赖

默认单人顺序：M1 最小完整闭环 → M2 → M3 → M4 → M5 → M6 收口 → M7 发布切换。阶段号表示成果组织，不表示所有任务必须等待前一个阶段的全部页面完成。

```text
已有 M0 完整性基线
  └─ M1 契约/评分/受控工具
       ├─ M1 原生 Agent 完整用户链路
       ├─ M2 ExternalJob → C-Eval
       │    └─ M3 Task/Trial → Harbor/Terminal-Bench
       ├─ M4 Pi / Claude / Codex
       └─ M5 Workflow / Skill / Judge

M6-Lite T01–T03：随 M2 交付，同一比较与 Gate 实现
M6-Full：消费 M2/M3/M4/M5 的实际证据类型并收口
M7：打包、安全、备份从前期持续推进，最终进行迁移和发布验收
```

| 阶段 | 必须先就绪 | 不必等待 | 向后交付的稳定边界 |
|---|---|---|---|
| M1 | 当前主干、离线 fixture、受控 workspace | 外部 Runner、Pi、完整统计 | Observation、Evaluator、AgentResult、调用日志与受控工具 |
| M2 | M1 证据/评分边界、现有 Backend/Plugin | M1 全部 UI、M4、M5 | ExternalJob、数据/Profile、幂等采集、native/diagnostic 指标 |
| M3 | M2-T01/T02/T03、M1 Artifact/指标 | C-Eval UI 收尾、M4 | TaskIdentity、Trial、VerifierObservation、环境/清理证据 |
| M4 | M1 runtime/工具/取消/评分边界 | Harbor 全部完成 | RuntimeProfile、model/tool control、session、可观察能力 |
| M5 | M1；基础比较引用可用 | 真 Pi 或所有 CLI、完整实验系统 | Workflow、SkillVersion、JudgeSpec、校准与人工干预 |
| M6 | Lite 仅需固定报告；Full 需各类型证据 | Lite 不等待 M3/M5 | ComparisonPolicy、Experiment、Baseline、Gate、退出语义 |
| M7 | SDK 可分步；最终需必备成果与支持证据 | 历史全部导完不阻塞新功能开发 | 安装产物、迁移报告、一致备份、发布/切换记录 |

外部环境阻塞 M3 时，可以推进已满足依赖的 M4/M5，不能把它们误写成 Harbor 的硬依赖。建议同时只推进一个主功能加一个验证工作包，减少在共享契约上交叉修改。

## 3. 跨阶段的唯一实现责任

| 能力/对象 | 主要负责阶段 | 其他阶段如何使用 |
|---|---|---|
| RunDispatcher、CAS、执行锁、CaseAttempt、needs_review | 已有基线 | 全部复用，不建立第二套运行主权 |
| Observation、MetricResult、Evaluator、InvocationRecord | M1 | M2–M5 产出/消费同一证据和指标约定 |
| ExternalJob、启动 token、采集 checkpoint | M2 | M3 复用；M4 可复用 supervisor 能力 |
| TaskIdentity、TrialPlan/Result、VerifierObservation | M3 | M6 在其上做统计；不能把 transport retry 当 Trial |
| RuntimeDefinition/Profile、model_control、session | M4 | M5 按能力注入 Skill/驱动多轮，不假设所有 runtime 相同 |
| WorkflowVersion、Fixture、SkillVersion、JudgeSpec | M5 | M6 把这些版本和干预信息纳入比较条件 |
| ComparisonPolicy、coverage、Baseline、Gate | M6 | M2-T09 实施 Lite 子集，但不另建 C-Eval 私有 Gate |
| ScoringPass/ScoreSet | 已有基线，M1/M5 扩展评分用途 | M6/M7 必须引用具体 pass，不追随 current 指针 |
| API 客户端、ImportManifest、恢复/发布产物 | M7 | 使用前述公共契约，不复制执行器/Parser 逻辑 |
| UI 状态、设计 token、公共组件 | 全阶段，项目 DESIGN.md 管理 | 不为每个 suite 新建独立状态机和样式系统 |

所有拟新增对象、方法、路由、命令、文件与测试路径都是规划目标，不应被描述成当前已有 API。实施时以最新主干检查同职责模块是否已经存在；存在则增量扩展并更新路径映射，不能因为计划写了“新增”就创建重复实现。

## 4. 全局约束

### 4.1 产品与架构

保持单用户、API-first、Python/FastAPI/Pydantic、React/Vite、现有 uv/pnpm workspace。沿用单执行器及 SQLite/PostgreSQL 支持，不把多 Worker、Kubernetes、多租户、RBAC 或组织审批作为功能前置。

API 负责校验、提交、查询；不在请求进程里执行不可信代码、工具、付费 Benchmark 或 Judge。执行和评分用途可以不同，但复用 Worker 执行权、已有资源安全与审计，不引入隐藏执行通道。

Provider 协议、ExecutionBackend、BenchmarkPlugin、Evaluator 是不同扩展点。不能通过注册 Provider 假装接通 Agent，也不能通过创建页面假装后端可执行。

### 4.2 数据、恢复与版本

Run、Case、Trial、传输重试、操作员 retry 和评分 pass 的身份分开。外部请求或副作用可能已发生但尚未落库时，保留 indeterminate/needs_review，不自动完整重放，不承诺外部 exactly-once。

已发布资源与终态评分不可覆盖。模型、数据、Profile、runtime、Skill、Judge、scorer、parser 和环境版本固定在相应快照。新算法、新评分规则、新资源版本只生成新记录；历史 source/原始结果仍可追溯。

新的 storage migration 按执行时的实际 head 追加，不在多份并行计划中争用硬编码 revision。SQLite/PG/InMemory 的语义测试保持一致；新增 schema 必须覆盖旧读兼容与回退限制。

### 4.3 证据与评分

运行完成、任务通过、证据足够、结果可比、Gate 放行是不同判断。进程退出 0 或模型说“完成”不是通过证据。

MetricResult 是评估视图，持久化仍进入既有 ScoreSet。每次正式结果引用 Run + ScoringPass。未知、缺失、未尝试、不适用和失败不统一填 0，也不静默过滤；合法指标分母由注册政策决定。

原始 Runner 指标与平台诊断指标分开。证据范围不完整时，不能根据“没看到违规”判定无副作用。用户已授权的隐私数据仍需在发布/日志边界脱敏，gold/隐藏断言不进入 subject 输入。

### 4.4 费用与不可信执行

默认测试不使用真实模型或个人订阅。subject、Judge、原生 Harness 的付费动作分别显式授权，预算与已知/未知费用必须说明。GET/report/compare/gate 不触发新的付费调用。

LLM Judge rescore 不调用被测模型，但可能调用 Judge 并收费；不能继续笼统宣称所有 rescore 零网络。新的评分作业在完成前不替换有效的 current ScoringPass。

工具按权限交集执行；Skill 不自动授予权限。不可信任务不获得宿主凭据目录、数据库或 Docker socket。可信 Runner 的环境管理权限与任务容器权限分开记录；改变原生 Benchmark 环境语义时产生自定义 Profile，不再冒充完全同口径。

### 4.5 前端与接口

遵守根 `AGENTS.md` 与 `apps/web/DESIGN.md`。样式引用 `apps/web/src/index.css` token；状态从 STATUS_META 派生；继续使用指定 Radix/Phosphor 体系。不重做整体视觉风格，不在不同 suite 维护第二份 Provider/Run/评分逻辑。

每条能力提供配置/预检、执行或明确不支持、事件、样本/产物、评分/不评分原因、结果与错误反馈。API/CLI/Web 使用同一事实源和公共类型；前端不能根据名称或缺字段猜测执行成功。

## 5. 迁移决策 RD-001

本次用户明确要求以 MoTTEavl 为主线并迁移旧平台能力，故将总设计早期“只参考、不复用代码”的约束修订为下列规划决定：

允许迁移独立 Parser、评分/校验算法、DSL 语义和已授权测试资产；每项必须记录来源提交、保留/修改语义、依赖与许可、测试对照。禁止整套迁入旧 API/数据库/权限/Celery 控制面，禁止将私有业务数据、原始凭据、会话或未获许可的数据集公开提交。

RD-001 不自动批准运行旧代码或进行数据切换。具体实施变更随阶段 PR 更新设计/兼容文档；历史数据导入与功能迁移分开验收。新代码长期主线只有 MoTTEavl，不建立双写/双调度系统。

## 6. R1–R10 与阶段任务映射

| 总路线近期任务 | 对应阶段工作包 | 开工/完成条件 |
|---|---|---|
| R1 通用 Observation/Evaluator | M1-T01/T02/T03 | 同一冻结证据产生多指标，旧 pass 不变 |
| R2 Builtin Agent backend | M1-T04/T05 | 实际分发、正确消息/工具历史与模式 |
| R3 文件/Artifact 纵向链路 | M1-T06/T07/T08 | 路径/预算/取消/未知副作用与证据完整 |
| R4 Agent 用户工作区 | M1-T09，最终纳入 T10 | Web/CLI/API 对同一 Run 一致 |
| R5 外部 Job | M2-T01/T02/T03 | 一 Job 多 Case、句柄、取消与幂等采集 |
| R6 C-Eval 数据/Profile/Parser | M2-T04/T05/T06 | 固定来源与原始/诊断分数对照 |
| R7 C-Eval 产品链路 | M2-T07/T08 | 公共入口可预检、执行、读取结果 |
| R8 C-Eval 真实验收 | M2-T10 | 固定范围、明确授权和实际证据 |
| R9 Harbor Trial 导入 | M3-T01/T02/T04 | 稳定身份、多 Trial、Verifier 错误区分 |
| R10 比较与覆盖门禁 | M6-T01/T02/T03，在 M2-T09 交付 | 同一算法，不因缺样本而误放行 |

最近一批从 M1-T01 开始，依次形成评分和原生 Agent 文件任务闭环。M2 ExternalJob 只需要 M1 的接口边界稳定，不要求等 Pi、所有 CLI 或完整统计系统。

## 7. 工作包执行与完成标准

每个工作包都是有明确输入/输出、文件触点和验收反例的独立评审单元。执行前读取对应 M 文档、当前相关代码与测试。后期阶段的具体函数签名以已合入前置契约为准，不能机械执行过时路径。

标准步骤：

1. 核对当前主干与本计划差异，保留已有实现和用户工作；仅更新本任务范围。
2. 先增加该任务规定的失败案例、合成输入和期望输出，运行 focused tests 确认缺口。
3. 实现最小有效链路，明确错误、缺失、取消与版本语义；不以空目录/伪造成功交差。
4. 运行 focused、相邻回归、公共契约/类型、适用的 UI 和构建检查。
5. 更新操作说明、能力矩阵、迁移差异和验证记录，提交一个可独立评审的变更。
6. 取得实际验证证据后才勾选对应目标；未满足项记录原因，不用“后续再补”当作已完成。

可以在 `docs/superpowers/plans/` 为一个实际开工工作包进一步写逐文件/TDD 微计划。本目录不是允许一次性自动执行全部 M1–M7 的授权，也不是一份已经通过运行验证的实现代码。

### 四个状态维度

| 维度 | 建议值 | 说明 |
|---|---|---|
| 规划/实现 | planned / implemented | 本次新增文档属于 planned |
| 协议 | not_ready / ready | 有效格式或握手不等于真实任务可执行 |
| 执行 | unavailable / experimental / supported | 具体版本、环境和任务范围需要证据 |
| 验证 | not_run / passed / failed / blocked / not_applicable | 每个测试层独立记录，不把 skip 当 passed |

## 8. 统一验收与证据记录

所有阶段至少覆盖：配置不支持、缺凭据、外部错误、工具失败、超时、取消、执行中断、证据缺失、畸形结果、重复采集、评分失败、费用未知。具体后端不适用的场景要说明理由，不简单省略。

| 层级 | 证据要求 | 不能据此推断 |
|---|---|---|
| Contract/单元 | 测试、命令、退出码与输入/输出断言 | 真实服务或模型可用 |
| Golden/Replay | 固定原始文件 hash、预期结果、parser/scorer 版本 | 官方数据真实性或模型真实质量 |
| 服务集成 | API/存储/Worker/本地 HTTP 或 CLI、Docker/PG 的实际结果 | 所有平台、所有 Runner 版本均兼容 |
| 显式 live | 模型/runtime/数据/Profile、授权范围、费用/限制、原始工件 | 未执行的任务/组合已正式支持 |
| 发布/运维 | 干净安装、升级、备份恢复、旧读兼容 | 仅凭 compose config 或 CI 总体成功就完成部署 |

### 实施时的验证记录格式

```yaml
milestone: M1
work_package: M1-T01
implementation_commit: required-before-acceptance
source_baseline: a668d13ee5ea0c3613648f8992ecaa4a148d3855
verification_layer: contract
result: not_run
command: required-before-acceptance
environment: required-before-acceptance
evidence_refs: []
limitations: []
```

这是登记格式示例，不是本次已运行记录。接受验收前必须填真实 commit、命令、环境、结果与证据；字段缺失或仍为未运行时自动视为未通过。

建议每阶段使用 `docs/verification/MN.md` 汇总实际实施证据；这些验证文件将在实现时创建，本次只提交规划，不预填虚假通过记录。

## 9. 必备、追加与稳定发布范围

| 内容 | 归类 | 不允许的处理 |
|---|---|---|
| M1 原生 Agent、确定性评分与产物 | 必备 | 只提交 Agent 名称/页面而没有执行 |
| M2 C-Eval 全链路 | 必备 | smoke 冒充完整 Profile 成绩 |
| M2 CMMLU | 追加 | 直接继承 C-Eval 的支持验证 |
| M3 明确版本 Terminal-Bench | 必备 | “Harbor 能跑”代替平台验收 |
| M4 真 Pi、Claude/Codex batch | 计划必备 | 没完成时静默从 v1 范围删除；调整需记录 |
| M4 交互命令、Inspect 导入 | 增强/扩展 | 缺消费者仍返回假送达 |
| M5 主流程、Skill fixture/A-B、Judge 校准链路 | 必备 | 静态 Skill 校验当行为有效，未经校准 Judge 放正式门禁 |
| M6 比较、基线、覆盖与 Gate | 必备 | Run completed 当放行，或分母/版本漂移 |
| M7 安装、迁移、安全与恢复 | 必备 | 删除旧数据或把未知历史信息自动补齐 |

发布候选必须确认三个替代场景：C-Eval 模型比较、Harbor Agent Benchmark、多轮 Scenario/Skill 回归。各 backend 的支持级别按真实版本/环境登记；任何范围删减写出原因、影响、保留接口和后续条件，不用一个笼统“v1 已完成”遮盖未验收能力。

## 10. 文档变更与后续维护

本次分支范围只有 master roadmap、M1–M7 和本索引；不修改应用代码、不运行付费评测、不迁移数据库、不自动创建 PR 或合并 main。阶段复选框保持未完成，是对待实施状态的准确记录。

总路线原件保持历史快照。新的范围、依赖或契约决定在本索引/对应 M 文档修订并记录理由；不能改某个 M 的字段而不更新其消费者。跨阶段冲突优先保持当前已发布契约，通过显式版本演进解决。

文档检验至少核对：九个目标文件存在、M1–M7 无遗漏、目标/任务/验收 ID 唯一、相对链接正确、所有当前事实有固定来源、拟新增接口标注清楚、只有文档发生变化、main 未被更新。它与实现时的 `make check` 是两类验证，不应混报。
