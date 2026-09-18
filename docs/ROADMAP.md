# MoTTEavl 完整产品与迁移路线规划

日期：2026-09-19（UTC+08:00）  
状态：建议路线；未修改 GitHub 仓库，未触发付费调用  
目标主线：`BaiZhi967/MoTTEavl`  
核对基线：`a668d13ee5ea0c3613648f8992ecaa4a148d3855`  
迁移参考：`BaiZhi967/llm_agent__evaluation_platform@b661bcdf83e1c3dfb8d6062ee78817d249e86a4c`

> 核心判断：上一轮完整性修复转入持续回归基线。后续以真实评测能力为交付单位，不再次从零建设调度器、评分历史或 Benchmark 注册表。近期先完成最小 Builtin Agent 评测闭环，再迁移 C-Eval 和 Harbor；界面、证据、评分和发布验证随每条能力同步交付。

## 1. 已确认的起点

PR #5 已合并，合并提交为上述 MoTTEavl 基线，主干对应 CI 的结论为 success。本次核对了相关代码、运行说明、能力矩阵与 CI 状态，并未重新执行完整测试或验证真实付费端点。[S1][S2]

以下能力不再作为新路线中的“从零建设”任务：

| 已有基础 | 后续处理 |
|---|---|
| UUID Run ID、CAS revision、原子 claim | 保留，纳入所有新执行器回归 |
| 单执行器互斥及中断恢复 | 保留，不为功能扩展提前引入多 Worker |
| CaseAttempt、indeterminate、needs_review | 保留，不把未知执行结果自动重试为第二笔费用 |
| RunDispatcher 与 ExecutionBackend 注册 | 扩展真实后端，不建设第二个任务调度中心 |
| GSM8K / Direct LLM BenchmarkPlugin | 扩展插件，不重新进行一轮 GSM8K 解耦 |
| ResolvedManifest v2、资源发布与不可变快照 | 在新资源/外部执行器中复用 |
| 追加式 ScoringPass / ScoreSet | 增加评分能力，不再覆盖历史评分 |
| 请求/报告模型身份与策略证据 | 扩展到外部执行器可观察边界 |
| RunCommand 持久化与不支持时显式拒绝 | 接通真实消费者后才开放交互按钮 |
| Direct LLM JSONL 导入、抽样、exact/contains/regex、比较页面 | 保留，扩展而不是重写 |

对应证据见完整性说明、调度器、Benchmark 插件注册和 Direct LLM 指南。[S3][S4][S5][S6]

当前真实能力边界仍须保留：Direct LLM 与 Replay 是可用执行后端；external-benchmark 有注册但不可用；Builtin Agent、Pi、Claude/Codex 尚未接入可用 Run backend；Pi 默认明确返回执行不可用而不是 echo 成功；Provider 流式和真实端点验证记录仍需推进。协议可用、二进制已安装与端到端可执行不是同一个状态。[S3][S7]

## 2. 产品目标与范围

### 2.1 产品定位

MoTTEavl 是单用户、API-first、可本地运行的 LLM / Agent / Skill / Harness 评测工作台。重点是回答：

- 在相同任务与约束下，哪个模型、Prompt、Agent 或 Skill 组合更合适？
- 某次变更提升了什么，又造成了哪些回归？
- 一个失败来自目标行为、数据、工具、执行环境还是评分器？
- 一个质量结论是否有足够证据，能够复核并进入 CI？

继续保留 Python、FastAPI、React + Vite、现有 workspace 和单用户边界。原设计“不复用其他项目代码”应通过一份新的迁移 ADR 更新为“允许经测试的独立逻辑与测试资产迁移，不继承旧平台控制面、数据库和权限耦合”。[S8]

### 2.2 v1.0 建议范围

这里的 v1.0 是建议的产品验收范围，不是当前已有发布标签。

| 领域 | v1.0 必备 | 明确不作为本轮前置条件 |
|---|---|---|
| 模型评测 | 自定义文本集、结构化输出与工具调用评测、固定模型与参数 | 模型训练、托管推理集群 |
| LLM Benchmark | 保留 GSM8K；C-Eval 完整接入；CMMLU 在同 Runner 验证后追加 | 一次接入所有公开 Benchmark |
| Agent Benchmark | Harbor + 一个明确版本的 Terminal-Bench 2.x | 仅因 Harbor 支持某数据集就声称平台已支持 |
| 自有运行时 | Builtin Agent + 工具/沙箱/预算/评分闭环 | 多 Agent 编排和可视化 Agent 编辑器 |
| 外部运行时 | 真 Pi 接入；Claude/Codex 批处理链路及兼容矩阵 | 完整交互式终端、所有 CLI 协议形态 |
| 场景与 Skill | 多轮主流程、状态/工具/产物断言、Skill fixture 与 A/B 评测 | Skill 商店和任意不可信插件热安装 |
| 结果与质量 | 多指标报告、显式可比性、固定基线、质量门禁 | 跨任务任意合成一个“万能总分” |
| 开发者能力 | Python 客户端、CLI、pytest/CI、报告导出 | 同时维护多语言 SDK |
| 运维与历史 | 干净安装、备份恢复、历史只读导入、公开仓库发布检查 | 多租户、RBAC、组织审批、Kubernetes 必选 |

## 3. 规划原则

**以用户闭环交付，而不是以目录交付。** 每项功能必须包含配置、预检、执行、证据、评分/明确不评分、报告和失败反馈。

**保留单一运行主权。** 新模块复用 RunDispatcher、CaseAttempt、ScoringPass 和快照，不引入第二套生命周期。

**区分三种结果。** 运行完成、任务达成、证据足够是三个独立判断。进程正常退出不等于答案正确；答案正确也不代表可以与另一 Run 直接比较。

**区分验证层级。** 已实现、离线 fixture 验证、真实服务集成验证、显式 live 验证分别记录。不能用一项 CI success 代替所有层级。

**先可用，再扩宽。** 每个阶段选一条代表性链路完成，不等待所有 Provider、所有运行时和所有指标一起完善。

**有明确停止条件。** 基础设施变更只为当前功能所需；修完并通过验收后回到产品能力，不开展无边界的重构。

## 4. 目标结构与必须保留的语义

### 4.1 四个维度

| 维度 | 表达的问题 | 示例 |
|---|---|---|
| 评测对象 | 比较谁 | 模型、Prompt、Agent、Skill 组合、完整 Harness |
| 任务定义 | 做什么 | 自定义 Case、C-Eval、Terminal-Bench、多轮业务流程 |
| 执行后端 | 谁来运行 | Direct、Builtin Agent、Pi、CLI、OpenCompass、Harbor |
| 评分协议 | 如何判断 | exact、JSON Schema、文件断言、Verifier、LLM Judge |

UI 的评测工作区可以不同，但不应为每个工作区建立独立的 Run、模型配置或评分历史。

### 4.2 演进现有后端，不重建它

当前 ExecutionHandle 的主要接口是 `invoke(case_id)`。[S9] 这适合现有逐样本调用，但外部 Benchmark 经常按完整 Job 启动并产出一组样本。

建议在现有后端注册上区分 sample-based 与 job-based 两种执行形态。外部 Job 应具有持久化句柄、启动/监控/中断/收集/导入状态，而不是每一题重启一次 OpenCompass 或 Harbor。最终结果仍导入同一 CaseRun / Artifact / ScoringPass 体系。

API 始终负责提交与查询，不执行不可信任务。命令通道由 Worker/运行时实际消费，收到 ack 才显示送达。

### 4.3 实验重复与恢复重试

保留 Run、CaseRun、CaseAttempt 的现有语义。接入 Harbor 时补充 Trial 或等效的明确实验重复标识：计划中的独立实验重复，不等于传输错误重试，也不等于操作员 retry 创建的子 Run。pass@k 不能把失败的网络请求自动算成独立答题机会。

### 4.4 证据与评分

一个 Evidence/Observation 视图应能提供最终文本、结构化内容、可见工具轨迹、文件与 patch、退出原因、模型身份、计量和证据引用。Evaluator 不读取特定 Harness 的私有对象。

模型输入与 gold、隐藏断言、Verifier、Judge rubric 分离。只记录上游实际可观察到的事件，不推断或编造未暴露的推理过程。

一个 ScoringPass 固定输入证据、评分器版本、规则/rubric、参数和可选 Judge 配置。离线重评分不调用被测模型；新增 LLM Judge 时，必须另行声明其可能产生 Judge 费用，不能沿用“所有 rescore 零网络”的模糊承诺。

## 5. 总体里程碑

| 阶段 | 产品成果 | 主要依赖 | 退出条件 |
|---|---|---|---|
| M0（已有基线） | 完整性加固 | 当前主干 | 作为持续回归，不重新开发 |
| M1 | 原生 Agent 与通用评分闭环 | M0 | 同一文件任务能跑、能判、能解释 |
| M2 | C-Eval 与正式 LLM Benchmark 工作区 | M0；复用 M1 证据/评分边界 | 一次完整外部 Job 可复现、可查询、可比较 |
| M3 | Harbor / Terminal-Bench | M2 外部 Job 契约 | Task/Trial/Verifier/Artifact 全链路成立 |
| M4 | Pi 与 CLI Harness | M1；不依赖 M3 全部完成 | 真实执行、取消、日志和产物被统一接收 |
| M5 | 主流程场景与 Skill 评测 | M1；外部 Agent 可由 M4 增强 | 状态变化、权限和副作用可验证 |
| M6 | 实验、可比性、回归门禁 | 各阶段逐步建设，最终依赖 M2/M3/M5 | 有条件的结论可以进入 CI |
| M7 | SDK、历史切换与稳定发布 | 必备能力验收 | 新环境可安装、可恢复、可替代旧主线 |

默认单人执行顺序：M1 最小闭环 → M2 → M3 → M4 → M5 → M6 收口 → M7。M6 的最小比较与门禁在 M2 就开始；M7 的打包/备份/验证也随阶段推进，并非到最后才测试。

依赖上，M4/M5 不必等待 Harbor 完成；外部环境阻塞 M3 时，可转向已就绪的 M4/M5。避免同时展开所有路线，工作进行中限制建议为一个主功能加一个验证任务。

## 6. M1：原生 Agent 与通用评分闭环

### 用户成果

选择两个已发布模型，让它们驱动 Builtin Agent 完成同一个文件整理任务；系统自动检查输出文件、内容、允许的工具和禁止的副作用，并展示失败发生在哪一步。

### 工作包

| 工作包 | 内容 | 验收 |
|---|---|---|
| M1-A Observation 与 Evaluator 接线 | 复用现有 exact/contains/regex；统一多指标结果；补 JSON Schema、文件存在/内容、退出码、工具调用断言 | 同份冻结证据可产生多个带版本的指标；错误/缺证据不变成通过 |
| M1-B BuiltinAgentBackend | 注册真实执行后端；接入现有 AgentRuntime、Provider、ToolRegistry、Sandbox；明确 native tool call 与 legacy 文本动作模式 | 两种模型 fixture 都能进入真实工具循环，且执行模式进入快照 |
| M1-C 预算与终止 | 最大步数、输出/总 token、时长、工具调用数；可观察成本限制；记录停止原因 | 无限循环被终止；取消清理；未知成本不伪装成硬预算 |
| M1-D 轨迹与产物 | 每步模型、工具请求/结果、错误和文件变化进入 Evidence；产物按内容哈希引用 | 评分能指向具体文件/事件；无凭据、无 gold 泄漏 |
| M1-E 用户入口 | 同一工作区完成配置、发起、监控、结果下钻；能力不足的模型创建前拒绝 | Web/CLI/Worker/报告读取相同结果 |

### 代表性验收集（建议新增，不是已有测试统计）

第一组：正常创建指定文件并通过内容断言。第二组：工具失败后收到 observation，允许的恢复成功。第三组：试图越权写文件或无限循环，系统阻断并给出明确失败/停止证据。

离线验证先用可控 Provider 和工具 fixture，随后显式执行小规模真实模型与 Docker 链路。运行总费用由操作者授权，不由普通 CI 自动触发。

### 本阶段不做

不做复杂 Planner、多 Agent 协作、任意 MCP 自动发现，也不为了接入一个 Agent 就重写整个 API 或存储包。现有 legacy 文本动作协议作为不同版本保留，不静默替换后声称结果仍直接可比。

## 7. M2：C-Eval 与正式 LLM Benchmark

### 用户成果

在 MoTTEavl 中选择 C-Eval 数据版本、学科、Profile、模型与预算，查看预检后发起；得到完整样本输出、各维度指标、失败说明和可比性提示。

### 工作包

**M2-A 外部 Job 执行形态。** 在现有 ExecutionBackend 上实现 job-based 生命周期，持久化 Runner 标识、Job ID、工作目录、镜像/环境版本和采集检查点；API/Worker 各自职责不变。先用确定性假 Runner 验证，再装真实 Runner。

**M2-B Benchmark Catalog 与来源。** 插件注册继续承担准备/评分/聚合；Catalog 增加可执行条件、数据来源、revision、checksum、split、许可证、Profile、样本选择及镜像信息。不把“适配器已注册”显示成“一键可运行”。

**M2-C OpenCompass/C-Eval 迁移。** 迁移旧实现的参数构造、学科筛选、输出明细与原始指标解析，不迁移旧 API/数据库/Celery 控制面。旧适配器包含答案二次提取和重算，应分别保留 Runner 原始指标与平台诊断指标。[S10]

**M2-D 费用与模型证据。** 明确外部 Runner 的调用路径。能通过共享 Provider 适配钩子时尽量复用；不能时保留原生语义并标记观测缺口，不声称同等身份核验和预算能力。冻结原生重试设置，避免平台与 Runner 叠加重试。

**M2-E 产品工作区。** C-Eval 操作/样本/监控/结果/比较页面复用通用组件，只增加学科/Profile 等特有控件。

### 迁移验证与退出门

先用同一份脱敏原始 Runner 输出分别喂给旧解析器与新适配器，对齐样本身份、选择范围、原始分数和诊断分数。分数有意改变必须记录 scorer 版本与理由，不能以“迁移”为名覆盖口径变化。

集成验证覆盖成功、部分输出、无明细、空输出、畸形结果、取消、非零退出、采集后进程崩溃以及重复导入。每个已选择 Case 都有明确处置结果；总分与分母、覆盖率可核对。

建议以少量真实样本验证调用路径，然后用完整选定 Profile 验收。小样本 smoke 报告不得标为完整 Benchmark 成绩。代码与数据许可证分开记录；数据仅按其具体授权方式获取和分发，不能把代码许可证套到题目上。[S18]

CMMLU 在复用同一 Runner 后仍需自己的数据/Profile/结果测试。不要同时在此阶段进行框架大版本升级；版本升级形成单独 PR 与兼容矩阵记录。

## 8. M3：Harbor / Terminal-Bench

### 用户成果

在可管理的容器环境中运行固定版本的 Terminal-Bench 任务，比较 Agent/模型配置；逐 Task/Trial 查看执行过程、Verifier 结果、产物和费用证据。

Harbor 的任务包含指令、沙箱环境与测试，数据集可以来自本地、注册表或 Git；Agent 接口也有不同执行形态。因此应让 Harbor 保持其原生环境管理与测试语义，MoTTEavl 负责外围管理和统一证据，而不是重新实现一个 Harbor。[S14][S15]

### 工作包

| 工作包 | 具体交付 |
|---|---|
| 任务与 Trial | 固定数据集版本、任务内容/路径、Agent 配置、计划重复次数；稳定映射跨 Job 的 Task 身份 |
| 执行与回收 | Runner 生命周期、容器所有权、超时/取消、失败清理和残留扫描 |
| 结果归一化 | 区分 Agent 失败、环境失败、Verifier 失败、未尝试、结果不确定 |
| Artifact 与 Trace | 原始日志、结构化轨迹、文件/patch、Verifier 原始输出；缺失与截断明确标注 |
| 指标与比较 | Task 通过率、Trial 分布、成功任务成本、时长、有效样本覆盖；计划 Trial 与网络重试分开 |
| UI | Task 列表、Trial 切换、终端文本/轨迹、文件差异、Verifier 证据 |

旧适配器中的 trial/reward/trajectory 解析是迁移起点，而不是已经通过新平台验证的完整支持声明。[S11]

### 关键安全与真实性边界

不可信任务容器不能得到 Docker socket；可信环境管理进程的权限与任务内权限分别说明。不要在 MoTTEavl 沙箱中无条件再套一层 Harbor 环境，造成取消、文件和资源所有权混乱。

Verifier 与隐藏 gold 应与被测 Agent 的可见工作区隔离；按所选官方任务协议部署测试与环境。平台凭据只提供实际必需范围，不向任务暴露整个本机凭据目录。

### 退出门

先使用确定性的通过/失败任务和适当的校准 Agent 验证环境与 Verifier，再进行用户授权的真实 Agent 小批次。验收覆盖任务同名不同路径、多个 Trial、没有 reward、Verifier 超时、Agent 成功但评分失败、取消时工具仍执行、Runner 退出后迟到输出等场景。

Terminal-Bench 2.0、2.1 或其他具体版本分别登记；不能以“Harbor 支持”替代某个数据集/Profile 的平台验收。

## 9. M4：真实 Pi 与 CLI Harness

### 用户成果

将同一任务交给 Builtin Agent、Pi、Claude/Codex，查看可观察行为与结果差异；清楚区分底层模型可控程度、原生工具和会话配置。

### 分三层推进

**第一层：Pi 真接入。** 将已加固的 bridge 协议连接到固定版本的真实 Pi 包；模型调用路径、工具执行边界、Skill 注入和 session 生命周期都要进入配置与证据。保留原有 malformed/timeout/进程树测试，而不是替换成几个成功输出测试。

**第二层：Claude/Codex 批处理。** 启动前检测版本和执行条件；固定工作目录与配置；接收结构化事件；正确处理退出、超时、取消、文件产物与成本未知。已有 binary probe 不能直接作为执行就绪状态。

**第三层：交互式命令。** 先选择一个后端接通 RunCommand consumer，再实现消息、批准/拒绝、取消的投递与 ack。命令包含幂等 ID、目标 session、有效期与投递状态；重启后不得重复执行危险操作。操作员干预进入轨迹，并在比较中标为不同条件。

第三层可作为后续增强，不阻塞首批批处理链路；未接通的命令继续明确拒绝。

### 公平比较边界

当 Harness 自带工具、提示词或无法固定的路由时，评测对象是整个系统，不是纯模型。不能将“不同 Harness + 不同原生工具 + 不同限制”的结果包装成只比较模型能力。

### Inspect 的位置

先接入已生成日志的只读导入，复用原始日志与样本身份；有实际执行需求时再接入外部 Job。Inspect 已有评测集合、样本复用、日志和重试机制，不应在平台中完整复制同一套能力。[S16][S17]

需要显式保留失败日志，并协调它的恢复/重试策略与 MoTTEavl 的 needs_review 语义。不能因外部框架默认重试而绕过平台对未知付费请求的处理。

### 退出门

每个标记支持的后端都有一次真实小任务的执行证据，以及离线可回放的脱敏协议 fixture；取消、版本不匹配、 malformed 输出和产物采集都可验证。协议已连接但证据不完整的部分单独标为限制。

## 10. M5：主流程场景与 Skill 评测

### 用户成果

不仅检查最终回答，还能验证“询问必要信息 → 调用工具 → 修改状态 → 异常恢复 → 完成任务”的流程。Skill 可以被独立验证，也可以作为 Agent 的一个受控实验变量。

旧场景引擎已有 when/expect、checkpoint、branch、loop 等执行逻辑，是迁移语义的起点。[S12]

### 工作包

**Scenario Workflow。** 版本化 Given/When/Then、分支、循环、检查点、终止条件；Fixture 初始化/快照/清理；工具 mock/replay/real/deny；对话输入与目标 Agent 解耦。

**业务状态断言。** 请求字段、工具参数、调用顺序、数据库/文件状态、禁止副作用、最终任务达成。先迁一个完整的订单取消或文件处理场景，不先实现一个通用可视化工作流编辑器。

**Skill Manifest。** 名称/版本/内容 hash、指令与资源、依赖、fixture、适用约束和声明权限；声明权限不等于授权。纯指令 Skill 不强制有可执行入口；可执行 Skill 才要求相应输入输出与运行沙箱。

**Skill Fixture。** 输入输出、允许工具、文件副作用与资源边界测试。准备、执行、失败、清理各阶段均留证据。

**Skill A/B。** 无 Skill / Skill v1 / Skill v2 在同模型、同 Agent、同任务集与明确预算策略下成对比较。记录因 Skill 长度产生的输入成本，避免只报告成功率增益。

**LLM Judge。** 对不能确定性评分的内容增加固定 Judge profile、rubric/prompt 版本和结构化结果；维护一份人工标注的校准集；被测输出作为待评数据，不允许其修改评分规则；Judge 错误和未知不自动判通过。Judge 调用与费用单独记录。

### 退出门

一个主流程覆盖成功、确认缺失、工具错误、回退、超时、越权和副作用失败。至少一个 Skill 支持 fixture 测试与加入/移除后的配对实验。重评分追加新 pass，历史基线继续指向旧 pass。

## 11. M6：实验、可比性与回归门禁

这一阶段从 M2 开始逐步实现，最后形成跨类型的统一产品，而不是直到 M6 才第一次比较结果。

### 实验对象

建议增加轻量 ExperimentSpec，固定任务集、选样、变更维度、预算策略、重复次数和评分协议；一次矩阵展开为一组相互关联的 Run。支持模型、推理等级、Prompt、Agent、Skill 版本的显式变量，不引入多租户项目管理。

### 可比性

执行指纹记录全部配置以支持复现；比较签名只对要求保持一致的条件做约束，并包含允许变化的维度。比较两个模型时模型不同是预期变量，不能因为完整 hash 不同就一律判不可比。

报告给出 comparable / partially_comparable / not_comparable 及具体理由，例如样本集合不同、Judge 版本不同、超时政策不同、人工介入或未知模型身份。部分可比应明确可比较的子集和指标。

### 指标与缺失

保留每个 Benchmark 自己的合法分母，不把 GSM8K 的 selected_cases 和 Direct LLM 的 judged_cases 强行统一。[S5][S6]

同时展示 selected、attempted、judged/valid、not_attempted、call_failed、unknown；正式门禁加入最低覆盖率和证据要求。不能通过跳过难题或失败样本改善分数。

支持多指标、分层结果、按 Case 配对的差值与不确定性区间；多个独立 Trial 的统计与基础设施重试分离。不对统计样本很少的 smoke 结果给出强排名结论。

### 基线与门禁

Baseline 绑定 Run + ScoringPass + 比较签名；旧平台已有新失败/修复/持续失败的对比思路，可迁移为独立于旧 ORM 的服务。[S13]

规则至少覆盖绝对阈值、允许退化幅度、成本/时长约束、最低样本量、最低覆盖、证据不足与安全阻断。门禁结论区分通过、质量失败、证据不足/不可比、执行错误，CLI 使用可机器读取的结构化结果和明确非零退出语义。

最小门禁在 M2 就可以支持“固定数据集与 Profile、完整覆盖、指标阈值”；跨 Agent/Skill 统计在 M4/M5 后扩展。

### 退出门

同一受控配置生成可复核对比；换评分器后不会悄悄改变旧基线；数据不同、Judge 失败、缺失样本、未知费用和 needs_review 各有明确政策；CI 不把“运行正常结束”当作“质量通过”。

## 12. M7：SDK、旧平台切换与稳定发布

### Python / CLI / CI

基于新 API 提供类型化客户端和错误，支持提交、等待、事件恢复、取消、retry、指定 ScoringPass 报告、比较、Gate 和导出。复用已有本地执行核心，不把旧客户端的资源层级和 API 路径原样复制。

增加 pytest 集成与机器可读 JSON/JUnit 输出；CLI 的本地与远程模式使用同一参数校验和契约。构建 wheel 并在干净环境安装，确保样例/资源可用，不只验证 editable 开发模式。异步 SDK 可按真实调用需要排期，不与同步客户端同时重写。

### 历史数据迁移

配置 → Dataset/Scenario → 历史 Run/Artifact/Score，分阶段导入。只迁凭据引用，不迁真实 Secret。每条记录保留旧来源、旧 ID、schema/importer 版本、原始哈希和映射信息。

默认只读历史导入，不伪装成新平台执行出来的记录。缺失模型身份、样本、ScoringPass、价格或版本时标记未知；不能从当前配置反推历史事实。支持 dry-run、检查点、幂等重跑、逐条失败原因、导入前后数量与哈希核对以及回退。

不默认迁移正在运行的旧 Job；先完成或显式停止旧执行，再做切换。

### 发布与运维

开发、离线 CI、服务集成、显式 live 四种环境分离。固定依赖与 Runner 镜像兼容记录。验证空库安装、从上一兼容版本升级、备份恢复、故障重启、工件一致性及 Windows/WSL2 能力边界。

真实 Compose build/up 与恢复演练纳入受控验证；它们不能由 compose config 的成功代替。备份应覆盖数据库和工件一致性，恢复后再核对引用，而不只验证命令退出码。

公开仓库发布前进行凭据、私有业务数据、原始 Provider 响应、Fixture 和数据许可证检查。默认本地绑定；需要远程访问时增加单用户认证与安全传输，而不是开放未认证执行接口。

### 旧平台停止维护的门

C-Eval 模型比较、Harbor Agent Benchmark、多轮主流程/Skill 回归三类替代场景均通过；历史数据和未迁移功能有明确清单；新的功能开发只进入 MoTTEavl。未迁移部分保留为只读参考，不再长期双写、双调度或双数据库同步。

## 13. 横向工作流：每个里程碑都必须包含

| 横向流 | 持续交付内容 | 防止的退化 |
|---|---|---|
| UI 与设计系统 | 复用现有 DESIGN.md/token；公共模型选择、参数、预检、状态、证据、评分历史组件；页面按 suite 扩展 | 每迁一个 Benchmark 就复制一套样式和 Run 状态 |
| Provider | 流式事件、原生工具历史、结构化输出能力、探针证据、身份和计量；每项单独验证 | 兼容入口把不支持字段静默删除；UI 进度流被当成模型 token 流 |
| Benchmark 治理 | 注册能力、数据/代码许可证、revision/checksum、Profile、样本 ID、Runner/scorer 版本 | 名称相同但口径不同却被直接比较 |
| 证据与安全 | 输入/gold/Verifier 隔离；脱敏；输出大小和工件配额；保留/删除政策；不确定调用保守恢复 | 凭据落日志、评分数据泄漏、无限工件、未知费用变零 |
| 工程质量 | 当前完整性回归、Contract/生成类型、服务集成、构建安装、备份恢复 | 新功能绕过已有 Run/CAS/ScoringPass 规则 |

Web 已明确以 `apps/web/DESIGN.md` 为唯一设计依据，并要求设计决策与 CSS token 同步变更；本路线保持现有设计语言，不另起 UI 重设计项目。[S19]

建议每个新工作区都具有配置/预检、监控、样本详情、结果、比较入口；没有真实能力的按钮显示具体原因。实时状态、后台数据与重试子 Run 由后端事实驱动，不由前端猜测。

## 14. 迁移资产清单

| 旧平台资产 | 新平台处理方式 | 阶段 |
|---|---|---|
| 执行计划/不可变快照/模型一致性 | 新主线已承接基础机制；不再复制旧实现 | 已有，持续回归 |
| Provider→Account→Deployment→Alias 全层级 | 保留新 Connection/Profile/PriceTable；只迁有用配置与证据 | 不整体迁移 |
| OpenCompass/C-Eval/CMMLU | 独立配置、Parser、Profile 与 fixture 迁移 | M2 |
| Harbor / Terminal-Bench | Task/Trial/Verifier/Artifact 语义迁移 | M3 |
| lm-eval | 保留迁移清单，具体任务触发；按生成/loglikelihood 能力分别准入 | v1 后 |
| Inspect | 原始日志只读导入先行，执行适配按需求 | M4 扩展 / v1 后 |
| Scenario Runner | 迁 DSL 与断言语义，重接新 Runtime 和证据 | M5 |
| Evaluator / Judge | 复用可独立测试的评分逻辑，重接 Observation/ScoringPass | M1/M5/M6 |
| Baseline / Gate | 保留质量语义，去掉旧 ORM/项目权限耦合 | M6 |
| Transcript / Trace / Artifact 组件 | 接入新视图模型及设计系统 | 随 M1/M3/M4 |
| SDK / pytest / 导出 | 面向新 API 改造，不移植旧路由假设 | M7 |
| 历史 Run 与 Score | 来源可追溯的只读导入，未知信息保持未知 | M7 |
| 多租户/RBAC/组织审批/复杂豁免 | 与单用户边界冲突或收益低 | 不迁移 |
| Embedding/Rerank/RAG 与多模态 | 独立能力路线，不阻塞核心迁移 | v1 后 |

每次迁移都应记录来源提交、复用文件、语义差异、删去的耦合、对应测试和当前验证层级。不以复制文件数统计迁移完成度。

## 15. 下一批可落地任务

以下是拟议任务，不代表已经在仓库中创建 Issue 或文件。现有路径为核对过的模块，新模块路径是建议。

| ID | 交付 | 主要触点 | 关键测试 | 依赖 |
|---|---|---|---|---|
| R1 | 通用 Observation/Evaluator 接线；保留现有三评分器行为 | `packages/contracts/`、`packages/evaluators/`、现有 `motte_sdk` 评分入口 | 多指标、无期望、缺证据、评分失败、追加历史、gold 不进入模型输入 | 当前基线 |
| R2 | BuiltinAgentBackend 真实注册与 Provider/工具接线 | `packages/sdk-python/motte_sdk/execution_backends.py`、`packages/agent-runtime/` | 正常工具循环、工具错误回灌、协议不支持、预算终止 | R1 的证据边界 |
| R3 | Agent 文件任务与 Artifact 采集纵向链路 | `packages/sandbox/`、`packages/trace/`、现有存储/运行入口 | 越权文件、取消清理、丢失产物、输出截断、任务未达成 | R2 |
| R4 | Agent 操作/监控/结果下钻与能力显示 | `apps/web/src/evalTypes/`、共享组件、现有 API 契约 | 不可执行按钮、真实 Run 状态、历史 ScoringPass、错误可定位 | R2/R3 |
| R5 | 外部 Job 执行句柄与假 Runner 集成 | 现有 `execution_backends.py` / `dispatcher.py`；拟新增 `packages/benchmark-runtime/` | 一 Job 多 Case、异常退出、取消、重复采集、恢复检查点 | M1 接口稳定；无需全部 UI 完成 |
| R6 | C-Eval 资源/Profile 与旧新 Parser 对照 | 拟新增外部 Benchmark 模块、现有插件注册 | 样本身份、split、原始/诊断分数、空明细、数据版本差异 | R5 |
| R7 | C-Eval API/CLI/Web 纵向接入 | 已有 API/CLI/Web suite 扩展点 | 非付费集成闭环、能力预检、报告分母与覆盖 | R6 |
| R8 | C-Eval 显式小规模 live 验收与基线 | 受控运行环境、验证记录与回放 fixture | 模型/参数对账、真实 Runner 工件、费用和版本记录 | R7；操作者授权 |
| R9 | Harbor 原始结果导入与 Trial 契约 | 拟新增 Harbor adapter、contracts、artifact 视图 | 同名不同路径、多 Trial、无 reward、Verifier 错误 | R5 |
| R10 | 首条实验比较签名与最低覆盖门禁 | 现有报告/ScoringPass；拟新增 experiment/comparison 服务 | 同集可比、异集阻断、评分版本变化、缺失样本不误通过 | R1/R7 |

当前只建议同时启动 R1 与其测试准备，随后 R2→R3→R4。R5 的契约可以在 M1 边界稳定后进入下一批，不要求提前实现 Pi、所有 CLI 或完整统计系统。

## 16. 质量验收矩阵

| 测试层 | 范围 | 网络/费用 | 必须回答的问题 |
|---|---|---|---|
| Contract / 单元 | 注册、快照、评分、身份、参数、映射 | 默认零网络零费用 | 形状和语义是否稳定？ |
| Golden / Replay | 新旧 Parser 同份输入；跨入口同份证据 | 零网络零费用 | 迁移有没有悄悄改变样本或评分？ |
| 集成 | API→存储→Worker→假 HTTP/假 CLI；PG；真实 Docker 确定性任务 | 可用本地服务，不用付费模型 | 接线、事务、取消与清理是否成立？ |
| Live | 固定小样本、真实 Provider/Agent/Harness | 显式授权与限额 | 外部实际协议和证据是否成立？ |
| 发布 | 干净安装、升级、备份/恢复、旧记录兼容 | 环境受控 | 新用户能否复现并保留历史？ |

每个新后端复用同一组失败路径：配置不支持、缺凭据、模型错误、工具失败、超时、取消、执行中断、证据缺失、结果畸形、重复采集、评分失败、费用未知。各后端可声明不适用，但不能简单省略。

样本任务“完成”必须同时满足代码、Contract、测试、用户入口、运维说明和能力矩阵更新。对外支持级别则必须进一步指出真实外部验证范围。

## 17. v1.0 发布退出条件

| 条件 | 要求 |
|---|---|
| 模型闭环 | 自定义 Direct LLM、GSM8K、C-Eval 在固定配置下可运行并提供样本级报告 |
| Agent 闭环 | Builtin Agent 与真实 Pi；首批 CLI 批处理按矩阵验收，不混入未完成交互能力 |
| Benchmark 闭环 | Harbor + 明确版本 Terminal-Bench 的 Task/Trial/Verifier/Artifact 全链路 |
| 业务闭环 | 至少一个多轮场景和一个 Skill fixture/A-B 实验 |
| 质量闭环 | 固定 ScoringPass 的基线、可比性理由、覆盖门禁与机器可读结果 |
| 运维闭环 | 支持环境下干净安装、升级、恢复与历史核对 |
| 迁移闭环 | 所有旧功能已归入已替代/已迁移/明确后置/不迁移；无长期双写 |

若某个外部 CLI 因协议/环境尚不能达到支持级别，应明确将其标为实验性并调整发布范围，而不是将整个平台声称为所有后端均已稳定。发布范围变更必须记录，不静默漏掉原计划。

## 18. v1 后的扩展池

| 方向 | 开始条件 | 最小有效交付 |
|---|---|---|
| RAG / Embedding / Rerank | 文本/Agent 核心闭环稳定，已有真实检索项目需要回归 | 文档/索引快照、检索/重排/生成分段指标与引用证据 |
| 多模态 | 明确任务、可用数据与支持端点 | 先图像/文档，再音视频；资产 hash、模型能力和评分契约齐备 |
| lm-eval / 更多 Inspect 任务 | 能指出现有三个入口无法覆盖的具体任务 | 单任务/固定 Runner Profile 完整验收，而不是批量登记名称 |
| 浏览器/桌面 Agent | 工具与沙箱边界已经稳定 | 可回放状态、截图/动作证据、任务级 Verifier |
| 线上 Trace 转回归 | 有真实应用 Trace 与脱敏授权 | Trace 导入、样本筛选、gold 审核与失败集生成 |
| 性能与并行执行 | 真实负载已证明单执行器成为瓶颈 | 先样本级受限并行；再评估多 Worker 所有权/租约与资源配额 |
| 新任务域（例如 Blender） | 有具体任务、工具环境和可验证产物 | 小型任务包 + 固定运行环境 + 可重复的产物校验，不作为当前核心依赖 |

## 19. 持续维护方式

建议一个总路线文件负责目标/依赖/退出条件，各阶段单独写设计和实施计划；每个阶段只细化当前可执行的一批任务，不提前为半年后的能力写脆弱的精确函数名。

建议文档位置：`docs/ROADMAP.md`、`docs/migration/legacy-capability-map.md`、`docs/protocols/execution-backends.md`，以及现有 `docs/superpowers/specs/` 与 `docs/superpowers/plans/` 下的阶段设计/实施计划。以上是拟议仓库落位，本次没有写入仓库。

功能状态表至少记录能力、源/目标版本、依赖、协议是否可用、执行是否可用、fixture/integration/live 验证状态、限制及证据路径。以此更新 UI 与发布说明，避免三个地方维护相互矛盾的“完成”。

每个阶段完成后检查：新增了什么可用场景？替代了旧平台哪项能力？是否制造新的独立生命周期？是否增加必须长期维护的上游适配面？下一批工作是否由真实缺口驱动？

**最近的开工顺序：通用 Observation/Evaluator 接线 → Builtin Agent 文件任务闭环 → 外部 Job 与 C-Eval 迁移。完整性内核由回归保护，不再开展另一轮泛化基础建设。**

## 参考资料

所有仓库事实以本节固定提交为基线；后续版本发生变化时需重新核对。外部文档仅用于确认框架职责和设计边界，实施时仍须锁定所选版本。

- [S1] [MoTTEavl PR #5](https://github.com/BaiZhi967/MoTTEavl/pull/5)
- [S2] [基线 CI](https://github.com/BaiZhi967/MoTTEavl/actions/runs/35371435164)
- [S3] [平台完整性升级说明](https://github.com/BaiZhi967/MoTTEavl/blob/a668d13ee5ea0c3613648f8992ecaa4a148d3855/docs/operations/platform-integrity.md)
- [S4] [RunDispatcher](https://github.com/BaiZhi967/MoTTEavl/blob/a668d13ee5ea0c3613648f8992ecaa4a148d3855/packages/sdk-python/motte_sdk/dispatcher.py)
- [S5] [Benchmark 插件注册](https://github.com/BaiZhi967/MoTTEavl/blob/a668d13ee5ea0c3613648f8992ecaa4a148d3855/packages/sdk-python/motte_sdk/benchmark_plugins.py)
- [S6] [Direct LLM 操作指南](https://github.com/BaiZhi967/MoTTEavl/blob/a668d13ee5ea0c3613648f8992ecaa4a148d3855/docs/operations/direct-llm.md)
- [S7] [兼容与能力矩阵](https://github.com/BaiZhi967/MoTTEavl/blob/a668d13ee5ea0c3613648f8992ecaa4a148d3855/docs/protocols/provider-compatibility.md)
- [S8] [MoTTEavl 总体设计](https://github.com/BaiZhi967/MoTTEavl/blob/a668d13ee5ea0c3613648f8992ecaa4a148d3855/docs/superpowers/specs/2026-09-14-llm-agent-harness-evaluation-platform-design.md)
- [S9] [ExecutionBackend 当前契约](https://github.com/BaiZhi967/MoTTEavl/blob/a668d13ee5ea0c3613648f8992ecaa4a148d3855/packages/sdk-python/motte_sdk/execution_backends.py)
- [S10] [旧 OpenCompass 适配器](https://github.com/BaiZhi967/llm_agent__evaluation_platform/blob/b661bcdf83e1c3dfb8d6062ee78817d249e86a4c/packages/benchmark-adapters/src/evalstudio_benchmark_adapters/opencompass.py)
- [S11] [旧 Harbor 适配器](https://github.com/BaiZhi967/llm_agent__evaluation_platform/blob/b661bcdf83e1c3dfb8d6062ee78817d249e86a4c/packages/benchmark-adapters/src/evalstudio_benchmark_adapters/harbor.py)
- [S12] [旧 Scenario 引擎](https://github.com/BaiZhi967/llm_agent__evaluation_platform/blob/b661bcdf83e1c3dfb8d6062ee78817d249e86a4c/workers/scenario-runner/src/scenario_runner/engine.py)
- [S13] [旧 Gate 服务](https://github.com/BaiZhi967/llm_agent__evaluation_platform/blob/b661bcdf83e1c3dfb8d6062ee78817d249e86a4c/apps/api/app/services/gate_service.py)
- [S14] [Harbor Datasets](https://www.harborframework.com/docs/datasets)
- [S15] [Harbor Agents](https://www.harborframework.com/docs/agents)
- [S16] [Inspect Log Files](https://inspect.aisi.org.uk/eval-logs.html)
- [S17] [Inspect Eval Sets](https://inspect.aisi.org.uk/eval-sets.html)
- [S18] [C-Eval 官方仓库与许可证](https://github.com/hkust-nlp/ceval)
- [S19] [MoTTEavl Web DESIGN.md](https://github.com/BaiZhi967/MoTTEavl/blob/a668d13ee5ea0c3613648f8992ecaa4a148d3855/apps/web/DESIGN.md)
