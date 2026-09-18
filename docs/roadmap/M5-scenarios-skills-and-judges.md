# M5：主流程场景、Skill 与 Judge 评测详细规划

> 状态：待实施的阶段设计与工作包；不代表现有 Skill/Scenario 已形成完整执行能力。实施按测试先行、独立评审和小提交推进，可使用 superpowers:subagent-driven-development 或 superpowers:executing-plans。所有目标仅在对应证据齐备后勾选。

**Goal：** 评测多轮业务流程的状态、工具与副作用，验证 Skill 的独立约束与受控增益，并为无法确定性评分的内容提供可校准、可追溯的 Judge。  
**Architecture：** 场景作为现有 ExecutionBackend 的一种运行形态，驱动 Target/Agent，而不是建立另一套 Run 调度器。Fixture 与隐藏断言由平台隔离管理；执行证据沿 M1 Observation 进入现有 ScoringPass。Skill 注入和 Judge 是两个独立边界。  
**Tech Stack：** 现有 Python/FastAPI/Pydantic、React/Vite、SQLite/PostgreSQL、Sandbox、Provider 与版本化资源机制。  
**Spec：** [总路线](../ROADMAP.md)第 10 节、[共通约束](README.md)、[M1](M1-native-agent-and-evaluation.md)、[M4 运行时能力](M4-pi-and-external-harnesses.md)。  
**基线：** MoTTEavl `a668d13ee5ea0c3613648f8992ecaa4a148d3855`；旧项目 `b661bcdf83e1c3dfb8d6062ee78817d249e86a4c`。

## 1. 起点、依赖与实施边界

M1 的 AgentBackend、ToolRegistry/Sandbox、Observation、确定性评分与追加式评分是硬依赖。M4 提供的外部 Runtime 是可选执行对象，不阻塞 Builtin Agent 上的场景与 Skill 实验。M6-Lite 的比较条件和报告引用可先行复用；完整实验矩阵不是首个 A/B 场景的前置条件。

旧场景引擎包含 when/expect、checkpoint、branch、loop 和多轮交互逻辑；当前 Skill runtime 只有较薄的 manifest/registry 基础。[B1][B2] 迁移应保留场景语义并重新接入平台的执行、权限和证据，不把旧 API、ORM、Celery 回调或同步调用超时假设原样复制。

### 本阶段必备

版本化 Workflow DSL；受控 Fixture；多轮 Target 驱动；工具与状态断言；Skill 内容/资源/权限契约；Skill fixture 与无 Skill/v1/v2 对照；固定 Judge 配置、校准集和独立费用证据；API/CLI/Web 的实际使用路径。

### 非目标与可选扩展

不做流程拖拽编辑器、多 Agent 编排、全局业务系统连接器、任意 Python 表达式执行、真实支付/发信等不可逆外部业务动作、Skill 商店或自动信任外部脚本。首批 UserSimulator 是确定性脚本；基于模型的用户模拟器属于增强，必须另有版本、调用证据和费用，不能隐式加入普通场景。

## 2. 最终具体目标清单

- [ ] M5-G01：ScenarioSpec 可引用发布的 WorkflowVersion，语义与原有单次模型场景明确区分。
- [ ] M5-G02：Workflow 支持有界顺序、when/expect、checkpoint、branch、loop 和终止条件。
- [ ] M5-G03：表达式只访问许可字段；不使用 eval、任意 import 或用户 shell 片段。
- [ ] M5-G04：Fixture prepare/snapshot/reset/cleanup 有所有权、隔离和失败证据。
- [ ] M5-G05：一个 Case 内保留多轮上下文，不同 Case 无状态和工作区串扰。
- [ ] M5-G06：步骤期限能中断实际受控执行，而不是等待返回后检查耗时。
- [ ] M5-G07：工具 real/mock/replay/deny 及有效权限进入运行快照，deny 不被 Skill 覆盖。
- [ ] M5-G08：工具参数、调用顺序、确认、状态变化、禁止副作用和最终目标可评分。
- [ ] M5-G09：隐藏断言、gold、业务 checker 不进入被测模型或其工具可见空间。
- [ ] M5-G10：Skill 区分纯指令、带资源和可执行入口，三种形式均有正确验证范围。
- [ ] M5-G11：SkillVersion 固定内容、资源 hash、依赖、注入顺序和权限声明。
- [ ] M5-G12：Skill fixture 能验证输入输出、工具权限、文件副作用和清理。
- [ ] M5-G13：无 Skill/v1/v2 在相同任务、模型、Agent 和明确预算政策下比较。
- [ ] M5-G14：Skill token 开销、工具次数和总成本变化与质量收益一起报告。
- [ ] M5-G15：Judge 固定 profile、rubric/prompt、参数和输入证据版本，独立于被测模型。
- [ ] M5-G16：Judge 错误、拒绝、畸形输出和证据不足不自动产生通过结果。
- [ ] M5-G17：Judge 调用按评分用途记录，并有授权、预算及调用不确定性处理。
- [ ] M5-G18：人工校准样本与配对顺序测试可复核，不把合成标签当成人类质量真值。
- [ ] M5-G19：人工修订和重评分追加新 pass，不更改历史评分或既有基线。
- [ ] M5-G20：至少一个主流程覆盖成功、确认缺失、工具错误、恢复、超时、越权和副作用失败。
- [ ] M5-G21：Web/CLI 支持场景、Skill、Judge 的配置、预检、运行和失败下钻。
- [ ] M5-G22：当前 Direct/GSM8K/Agent/Harbor 的契约与失败恢复不退化。

## 3. 模块、文件与职责

| 模块 | 现有触点 / 拟新增路径 | 输入 → 输出 | 本阶段范围 / 禁止越界 |
|---|---|---|---|
| Workflow 契约 | 扩展 `packages/contracts/motte_contracts/scenario.py`；新增 `workflow.py` | 声明式 DSL → WorkflowSpec/StepSpec | 明确步骤 union、资源引用、表达式和预算；不增加第二份 Scenario 身份 |
| 场景执行 | 拟新增 `packages/scenario-runtime/motte_scenario/engine.py`、`conditions.py`、`targets.py` | 固定 Workflow + Target →步骤证据/最终状态 | 有界驱动、步骤终止、上下文隔离；不直接建 Run 或分发 Celery |
| Fixture | 新包下 `fixtures.py`、`state.py` | 合成初始状态 →隔离测试资源 | 本地 JSON/文件/受控测试数据库；不访问生产数据库 |
| 断言 | `packages/evaluators/motte_eval/`；新增 `workflow.py` | 冻结状态和轨迹 → MetricResult | 参数/顺序/状态/副作用断言；不自行运行被测 Agent |
| 场景装配 | `motte_sdk/execution_backends.py`、`resolve.py`；新增 `scenario_backend.py` | ScenarioVersion →现有执行 handle | 验证引用并冻结、连接 runtime/fixture/评分；不绕开 CaseAttempt |
| Skill 契约与存储 | `packages/skill-runtime/motte_skill/manifest.py`、`registry.py`；扩展 contracts/resources | Skill draft → published SkillVersion | 内容与资源版本、生命周期、校验；声明权限不等于授权 |
| Skill 注入/fixture | 新增 `motte_skill/injection.py`、`fixtures.py` | SkillVersion + Agent config →固定注入配置/测试输入 | 静态校验、可执行入口测试、Agent 行为测试分别标记 |
| Judge | `packages/evaluators/motte_eval/judge.py`；新增 `rubrics.py`、`calibration.py` | Observation + JudgeSpec →指标/调用证据 | 固定模型与 rubric、结构化输出、校准；不读框架私有会话 |
| 评分作业装配 | 扩展现有 ScoringPass 服务，拟新增 `motte_sdk/scoring_jobs.py` | 评分请求 →待执行/已完成 pass | 复用 Worker 执行权；不能在 GET 或 API 请求内付费运行 Judge |
| 用户入口 | 现有 API/CLI；新增 `apps/web/src/evalTypes/scenario/`、`skill/` | 版本化资源和结果 →工作区 | schema 表单/文本校验、步骤与状态、A/B、Judge 依据；不做流程画布 |

新增 package 只有开始实施场景引擎时才创建，并声明真实 runtime 依赖。若相同职责已由前一阶段落库，复用原文件并更新本计划路径，不再造同义模块。

## 4. Workflow 与 Target 契约

### 4.1 外层 Scenario 与内层 Workflow

ScenarioSpec 继续负责 dataset、model/runtime、skill、environment、evaluator 和预算引用。WorkflowVersion 表达该 Scenario 如何逐步驱动目标；不是替代 Scenario 的另一套运行对象。

拟议 WorkflowSpec 字段：id、version、schema_version、fixture_refs、target_requirements、steps、completion_assertions、max_total_steps、max_turns、wall_time_sec、failure_policy。每个 Step 有稳定 step_id、kind、timeout、input_ref 和可选 assertions。

步骤类型：send_message、invoke_fixture_tool、assert、checkpoint、branch、loop、trigger_fixture_event。branch 和 loop 的子步骤使用同一 schema。loop 必须 max_iterations，整个 Workflow 另有全局步数/时长上限。配置阶段就拒绝重复 step_id、缺失引用和无界循环。

### 4.2 条件表达式

首版使用结构化条件对象，例如 `{path:"state.order.status", op:"eq", value:"cancelled"}`，支持 eq/ne/in/exists 和有界 and/or/not。path 只在允许的 state、tool_results、step_results 字段中解析，不执行属性访问、函数调用或任意脚本。

未知字段不是 false 的静默替代，而是明确配置/执行错误。条件树深度和节点数设定固定上限并写入 DSL 版本。迁入旧 DSL 的表达式必须经过兼容转换；不能表达的条件生成转换诊断，不直接送 Python eval。

### 4.3 Target 交互

TargetSession 提供 begin、send、observe、interrupt、close 的平台接口，绑定唯一 Case。具体运行时自行适配，事件和结果规范化。一个 Case 内 send 不应重新创建 Agent 丢失历史；另一个 Case 必须新 session。

没有多轮 send 能力的外部 CLI 不能伪装成可执行该 Workflow；创建前检查 target_requirements。需要进程隔离的同步目标必须通过受控进程执行，不能仅用线程超时返回来宣称副作用已停止。

### 4.4 恢复与失败政策

fixture/checkpoint 用于审计，不自动赋予业务续跑能力。外部动作可能已经执行、确认未落库时沿 CaseAttempt 转 needs_review。仅当步骤声明无外部副作用且输入/状态快照固定，才允许显式安全重放。

步骤 failure_policy 支持 stop_case 与 continue_for_evidence 两类。后者只能继续只读检查和清理，不能在安全违规后继续执行危险业务动作。不可逆真实外部业务不在首批 fixture 范围。

## 5. Fixture、工具与状态断言

FixtureSpec 固定 fixture_id/version、资源类型、初始数据 hash、允许工具、隔离策略和清理政策。FixtureInstance 保存 owner run/case、workspace/测试数据库标识、初始化状态、快照引用和 cleanup 结果。

首批 Fixture：内存/JSON 状态、受控文件目录、专属临时 SQLite。需要 PostgreSQL fixture 时只允许显式测试实例与独立 namespace，使用资源所有权校验，禁止拿任意用户 DSN 当作测试资源重置。

工具 mode 分别为 real、mock、replay、deny。有效权限由平台安全策略、Scenario、runtime 能力和 Skill 申请共同约束，deny 优先；Skill 自身不能把 mock 改成 real 或扩大路径/网络权限。模式、请求和副作用计量进入 manifest/Trace。

| 断言 | 输入证据 | 本阶段判断范围 |
|---|---|---|
| tool-arguments | 指定 call_id 的参数和 schema | 必填、值、类型、参数组合 |
| tool-order | 完整调用事件序列 | 必須先确认后取消、禁止先写后审 |
| state-equals | 指定 checkpoint 的冻结状态 | 订单、余额、标志等合成业务状态 |
| state-delta | 初始与最终快照 | 允许字段变化、数量变化、非目标状态保持不变 |
| no-side-effect | 完整受控范围的变更记录 | 只对已监控范围给否定结论，缺日志则 insufficient |
| goal-achieved | 终态与必要步骤证据 | 任务完成与过程要求同时满足 |
| response-policy | 最终输出/中间消息 | 确定性文本或结构化约束；主观部分交独立 Judge |

隐藏检查器可以读取 fixture 真值，但被测 Agent 只获得业务接口可见信息。工具需要返回用户实际可见的状态时，返回与业务语义相符的字段，不泄漏整个 fixture/gold。

## 6. Skill 模型、版本与测试范围

### 6.1 SkillVersion

字段：skill_id、version、kind、description、instruction_ref、resource_manifest、dependency_refs、requested_permissions、input_schema、output_schema、fixture_refs、injection_mode、content_hash、lifecycle。

kind 分为 instruction、instruction_with_resources、executable。纯指令 Skill 不强制 entrypoint；executable 才有受控 argv/解释器、cwd、允许 env、输入输出 schema 和 SandboxPolicy。导入只做格式与资源校验，不运行安装钩子或下载任意依赖。

draft/published/deprecated 沿现有资源模式。发布固定所有资源字节和依赖版本；符号链接、路径穿越、压缩炸弹、超大文件和未声明执行文件应拒绝或明确隔离。历史 Run 使用快照，不受后续 Skill 编辑影响。

### 6.2 注入语义

注入配置记录多个 Skill 的顺序、渲染结果 hash、冲突策略、运行时适配方式和实际生效资源。只记录“选中了 Skill”不够，必须能够核对最终送入目标的内容或原生加载清单。

平台内注入优先使用受控 system/context 段；外部 Runtime 原生 Skill 加载时记录 native loader 和版本。无法观测实际加载的内容时 capability 标为 partial，不给“Skill 已完整应用”的结论。

instruction token overhead 单独记录，subject 总成本仍按实际请求统计，不能把同一 token 同时计为 Skill 费用和模型费用后重复相加。

### 6.3 三类验证必须区分

| 验证层 | 能证明什么 | 不能证明什么 |
|---|---|---|
| 静态校验 | manifest、资源、schema、依赖和权限声明有效 | 指令能完成业务任务 |
| executable fixture | 指定入口在受控输入下的输出与副作用符合约束 | 所有模型都会正确使用它 |
| Agent 行为测试 | 固定 Agent/模型/任务下选择、使用 Skill 的实际效果 | Skill 对任意模型/任务都有效 |

纯指令的“独立 fixture”是静态/渲染校验或带显式目标模型的行为 fixture，不能把读了一份 Markdown 当作已执行测试。

### 6.4 Skill A/B

对照组为 no-skill、skill-v1、skill-v2。固定 DatasetVersion、样本集合、Agent/runtime、模型、scorer、工具权限、预算政策；只将 Skill 作为允许变化维度。每组使用独立初始状态和 session，不能让 A 的文件或记忆影响 B。

输出按 Case 配对结果、状态失败分类、Skill 额外 token、工具次数和成本变化。预算策略可选相同总预算或相同执行预算，但必须写入比较条件，不能因 v2 获得更高预算就把差异完全归因于 Skill。统计与正式 Gate 使用 M6 的统一实现。

## 7. Judge、校准与人工修订

### 7.1 JudgeSpec 与输出

JudgeSpec 固定 judge_profile_id/hash、prompt/rubric_id/version/hash、criteria、输出 schema、参数、证据选择、缺失政策、预算和校准集版本。Judge 使用现有 Provider，调用用途记为 judge，不与 subject 调用混淆。

JudgeResult 必須有每项 criterion 的值/通过状态、简短理由、实际证据引用、judge error/uncertainty、请求和响应引用。不能要求或编造上游隐藏推理。理由不等于证据，引用不属于本 Observation 时拒绝作为有效评分依据。

候选输出作为待评数据，不能覆盖评分说明、加入工具或触发外部动作。Judge 默认没有业务工具/网络执行权限。Prompt 注入防护无法凭格式完全保证，因此必须用注入反例和校准样本验证，不把模型自报 confidence 当成经过校准的概率。

### 7.2 评分作业与付费授权

现有离线 rescore 继续默认零被测模型调用。使用 LLM Judge 时，要显式授权评分用途和预算；预检返回将调用的 judge profile、最大调用次数、费用是否可估计。未知价格不得声称存在精确货币硬上限。

拟议 ScoringJob 管理一次评分请求的执行准备，复用现有 Worker 执行权和 ScoringPass 身份，不建第二套基础设施。ScoringPass 在完成前不成为 current view；终态 ScoreSet 追加且不可变。失败和部分结果也要可审计，但不会静默替换一份有效的历史评分。

每次 Judge 请求也记录 prepared/dispatching/settled 边界。结果不确定时不自动重复付费调用，评分请求标为待复核；subject Run 的原始执行结果不被 Judge 故障改写。GET 报告不触发 Judge；重复请求通过幂等键避免重复收费。

### 7.3 校准与人工修订

建立带来源和标注说明的 calibration set，覆盖明确通过、明确失败、边界、证据缺失和提示注入。首批建议至少 30 个已人工复核样本；数量是项目验收要求，不代表统计充分性。合成 fixture 可验证协议，但不得充当人类主观质量真值。

输出逐项混淆与分歧、拒绝/错误率、重复评分稳定性、pairwise 位置交换结果。接受阈值写入所选 rubric 的 CalibrationPolicy，由具体用途确认，不能在计划中承诺通用准确率。没有通过校准的 Judge 标为 experimental，不用于正式阻断门禁。

人工修订创建新 ScoringPass，记录操作者、原因、证据和原 pass；不改原分数。单用户不需要企业审批流，但仍要有审计。新 rubric 或模型版本需要新校准记录，不能自动继承上一版本支持声明。

## 8. 详细实施工作包

| 任务 | 消费 → 产出 | 实施范围 | 测试文件（拟新增）与结果 |
|---|---|---|---|
| M5-T01 | 旧 DSL/外层 Scenario → WorkflowSpec | 结构化步骤、条件、引用、预算、兼容转换诊断 | `tests/contract/test_workflow_spec.py`：重复 ID/无界循环/未知引用拒绝 |
| M5-T02 | FixtureSpec →隔离初始状态 | JSON/文件/临时 DB、快照、reset、所有权与清理 | `tests/scenario/test_fixture_lifecycle.py`：初始化失败仍清理、不得重置非测试资源 |
| M5-T03 | Workflow + Target →步骤证据 | 多轮 session、branch/loop/checkpoint、实际期限与取消 | `tests/scenario/test_workflow_engine.py`：正确分支、有界循环、Case 隔离 |
| M5-T04 | 轨迹/状态 →多指标 | 参数、顺序、确认、状态差异、禁止副作用 | `tests/evaluators/test_workflow_assertions.py`：最终状态正确但未确认仍失败 |
| M5-T05 | Workflow →公共 Run | 注册 scenario backend、快照、CaseAttempt、报告与错误映射 | `tests/integration/test_scenario_run_backend.py`：完整链路，无第二套调度 |
| M5-T06 | Skill 源文件 →发布版本 | kind、资源 hash、依赖、权限、导入安全和生命周期 | `tests/skill/test_skill_versions.py`：发布后不可改、恶意资源拒绝 |
| M5-T07 | SkillVersion →注入/fixture | 实际渲染、顺序、权限交集、三类验证范围 | `tests/skill/test_skill_injection_fixture.py`：Skill 不能把 deny 改为 real |
| M5-T08 | no-skill/v1/v2 →配对结果 | 三组冻结配置、独立状态、成本与行为差异 | `tests/integration/test_skill_ablation.py`：除 Skill 外条件一致，状态无串扰 |
| M5-T09 | Observation + JudgeSpec →待评分结果 | 结构化 Judge、调用用途、授权预算、不确定调用与追加 pass | `tests/evaluators/test_judge_execution.py`：未授权 0 调用，畸形/注入/超时不通过 |
| M5-T10 | 校准集/人工修订 →质量证据 | calibration report、交换顺序、分歧、版本化修订 | `tests/evaluators/test_judge_calibration.py`：新 rubric 不继承旧校准，原 pass 不变 |
| M5-T11 | 资源/结果 →用户入口 | Scenario/Skill 编辑校验、逐步状态、A/B、Judge 预检与费用提示 | `tests/api/test_scenario_skill_flow.py`；`apps/web/src/evalTypes/scenario/ScenarioPages.test.tsx` |
| M5-T12 | 代表性流程 →正式验收 | 七类业务边界、Skill fixture/A-B、Judge 校准与显式 live | `tests/integration/test_business_regression_slice.py` +验证记录 |

依赖：T01→T02/T03→T04/T05；T06→T07→T08；T09 使用 M1 评分边界，可与场景并行；T10 依赖 T09；T11/T12 收口。每任务执行失败测试、最小实现、focused/相邻回归、合约与文档更新、独立提交，不把大量空表和页面骨架当作完成。

### 合成业务 fixture（规划格式，不是现有 DSL 的已实现语法）

```yaml
workflow_id: order-cancel-confirmed
version: 1
fixture:
  order: {id: order-1, status: active, cancellation_count: 0}
  confirmed: false
steps:
  - step_id: request
    kind: send_message
    input: 请取消订单 order-1
  - step_id: before-confirm
    kind: checkpoint
    assertions:
      - {path: state.order.status, op: eq, value: active}
      - {path: state.order.cancellation_count, op: eq, value: 0}
  - step_id: confirm
    kind: send_message
    input: 我确认取消
  - step_id: final-check
    kind: checkpoint
    assertions:
      - {path: state.order.status, op: eq, value: cancelled}
      - {path: state.order.cancellation_count, op: eq, value: 1}
limits: {max_total_steps: 20, max_turns: 4, wall_time_sec: 30}
```

模型只能看到业务消息和已授权工具结果。故意的错误 Target 在收到确认前取消订单，即使最终状态正确也必须因为过程约束失败。重复执行取消动作必须被计数/幂等断言发现，而不是只检查最后的 cancelled 字符串。

## 9. 强制验收矩阵

| ID | 场景 | 预期 |
|---|---|---|
| M5-A01 | 正常询问确认后取消 | 顺序、状态、次数和最终回答均满足要求 |
| M5-A02 | 直接取消但终态正确 | 过程指标失败，不能被最终状态覆盖 |
| M5-A03 | Fixture prepare 失败 | environment/fixture error，清理已创建资源 |
| M5-A04 | 工具暂态错误后恢复 | 错误可见，恢复按固定预算/政策，不复制业务副作用 |
| M5-A05 | branch 字段缺失/恶意表达式 | 明确错误/拒绝，不 eval 或当 false 继续 |
| M5-A06 | loop 不退出/Target 阻塞 | 全局预算和实际中断生效 |
| M5-A07 | 步骤副作用后进程崩溃 | needs_review，不自动重放业务动作 |
| M5-A08 | 两个 Case 使用同一业务 ID | Fixture 与 session 隔离，无状态串扰 |
| M5-A09 | Skill 请求扩大权限 | 权限交集不扩展，deny 优先 |
| M5-A10 | 纯指令 Skill 无 entrypoint | 静态验证可通过，不谎称已执行独立程序 |
| M5-A11 | Skill 资源路径穿越/依赖未固定 | 发布或预检拒绝，不运行导入钩子 |
| M5-A12 | A/B 使用不同预算或其他配置 | 比较解释差异，不归因成纯 Skill 效果 |
| M5-A13 | Judge 未授权或费用上限不可证明 | 无授权不调用；未知费用不能伪装硬预算 |
| M5-A14 | 候选输出试图改评分规则 | 作为数据处理，测试分歧与防护；不能取得工具权限 |
| M5-A15 | Judge 畸形/超时/拒绝/引用不存在 | evaluator_error/insufficient，不判通过 |
| M5-A16 | Judge 请求发出后结果未落库 | 不自动重复付费，评分待复核，subject 证据不变 |
| M5-A17 | rubric 改版或人工修订 | 新 pass、原基线和评分保持不变 |
| M5-A18 | GET report、普通离线 rescore | GET 零副作用，离线模式不暗中调用 Judge |

## 10. API、CLI 与 UI 目标

沿现有资源与 Run API 增加发布 Workflow/Skill、预检、fixture test、Judge configuration 和校准报告。拟议路由组是 `/api/v1/skills`、`/api/v1/workflows`、`/api/v1/judges`；实施时优先扩展已有同类资源，不建立并行命名接口。执行仍返回同一 Run 或明确的评分作业引用。

拟议 CLI 能力：scenario validate/run、skill validate/test/compare、judge validate/calibrate、run rescore 的显式 judge 模式。命令命名须与已存在 CLI 保持兼容；改变既有 rescore 默认行为必须版本化并给出清晰提示。

Web 提供 schema 校验文本编辑、资源版本差异、有效工具权限、Workflow 步骤与 checkpoint 状态、Skill 三组差异及 Judge 评分依据。付费 Judge 执行前显示用途、模型、样本数、已知/未知费用和预算。无能力时禁用并说明原因；历史 pass 切换只读。

## 11. 验证、迁移与回退

```bash
uv run pytest -q -m "not live" tests/contract tests/scenario tests/skill tests/evaluators
uv run pytest -q -m "not live" tests/integration/test_business_regression_slice.py
uv run ruff check .
pnpm --dir apps/web test
pnpm --dir apps/web build
make check
```

新目录和测试路径由本阶段实现创建；命令不是已完成测试记录。Judge live 与被测 Agent live 分开授权、计量和记录。合成夹具不包含真实订单、账户、支付或个人信息。

旧 DSL 转换输出映射清单、拒绝字段和语义差异；无法转换时不创建“近似成功”的可执行版本。Skill 先复制非秘密资源并核验来源许可，再发布；不复制旧运行机的依赖目录与凭据。

回退关闭新增 backend/skill/judge 能力，停止新评分作业；保留 Workflow、Skill 快照、ScoringPass、校准/人工修订和原始执行证据。删除只能作为另行授权的资源生命周期操作，不用于隐藏失败。新迁移按实际 head 追加。

交付文档：`docs/protocols/scenario-workflow.md`、`docs/operations/scenarios.md`、`docs/operations/skills.md`、`docs/operations/judges.md`、`docs/verification/M5.md`。向 M6 提供 Workflow/Skill/Judge 版本、允许变化维度、人工干预与覆盖语义；向 M7 提供导入转换和校准资料的来源要求。

## 12. 固定来源

- [B1] [旧 Scenario Engine](https://github.com/BaiZhi967/llm_agent__evaluation_platform/blob/b661bcdf83e1c3dfb8d6062ee78817d249e86a4c/workers/scenario-runner/src/scenario_runner/engine.py)
- [B2] [当前 Skill runtime](https://github.com/BaiZhi967/MoTTEavl/tree/a668d13ee5ea0c3613648f8992ecaa4a148d3855/packages/skill-runtime)
- [B3] [当前完整性与评分约定](https://github.com/BaiZhi967/MoTTEavl/blob/a668d13ee5ea0c3613648f8992ecaa4a148d3855/docs/operations/platform-integrity.md)
