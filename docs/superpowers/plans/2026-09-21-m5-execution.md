# M5 执行计划：Workflow、Skill 与独立 Judge

> 状态：planned；本文件不是 M5 实现或验收记录。开发 Agent 按工作包使用 `superpowers:subagent-driven-development`（有子 Agent）或 `superpowers:executing-plans`（单 Agent）。每包先写行为反例，再实现、复审和独立提交。不得把此计划视为真实模型费用授权。

**目标：** 完成交互业务流程的过程与状态评测、三种 Skill 的版本化验证与三组对照，以及可追溯、经校准的独立 Judge，覆盖 M5-G01–G22、T01–T12、A01–A18。

**需求优先级：** 用户本次要求 > 项目 AGENTS.md / DESIGN.md > [M5 需求全文](../../roadmap/M5-scenarios-skills-and-judges.md)及[共通约束](../../roadmap/README.md) > 本执行细化。[2026-09-19 计划](2026-09-19-m1-m7/M5.md)保留为历史规划，当前实施顺序和接口事实以本文件为准。

**代码基线：** M4 交付主干提交在交接时登记；编写期间 M4 整改尚在验证，禁止把计划中的拟新增接口当作现有能力。开工必须记录 `git rev-parse HEAD`、工作区状态、Alembic 实际 head 和 M4 验证记录中的未验收环境。

**技术栈：** Python 3.12、uv workspace、FastAPI/Pydantic、Node 24/pnpm、React/Vite、SQLite/PostgreSQL；既有 RunDispatcher、CaseAttempt、InvocationRecord、Artifact、FrozenObservation、ScoreSet、ScoringPass 和 Worker 执行锁。

## 1. 已核实的起点与必须先解决的接口

| 当前实际模块 / 接口 | 当前边界 | M5 的修改方式 |
|---|---|---|
| `motte_contracts/scenario.py:ScenarioSpec` | 有 dataset/model/agent/skills/harness；无发布 Workflow 语义 | 增量增加 Workflow 引用和 TargetRequirements；不造第二份 Scenario 身份 |
| `motte_sdk/resolve.py:prepare_run` | 在创建期展开版本资源、配置校验和 backend 解析 | 复用一次性冻结；Workflow/Fixture/Skill/Judge 快照为保留字段，客户端不能伪造 |
| `ExecutionBackendSpec`、`ExecutionHandle.invoke(case_id)` | 统一 sample/job 装配、attach 和能力声明 | 新增一个 scenario sample backend；步骤不成为子 Run 或伪 Trial |
| `RunDispatcher`、`RunService.execute` | 统一执行权、CaseAttempt 和终态归档 | 场景异常与不确定副作用复用原恢复路径 |
| `BuiltinReActRuntime.run_agent` | 每次调用重置上下文、步数和 call-id 集合 | 抽出持久 case-owned session；保留原 one-shot wrapper 和原算法 |
| `AgentCaseExecutor`、`agent_tasks_scores` | 固定 agent-tasks 快照与工具集 | 抽取共享日志/权限/证据钩子；不伪装成 agent-tasks 来绕过校验 |
| `motte_sandbox/ToolRegistry` | 当前主要是 handler / deny 分派 | 一次补齐 real/mock/replay/deny 的授权编译和执行语义，所有入口复用 |
| M4 RuntimeDefinition / Profile / session | 描述配置、进程、可观察能力及人工命令 | `interactive` 不等于 TargetSession 多轮 send；只能启用实际实现的适配器 |
| `FrozenObservation` / `EvidenceRef` | schema 受控；event/artifact/invocation 三类引用 | checkpoint/state 用带 schema 的冻结证据扩展，必须有 hash 与归属验证 |
| `evaluate_observation` / `MetricResult` | 多指标、限时、artifact_reader 与标准缺失状态 | 新指标走现有注册/投影；无独立私有 ScoreSet |
| `RunService._append_scoring_pass` | 当前追加即推进 current，身份主要来自原 Run | 支持预分配 pass ID、独立 scorer provenance 和作业原子完成；待处理结果不放进 terminal pass 表 |
| `scoring_passes.current` | 可能回退为最后一条 pass | 不允许 pending/failed 草稿被此回退逻辑选中 |
| Invocation 仓库三实现 | prepared/dispatching/settled、CAS、不确定调用 | 扩展 purpose=subject/judge、job/pass 归属；Judge 不确定性不得隔离原 subject Run |
| SkillManifest / SkillRegistry | 目前 entrypoint 必填、仅按名称内存注册 | 契约分 kind；资源仓库持有版本，registry 只做加载缓存 |
| `motte_eval/judge.py:JudgeMetadata` | 只有元信息，无实际评分作业 | 复用 Provider 完整装配 Judge，另记用途与预算 |
| ComparisonService / ComparisonPolicy | 已有固定 pass 报告与比较、允许因子列表 | 在唯一实现中加入 Skill/Workflow/Judge/rubric/calibration/budget；消费 M4 干预证据 |
| WorkerLoop / execution lock | 已有恢复与唯一执行权 | ScoringJob 是同一 Worker 的工作项，不建第二个调度系统 |

源码路径均相对仓库根。具体签名由实现者先检查再修改；若 M4 最终合入已有同职责代码，复用并更新路径，禁止照计划新建同义模块。

## 2. 冻结的工程决策与边界

1. **场景身份分层。** ScenarioVersion 引用 WorkflowVersion；Run 的 execution backend 是 scenario；内部 target runtime/profile 独立存放。不得把外层 `scenario@1` 与内层 `pi-agent@1` 混作同一 backend，也不得整体放松 M4 的身份校验。
2. **持久会话。** TargetSession 的 begin/send/observe/interrupt/close 一次 Case 一个实例。模型历史、已执行 call IDs、累计预算在 send 之间保留；换 Case、retry 子 Run、Skill 对照臂均重置。
3. **预算先于动作。** 每步按同一个 monotonic deadline 取剩余时间；全局 step/turn/tool 上限不能因进入 loop 重领。已报告 token/cost 与可执行硬限制分开，未知费用不能声称可证明货币上限。
4. **实际停止。** 对可能阻塞且产生副作用的同步 Target 使用受控进程。线程超时返回不代表后台停止；已发远端请求仍可能计费，按 indeterminate 留证。冻结最终状态前确认受控工具/进程停止；无法确认则保留资源、needs_review。
5. **Fixture 真值隔离。** checker/gold 在平台私有存储；目标只见业务工具结果。请求输入与错误信息也不得带出隐藏断言。首批只做合成 JSON、文件、专属临时 SQLite，不触及真实支付/邮件/生产业务。
6. **权限编译一次。** 平台 ∩ Scenario ∩ Target 能力 ∩ Skill 请求；deny 优先。Skill 不能把 mock/replay 提升为 real，不能扩展路径或网络。请求政策和最终生效政策都冻结。
7. **评分只读执行证据。** 确定性指标只读取 Observation 及归属核验过的 Artifact；不访问可变工作区、不调用 Target 或业务工具。缺完整副作用监测时，“没看到”只能 insufficient。
8. **Judge 是独立作业。** API 仅校验并持久提交，Worker 执行已授权 Judge。普通 rescore 默认离线；GET、历史切换、compare 和 gate 永远零模型调用。
9. **历史不可改写。** 发布资源、冻结 Observation、ScoreSet 和旧 pass 不可覆盖。人工修订、新 rubric、新 Judge 配置只追加；固定 Baseline/RunReportRef 不追随 current。
10. **不确定不重放。** checkpoint 是审计点，不是自动恢复授权。模型/业务/Judge 已 dispatch 但结果未落库时不自动重新调用；显式重试产生新身份并说明费用/副作用风险。

## 3. 实施顺序、提交和评审

```text
T01 契约/兼容转换 → T02 Fixture → T03 多轮与中断 → T04 断言 → T05 公共 Run 闭环
T06 Skill 版本 → T07 实际注入与验证 → T08 三组对照
T09 Judge 契约/作业 → T10 校准/人工修订
T11 用户链路 → T12 完整验收
```

T09 可以在场景链路稳定后独立推进；不同时派多个实现 Agent 修改共享存储/契约。一个主实现包配一个只读验证包。先列文件所有权和共同接口，再派新上下文 implementer；完成后分别做规格与质量审查，全部阻断问题修复后才进入下一包。

每包固定交付：反例与真实失败断言 → 最小实现 → focused + 相邻回归 → 文档/OpenAPI/TS 同步 → 独立复审 → 小提交。缺文件、ImportError、skip、只创建表或页面骨架不算行为验证。验证记录写明 commit、命令、平台、依赖版本、结果、失败原因和 evidence level。不要把实现中间快照的测试结果当最终提交证据。

## 4. 逐工作包执行卡

### M5-T01：版本化 Workflow 与安全兼容转换

**关联：G01/G02/G03；验收 A05。依赖：M1 契约。**

文件：修改 `packages/contracts/motte_contracts/scenario.py`、`motte_storage/resource_store.py`；拟新增 `motte_contracts/workflow.py`、纯资源解析器、`motte_scenario/conditions.py`、`motte_scenario/conversion.py`。第一个有实际实现和测试的条件/转换组件开工时创建场景包并登记 uv 依赖，不先搭空包。

1. 定义严格 frozen/extra-forbid 的 WorkflowVersion、Step union、Fixture 引用和 TargetRequirements。七类步骤必须齐备：send_message、invoke_fixture_tool、assert、checkpoint、branch、loop、trigger_fixture_event。所有嵌套 step_id 全局唯一。
2. `when` 保留动作语义，`expect` 是紧随动作的断言；不得误译为 if。branch/loop 的执行子树有有界深度/节点数，loop 必须 max_iterations，Workflow 必须正有限 wall time、max_total_steps、max_turns。
3. 条件只支持许可数据路径及 eq/ne/in/exists/and/or/not；类型、路径、深度、节点数在编译时校验，运行时缺字段报明确错误，不能默认为 false。禁止 eval/import/属性调用/shell。
4. ResourceStore 增加发布 WorkflowVersion；本包用无副作用的纯解析器展开已发布依赖、固定内容 hash 和版本，拒绝 draft/缺失/越权。完整 prepare_run 装配推迟到 T05；在可执行 backend 注册前，公开执行必须明确 unavailable，不能改选其他 backend。
5. 旧源码已定位至 `BaiZhi967/llm_agent__evaluation_platform@b661bcdf83e1c3dfb8d6062ee78817d249e86a4c` 的 `workers/scenario-runner/src/scenario_runner/engine.py`。它实际使用 `eval(expression)`、shell=True setup/cleanup、返回后检查 send 超时；这些实现不可搬运。转换输出 source commit/hash、转换器版本、字段映射、错误和语义变化；只读解析旧结构，不导入执行它。
6. 旧 given.fixtures/initial_state → Fixture；conversation_history → 固定输入历史；when.invoke_tool → invoke_fixture_tool；expect/final_assertions → 冻结断言；branch/loop 保留顺序。setup/cleanup shell 命令、不可表达 checker、模型用户模拟器默认明确拒绝。转换不完整时不得发布“近似可运行”版本。许可证核验后才复制代码/资源。

测试：`tests/contract/test_workflow_spec.py`、`tests/scenario/test_workflow_conversion.py`。重复嵌套 ID、无界 loop、NaN/bool/超大预算、缺引用、恶意条件与深度炸弹全部具名拒绝；旧 DSL 不支持字段必须出现在诊断中，运行计数为 0。

完成门：有效最小 Workflow 可以发布并通过纯解析器确定性编译为固定快照，零付费动作；不是 create-ready 声明。上述反例失败；协议文档 `docs/protocols/scenario-workflow.md` 有完整有效样例及转换矩阵。

### M5-T02：Fixture 所有权、状态与清理

**关联：G04/G09；验收 A03/A08。依赖：T01。**

文件：拟新增 `packages/scenario-runtime/motte_scenario/{fixtures,state}.py`；复用 `motte_sandbox/workspace.py` 和 ArtifactStore，不重复实现安全路径规则。

1. FixtureSpec 固定版本、初始数据 hash、资源种类、允许工具、隔离与清理政策。FixtureInstance 绑定 run/case/attempt/owner token，保存初始化进度与受控资源清单。
2. 实现 JSON 状态、受控文件目录、独立临时 SQLite；多个 Case 即使业务 ID 相同也不共享 DB/文件/session。prepare 部分失败仍进入清理。
3. snapshot 用稳定序列化生成内容 hash；reset 必须验证 owner+受控根，不接受任意 DSN、路径或别的 Case 的 token。
4. PostgreSQL Fixture 只接受显式测试实例及独立 namespace；首批没有此条件时保持不可用。不可把生产连接“临时改名”后重置。
5. checker 真值、gold、隐藏断言单独保存；目标可访问的 Artifact/workspace 不含这些文件。工具返回最小业务可见字段。
6. cleanup 区分成功/残留/未知，记录原异常与清理异常。只删除校验过的自身资源，不清空公共工作目录；中断后资源未知时保留供复核。

测试：`tests/scenario/test_fixture_lifecycle.py`。中途 prepare 抛错、清理再失败、错误 owner、同 ID 两 Case、symlink/path traversal、非测试 SQLite/PG reset。通过条件包含“受控资源已停且清理证据落盘”，不只有函数返回。

### M5-T03：持久多轮 Target 与有界引擎

**关联：G02/G05/G06；验收 A04/A06。依赖：T01/T02。拆两提交包。**

文件：修改 `packages/agent-runtime/motte_agent/builtin_react.py`、`motte_sdk/agent_backend.py`；拟新增 `motte_scenario/{targets,engine}.py`；复用 M4 supervisor，不复制进程终止实现。

1. 先实现 T03a：从现有 Builtin loop 提取 case-owned 状态容器，保存 messages、tool call IDs、budget 和 event seq；run_agent 保留兼容 one-shot 调用。begin 只能一次，close 幂等。一次 send 可包含多次模型/工具迭代；正常 final assistant response 只结束当前 turn，session 回到可 send 状态。只有 Workflow 完成、显式 close、取消、不可恢复错误或 Case 预算耗尽才结束 session；结束后 send 拒绝。one-shot wrapper 保留既有 final_answer 终结行为。
2. 为 Target adapter 声明实际能力：multi_turn、tool modes、interrupt、Skill injection、evidence。create preflight 检查 target_requirements；batch CLI 不能假装支持多轮。M4 app-server 的人工 steer 也不能直接当作任意下一轮 send。
3. T03b 实现七种步骤，checkpoint/branch/loop 按同一 step seq 和全局计数执行；子层不能重置预算。step_start/step_end 包含 run/case/attempt/step/session 身份。
4. 每次 dispatch 前核验剩余 step/turn/tool/time；给 Target 和工具实际剩余截止时间。阻塞模拟目标必须被真实中断；拒绝以线程 join 超时替代终止。
5. stop_case 停后续业务；continue_for_evidence 只允许运行只读检查和 cleanup。失败后不得再次取消订单、修改文件或调用任意 Target。
6. 错误与取消有稳定原因，保留暂态工具错误和成功恢复的每次尝试；累计次数仍受预算约束。不会通过循环悄悄重复未知副作用。

测试：`tests/scenario/test_workflow_engine.py`、原 `tests/runtime/test_agent_tool_history.py`、`test_agent_interruption.py`。第一轮正常 final_answer 询问确认后仍可 send，第二轮读取确认并只执行一次动作；close/Case 完成后拒绝 send。覆盖支持的 native-tools/legacy Agent 模式。另测不同 Case 看不到历史、同 call ID 不重复动作、无限 loop、阻塞子进程持续写入、取消与响应同时到达。

### M5-T04：过程、业务状态和副作用断言

**关联：G08/G09；验收 A02。依赖：T02/T03。**

文件：扩展 `packages/evaluators/motte_eval/{observation,tools,trajectory,files}.py`；拟新增 `motte_eval/workflow.py`；扩展 `motte_contracts/evaluation.py` 的版本化 Workflow 证据边界。

1. 复用 tool-arguments/tool-order/file 现有指标；新增 state-equals、state-delta、no-side-effect、goal-achieved、确定性 response-policy。
2. checkpoint 固定 snapshot Artifact+hash+schema+owner+step；reader 核验引用属于当前 Observation，拒绝跨 Case/跨 Run 或损坏 hash。评分不得读取活动 DB。
3. 区分“已确认一次取消”“先收到确认后取消”“最终状态正确”“非目标字段未改”，分别保留指标；goal conjunction 不得让成功终态覆盖过程失败。
4. 阴性结论需要声明监控范围及完整度；完整工具清单并不自动证明 shell/MCP 无隐藏副作用。未知保持 insufficient，不用 0 或 pass。
5. checker 无法执行、超时或 schema 错误返回 evaluator_error；业务不满足为 scored/passed=false；不适用独立保存。
6. 通过现有 MetricResult→Score 的复合身份投影进 ScoreSet；保持多指标分母与历史 pass 兼容。

测试：`tests/evaluators/test_workflow_assertions.py`。未经确认但最后 cancelled、重复取消两次、先改后恢复、缺 snapshot/action log、外来证据、断言超时；对冻结输入重评分零模型/工具调用。

### M5-T05：Scenario 公共 Run 纵向闭环

**关联：G01/G07/G20/G22；验收 A07。依赖：T01–T04。**

装配责任：在 `packages/contracts/motte_contracts/run.py:ResolvedManifest` 登记 Workflow/Fixture/Target 保留快照字段，再把 T01 纯解析器接进 `motte_sdk/resolve.py:prepare_run`。公开 create 必须验证服务端快照归属、版本和真实 Target 能力；客户端伪造字段拒绝，不能回落到 replay/direct backend。

文件：`motte_sdk/{resolve,execution_backends,service,agent_backend}.py`、WorkerLoop；拟新增 `motte_sdk/scenario_backend.py`；契约、资源仓库与各存储初始化/迁移。

1. 发布 Workflow、Fixture、Dataset、Scenario 后，从现有 API/CLI prepare_run 生成固定快照；拒绝 client-reserved snapshots、mutable refs、重复模型路径和不满足能力的 Target。
2. 一个 CaseAttempt 装配一个 FixtureInstance+TargetSession+engine；共用 service attach 的事件、调用日志、取消和 Artifact。流程步骤不新建独立调度器。
3. 抽出共享 invocation/evidence 钩子供 Agent 与 Scenario 使用，避免复制 AgentCaseExecutor；每次模型/工具动作保留 prepared/dispatching/settled 和所属步骤。
4. 结束时先停止受控动作，再冻结状态/轨迹/输出，随后清理 Fixture；将 Observation 送 T04 注册评分路径，不能落回字符串相等 legacy scorer。
5. 副作用后 crash、结果前 crash、保存后通知失败均覆盖。重启没有新的业务动作；不确定项 needs_review，retry 是显式子 Run/新 session。
6. 完成公开链路：创建→Worker→events→Case evidence→ScoreSet/pass→report→offline rescore→显式 retry；禁用 backend 后历史仍读得出。

测试：`tests/integration/test_scenario_run_backend.py`；SQLite 两实例 API/Worker、内存对等、PG 有条件实际执行；原 agent/backend/recovery/invocation 契约回归。此包结束必须有可用的最小业务场景，不等待所有管理页面完成。

### M5-T06：Skill 三种形态、不可变版本和安全导入

**关联：G10/G11；验收 A10/A11。依赖：T01 资源发布规则。**

文件：修改 `packages/skill-runtime/motte_skill/{manifest,registry}.py`、contracts/resource_store；新增 SkillVersion 契约与受控资源打包；按实际 migration head 追加，不先建空表。

1. 定义 instruction / instruction_with_resources / executable。仅 executable 要求 entrypoint；固定 argv/interpreter/cwd/env allowlist/schema/SandboxPolicy，不接收任意 shell 字符串。
2. manifest 固定指令资源、资源清单与逐文件 hash、依赖精确版本、请求权限、fixture_refs、注入模式、lifecycle。多 Skill 次序也是身份的一部分。
3. 导入只读取校验，不执行安装 hook、不下载依赖、不跑入口。路径穿越、符号链接、过量文件/解压尺寸、压缩炸弹、未声明执行文件和未固定依赖拒绝。
4. 资源字节写入受控内容寻址 Artifact；发布再次核验字节和依赖，draft 修改不能改变已发布 hash。deprecated 停止新选择、保留历史读取。
5. registry 改为版本化查找/缓存，持久仓库是唯一版本权威；同版同内容幂等，同版异内容冲突。
6. 来源与许可证进入导入报告；不能复制旧主机凭据、node_modules、虚拟环境或未知二进制作为 Skill 资源。

测试：`tests/skill/test_skill_versions.py` + 三存储资源契约。无 entrypoint 指令可发布，执行 Skill 未声明入口不可发布；历史 Run 在新草稿编辑后读出的资源 hash 不变。

### M5-T07：实际注入、权限交集与三层验证

**关联：G07/G11/G12；验收 A09。依赖：T03/T06。**

文件：拟新增 `motte_skill/{injection,fixtures}.py`；扩展 agent adapter、ToolRegistry 和实际有能力的 Runtime adapter；不用“已选 Skill”字段代替执行。

1. 编译有序注入计划：Skill版本、渲染输入/输出hash、冲突政策、权限交集、资源实际落位、适配方式和 native loader 版本。
2. Builtin 优先受控 system/context 段；固定每次模型请求最终有效注入引用。外部原生加载必须记录可验证清单；不可观察部分标 partial，不能声称完全生效。
3. 执行权限网关真正落实 deny/mode/path/network；fixture 工具不能因为 Skill 申请而升级成真实业务工具。
4. 静态/渲染验证、executable fixture、固定 Target 行为测试分别返回不同 validation_scope 与证据。纯指令“读文件成功”不是 executable 测试通过。
5. executable fixture 经 Worker 和受控 workspace 执行，固定输入输出、副作用、超时、清理；API 只提交。行为 fixture 明确 subject 模型及费用授权。
6. instruction token overhead 单列并标 measured/estimated/unknown；已经进入模型输入的 token 不再另加一次总成本。

测试：`tests/skill/test_skill_injection_fixture.py`。注入顺序改变hash、资源hash漂移拒绝、deny不能被Skill覆盖、mock不能变real、instruction无entrypoint不被误标执行成功、两个Case资源隔离。

### M5-T08：无 Skill / v1 / v2 三组受控对照

**关联：G13/G14；验收 A12。依赖：T05/T07 和现有 M6-Lite。**

文件：扩展 `motte_sdk/comparisons.py`、`motte_contracts/comparison.py`、`motte_eval/comparison.py` 和统一创建服务，不新建 Skill 私有 Gate。

1. 用一次冻结实验请求生成三条普通 Run：相同 Dataset/Workflow/样本、Target/runtime/model、fixture初态、工具政策、scorer；只有 Skill 允许变化。
2. 各臂独立 workspace/DB/session；保存 paired case keys、arm ID、Run ID 和显式 scoring_pass_id。
3. 预算政策必须选 same-total-budget 或 same-execution-budget；Skill overhead 如何计入预留写入比较条件，不用更多模型预算冒充 Skill 增益。
4. ComparisonPolicy 显式允许 skill 因子；其余模型、工具、环境、重试、人工干预、评分版本变化给不可比原因。人工介入后不得继续纯 Skill 归因。
5. 输出 Case 配对质量、失败分类、token overhead、工具次数、reported/estimated/unknown成本，保留未尝试/缺证据分母。
6. 统计/Gate 复用 M6；本阶段不重写全量 Experiment 调度和显著性统计。

测试：`tests/integration/test_skill_ablation.py` 和比较契约。偷偷改变预算/模型/工具/scorer必须可见；相同业务ID跨臂不串扰；固定历史pass的报告不随current更新。

### M5-T09：Judge 配置、用途与可靠评分作业

**关联：G15/G16/G17；验收 A13/A14/A15/A16/A18。依赖：M1 评分与调用日志；拆三提交包。**

文件：扩展 `motte_eval/judge.py`、Provider 装配、`motte_contracts/{evaluation,evidence}.py`（InvocationRecord 在 evaluation.py）、`motte_storage/invocations.py`/`pg_audit_store.py`/run_store、RunService、WorkerLoop；拟新增 `motte_eval/rubrics.py`、`motte_sdk/scoring_jobs.py`。

**T09a 契约与无费用验证：**

1. JudgeSpec 固定发布模型profile/hash、prompt/rubric版本/hash、criteria、输出schema、参数、输入证据选择、缺失政策、校准版本与预算。禁止从 subject 的会话内部取隐藏状态。
2. 输入由选定 Observation 白名单字段及归属验证过的 Artifact 组成，保存 input evidence digest。候选内容作为数据引用；不给 Judge 业务工具。输出只要简短理由和实际证据引用，不索取隐藏思维过程。
3. 输出schema逐 criterion 校验；不属于输入集合的证据引用拒绝。拒答/畸形/超时/缺证据保持 evaluator_error 或 insufficient，不补0/pass。
4. preflight 返回用途、模型、样本、最大调用次数（包含重复/换序）、tokens上限、价格覆盖、预算可执行性。无授权零调用；价格未知不声称精确美元硬上限。

输入契约必须区分 single 与 pairwise。pairwise 固定同任务的两个 candidate_id、各自 Run/pass/Observation 或校准样本版本引用、每侧 evidence allowlist/hash；presentation_order 独立于 candidate 身份。A/B 输出先映射为稳定 candidate_id 再算胜率。非 Run 来源的校准样本归属 calibration_job/sample；扩展调用账本为经过校验的 subject-or-calibration owner union，保留现有 subject run_id/case_id 必填约束，不造假 Run/CaseAttempt。正反序 fingerprint 不同且各计一次费用，均引用同一固定候选对。

**T09b 持久作业与调用账本：**

5. 请求幂等键与内容fingerprint分开。fingerprint含 Run/source pass/Observation hashes/cases/Judge+rubric+params/input selector/budget policy/mode。原子持久 request→job→reserved pass ID，重复同键同内容复用，异内容409；显式重复校准用新的请求键。
6. 同一 WorkerLoop/执行锁领取 ScoringJob；不在 API handler执行、不造假 subject Run/CaseAttempt 来套接口。Invocation使用kind=model并扩展purpose=judge、job/pass/criterion归属，费用账本与subject分开。
7. prepared/dispatching/settled边界必须先落库后发出动作；关闭会重新付费的不安全 Provider 自动重试。收到响应后的解析重试与模型重试分开。
8. 评分不确定时只隔离 job；原completed Run、Observation和有效pass不改变。历史报告GET与普通offline rescore不得领取或触发job。

取消由 ScoringJob 自己处理：幂等 cancel 请求与 job revision CAS；queued/prepared 且确认未 dispatch 时取消为零调用，dispatch 与 cancel 争用同一状态边界。已发远端请求只能尝试中断，不能宣称未计费；未知结果保留 indeterminate，不重发。已收到的原始响应保留。publish 事务若先赢，保留完成 pass 并返回 already_completed；cancel 先赢则不发布新的 current pass。任何路径都不修改原 subject Run 的状态/证据，不复用 RunService.cancel。

**T09c 原子发布：**

9. 待执行/失败/部分证据保存于job，不先append为ScoringPass；显式发布政策确定何时一组包含non-scored指标的结果可正式发布。
10. 终态ScoreSet+预分配ScoringPass+job完成receipt+current指针以同一事务CAS提交。通知或HTTP响应丢失不能重新执行Judge。提交后重复调用只返回原receipt。
11. ScoringPass记录真实Judge/rubric/config/calibration/inputhash/purpose及source pass，不沿用原subject scorer身份；既有baseline继续固定原pass。

12. 在 `ComparisonService._manifest_view(run_id, scoring_pass_id)` 或等价统一投影中，消费**所选 pass**的 scorer/Judge/rubric/config/calibration/input-selection/manual-revision provenance，而不是原执行 manifest 或当前 Judge 设置；输入中央 comparison/report identity。历史缺字段保持 unknown，不能从当前配置补齐。正式 Gate 的校准资格也只来自所选 pass，执行 manifest 保持不可变。

调用崩溃窗口必须逐项测试：

| 故障点 | 恢复要求 |
|---|---|
| prepared之前 | 零调用，没有冒出的pass |
| prepared后、dispatching前 | 只有明确无外部发送证据及作业政策才可继续 |
| dispatching后、请求前/响应落库前 | 不确定，job待复核，零自动重复费用 |
| 响应已settled、解析前 | 只重做确定性解析，不再次调用Provider |
| ScoreSet/pass事务中断 | 全有或全无；不出现半个current pass |
| 事务提交后、通知/HTTP响应前 | 返回原job/pass，不追加、不收费 |

测试：`tests/evaluators/test_judge_execution.py`、`tests/storage/test_scoring_jobs.py`、Worker恢复和API幂等测试；Memory、SQLite独立连接、PG独立连接均验事务。恶意候选试图改rubric/索取工具/伪造证据必须失败或留下实际分歧，不能仅凭JSON包裹宣称免疫注入。

补充强制反例：A/B 与 B/A 映射为同一稳定候选身份、两侧有效证据均可用、跨候选伪引用拒绝、两次调用计量且 settled 后恢复零新费用；queued/prepared/dispatch/response/publish 各时刻取消及重复取消；原 subject 保持原状。相同 Observation 的 rubric-v1/v2 pass 默认不可比；新 calibrated pass 不能使旧 experimental pass 获得 Gate 资格；固定旧报告引用在新 rescore 前后比较结果相同。

### M5-T10：校准、实验性状态与人工修订

**关联：G18/G19；验收 A17。依赖：T09。**

文件：拟新增 `motte_eval/calibration.py`；扩展资源/评分服务/报告；沿用 ScoringPass，不修改原分数。

1. CalibrationSet记录来源、标注说明、annotator、复核记录、版本/hash和适用rubric/模型/config。首批至少30条确实经人工复核的样本，覆盖明确通过/失败/边界/缺证据/注入。
2. Agent可以生成候选样本，不能伪造“人工已复核”。若尚无人工标签，软件功能可验证，G18/T10真实校准验收必须标not_run/blocked。
3. 输出逐criterion混淆/分歧、missing/refusal/error率、重复评分稳定性、pairwise正反位置结果；每次重复/换序均记Judge调用和费用。
4. CalibrationPolicy阈值由具体rubric用途固定；未校准或配置变化的Judge为experimental，不进正式阻断Gate，不继承上一模型/rubric的合格状态。
5. 人工修订指定source pass、actor、reason、证据和差异；稀疏改动生成完整新ScoreSet，未变指标带来源复制。并发修订用显式CAS政策，不能覆盖较新current。
6. 读取校准和修改历史零模型调用；人工修订无需Judge费用，不偷跑“验证一下”。

测试：`tests/evaluators/test_judge_calibration.py`、评分历史/基线回归。新rubric不继承旧校准、合成标签不满足人审门、换序调用数和费用准确、两个并发人工修订不丢记录。

### M5-T11：API、CLI、Web 可用链路

**关联：G21；验收 A18及全部用户路径。依赖：T05/T08/T10。**

文件：`apps/api/app/{main,schemas}.py`、`packages/cli/motte_cli/main.py`、OpenAPI和生成TS；拟新增 `apps/web/src/evalTypes/{scenario,skill}/` 及Judge管理视图，公共组件优先复用。

1. 资源管理采用现有版本资源入口，拟议路由组workflows/skills/judges只在确认没有同义入口后新增。validate/preflight仅纯校验；fixture/行为测试返回现有Run引用；Judge显式返回ScoringJob/pass引用。

Judge 作业提供独立提交、只读查询和幂等取消入口；CLI/Web 取消只作用于 job，展示 pending cancellation/indeterminate 与已完成竞态结果。API/CLI 端到端验证重复取消返回同一状态、刷新零调用、原 subject 状态与证据不变。
2. CLI先检查真实argparse结构再定名，提供scenario validate/run、skill validate/test/compare、judge validate/calibrate及显式Judge重评分；旧run/rescore默认行为不变。每条输出稳定JSON与正确非0错误码。
3. 页面完成资源→校验→预检→提交→监控→失败下钻→固定pass报告链；不能只有JSON编辑框和列表。Workflow用文本/schema编辑，明确字段错误，不做画布。
4. 监控展示逐步状态、checkpoint、受控工具模式、隔离/清理错误、未知结果。Skill明确三种验证范围和三臂条件差异；Judge展示证据、rubric、校准状态和独立成本。
5. 付费提交前显示用途、模型、数量、最大次数、已知/未知费用与预算，单独取得授权；仅用户主动操作可提交。读历史、切换pass、刷新不能收费。
6. UI按AGENTS路由技能：新页面design-taste-frontend、改页面redesign-existing-projects，DESIGN锁定3/2/7。只用既有token/Radix/Phosphor，状态由STATUS_META，迟到请求不能覆盖新Run/Case/pass，失败保留表单。

测试：`tests/api/test_scenario_skill_flow.py`、CLI端到端测试、`apps/web/tests/{ScenarioPages,SkillPages,JudgesPage}.test.tsx`。跨实例API/Worker、未知能力禁用、无消费者不假接受、失败后仍可查看原输入、刷新只读、变更选择时迟到响应被丢弃。运行完整Web test/build及生成契约漂移检查。

### M5-T12：代表性业务验收与交接

**关联：G20/G22；验收 A01–A18。依赖：全部工作包。**

1. 合成“取消订单前确认”场景：请求取消→目标询问→checkpoint保持active且count=0→确认→只取消一次→最终checkpoint cancelled/count=1。终态输出与过程/次数联合评分。
2. 同一公共链路跑七类边界：成功、缺确认、工具错误、按预算恢复、超时、越权、禁止/重复副作用。错误Target最终状态正确也必须过程失败。
3. 真正跑静态Skill、executable fixture、固定Target行为fixture和三臂对照；无相应能力时明确拒绝，不自动换Target。
4. Judge离线协议/预算/恢复和真实人工校准分别记录；subject live和Judge live分别授权、固定模型/预算/次数、保留调用与成本证据。
5. 对Direct/GSM8K/Agent/Harbor/M4运行同一套回归；Windows/POSIX/PG/Docker分别记录可用性。测试skip不算跨平台通过。
6. 编写 `docs/verification/M5.md`，完整G/T/A矩阵每格指向最终commit和测试/工件；最终独立全分支审查。M6接收固定版本、budget/intervention/coverage和RunReportRef；M7接收转换来源、校准来源、所有权及保留规则。

测试：`tests/integration/test_business_regression_slice.py`。验收需走真实公共入口与Worker，不能靠直接调用engine证明全链路。最终门禁失败不得用“已有基础设施”标签自动豁免。

## 5. 需求与验收覆盖矩阵

| 目标 | 主责包 | 退出证据 |
|---|---|---|
| M5-G01 | T01/T05 | 发布Workflow到现有Run |
| M5-G02 | T01/T03 | 七种步骤、when/expect、有界嵌套 |
| M5-G03 | T01 | 许可表达式、恶意输入拒绝 |
| M5-G04 | T02 | owner/reset/部分清理 |
| M5-G05 | T03 | Case内历史保留、跨Case隔离 |
| M5-G06 | T03 | 阻塞目标实际停止 |
| M5-G07 | T05/T07 | 四种mode与权限交集落地 |
| M5-G08 | T04 | 过程、状态、副作用联合指标 |
| M5-G09 | T02/T04 | checker/gold不可见反例 |
| M5-G10 | T06 | 三种Skill分型 |
| M5-G11 | T06/T07 | 资源/依赖/顺序/生效hash |
| M5-G12 | T07 | 三种验证范围和清理 |
| M5-G13 | T08 | 三臂固定条件与允许因子 |
| M5-G14 | T08 | 开销/次数/成本不重复计费 |
| M5-G15 | T09 | 独立固定Judge/rubric输入 |
| M5-G16 | T09 | 非成功输出不伪造分数 |
| M5-G17 | T09 | 授权、用途、预算、不重付 |
| M5-G18 | T10 | ≥30人工来源样本、重复/换序 |
| M5-G19 | T10 | 新pass、旧baseline不变 |
| M5-G20 | T05/T12 | 代表性业务七种边界 |
| M5-G21 | T11 | API/CLI/Web实际可用 |
| M5-G22 | 全部/T12 | 已有套件和恢复无回归 |

| 验收 | 主责包 | 必须观察的结果 |
|---|---|---|
| M5-A01 | T12 | 确认后且只取消一次，最终回复符合 |
| M5-A02 | T04 | 提前取消即过程失败，即使终态正确 |
| M5-A03 | T02 | 初始化失败有独立清理证据 |
| M5-A04 | T03 | 错误/恢复可见，副作用不重复 |
| M5-A05 | T01 | 缺字段/恶意表达式明确拒绝 |
| M5-A06 | T03 | 步数/时长限制和实际中断 |
| M5-A07 | T05 | 副作用后崩溃needs_review，零重放 |
| M5-A08 | T02/T03 | 同业务ID不同Case不串扰 |
| M5-A09 | T07 | Skill不扩权或提升工具mode |
| M5-A10 | T06/T07 | 纯指令无需入口，验证标签诚实 |
| M5-A11 | T06 | 恶意资源/未固定依赖拒绝 |
| M5-A12 | T08 | 预算或其他条件变更不能归因Skill |
| M5-A13 | T09 | 未授权零调用，未知价格不伪硬上限 |
| M5-A14 | T09/T10 | 注入候选无工具权限，分歧可查 |
| M5-A15 | T09 | 畸形/拒绝/超时/伪引用不给pass |
| M5-A16 | T09 | Judge不确定不重复付费且subject不变 |
| M5-A17 | T10 | rubric/人工修订新pass，原历史固定 |
| M5-A18 | T09/T11 | 所有GET及默认rescore零Judge调用 |

## 6. 存储演进与验证命令

迁移以开工时 Alembic head 为准；M4 交互存储可能已在旧 `0009_runtime_resources` 之后追加版本，禁止直接假设下一条是0010。T01/T06/T09分别在真正消费资源时新增最小schema。三存储保证同版发布、去重、CAS、作业领取与pass事务语义一致。

升级测试必须从有真实旧数据/评分/命令的数据库开始，不能只在空库建表。验证重复迁移、历史读取、唯一约束、并发冲突；若新历史证据已引用资源，downgrade具名拒绝，不能为回退删除证据。

每包首先运行该卡中的focused测试，再执行受影响的原契约。以下为将来实际运行命令，不是已通过记录：

```bash
uv run pytest -q -m "not live" tests/contract tests/scenario tests/skill tests/evaluators
uv run pytest -q -m "not live" tests/integration/test_business_regression_slice.py
uv run pytest -q -m "not live" tests/runtime tests/storage tests/api tests/cli
uv run ruff check .
uv run mypy packages/contracts
make openapi
pnpm --dir apps/web test
pnpm --dir apps/web build
make check
```

使用实际PG测试实例运行PG事务与迁移；普通CI不使用个人订阅或真实模型。没有Docker/PG/对应平台时写not_run及环境原因，不把静态DDL核对算数据库验收。验证日志和来源材料用受控临时目录，不能混进源码或读取用户凭据。

## 7. 外部依赖、回退与最终交付

必须由真实来源提供的依赖：旧项目可访问的固定源码及许可；至少30个人工复核校准样本；subject与Judge各自的具体模型/凭据引用/费用或调用上限；需验收的平台与PG/Docker环境。遇到缺项立即提出一个明确问题，同时继续不依赖它的工作。不得伪造人审、不隐式付费、不因环境缺失删除验收项。

回退关闭scenario/Skill/Judge新提交或领取能力，先确认活动受控进程停止，再做受控资源清理。保留Workflow、Skill快照、ScoringPass、job不确定记录、校准/人工修订、原始证据及导入来源；不得用删除历史掩盖失败。

最终文档与对应代码同提交维护：`docs/protocols/scenario-workflow.md`、`docs/operations/scenarios.md`、`docs/operations/skills.md`、`docs/operations/judges.md`、`docs/migration/workflow-parity.md`、`docs/verification/M5.md`。交付摘要列完成矩阵、实际测试、剩余阻断、提交/分支、费用证据和M6/M7接口；未经新的明确要求，不自动实现M6/M7或自动合并M5。
