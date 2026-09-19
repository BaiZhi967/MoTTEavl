# M1：原生 Agent 与通用评分闭环详细规划

> 状态：待实施的阶段设计与工作包，不代表功能已完成。实施时按工作包执行测试先行、独立评审和小提交；可使用 superpowers:subagent-driven-development 或 superpowers:executing-plans。所有复选框只有取得对应证据后才能勾选。

**Goal：** 让同一文件任务通过 Web/CLI 选择不同模型驱动 Builtin Agent，获得真实工具执行、文件产物、多指标评分和可定位的失败证据。  
**Architecture：** 扩展现有 RunDispatcher、ExecutionBackend、Provider、CaseAttempt 和 ScoringPass。Agent 执行产生统一 Observation，评分器只读取冻结证据，不持有 Worker 或特定框架私有对象。  
**Tech Stack：** 沿用 Python 3.12、uv、FastAPI/Pydantic、React/Vite、pnpm、SQLite/PostgreSQL、Docker；不引入第二个任务调度器。  
**Spec：** [总路线](../ROADMAP.md)第 4、6、13、16 节；[阶段索引与共通约束](README.md)。  
**代码基线：** `a668d13ee5ea0c3613648f8992ecaa4a148d3855`。文件路径标为“新增”的是本规划建议，不表示仓库当前已有。

## 1. 起点、依赖与交付边界

现有 `motte_agent/builtin_react.py` 是文本 JSON action 循环，具备工具错误回灌和步数预算；其事件目前由实例列表收集。当前已有 Dispatcher/Backend 注册、Provider 身份证据、追加评分和单执行器恢复。M1 的工作是将这些组件接成可持久运行的产品链路，不重新实现已有基础。[B1][B2]

进入条件：当前完整性回归可运行；一个发布的 ModelProfile 或离线 Provider fixture；受控测试 workspace。离线阶段不依赖真实模型凭据。Docker 与显式 live 验证分别记录，不把环境缺失写成测试通过。

交付分两层：M1-Core 是离线、假 HTTP 和真实 Docker 确定性链路；M1-Supported 在前者基础上补小规模真实模型证据。代码合入可以先完成 Core，正式宣称真实 Agent 支持必须满足 Supported。

### 本阶段包含

- 通用 Observation、EvaluatorSpec、MetricResult 与现有 Score/ScoringPass 的映射。
- exact/contains/regex 的兼容封装，以及 JSON Schema、文件、退出码、工具调用断言。
- Builtin Agent 的 native-tool 与 legacy-json 明确模式、完整消息历史、工具 schema、执行预算。
- 每步证据、产物采集、取消与不确定副作用恢复。
- Agent 操作、监控、样本详情、结果和评分历史；至少能并列阅读两个模型结果。

### 本阶段不包含

多 Agent、自动 Planner、任意 MCP 服务发现、Skill 商店、可视化工作流编辑器、Pi 真接入、LLM Judge、跨类型统计系统。模型 token 流不是本阶段阻塞条件；Run 事件流必须可用。API/存储只拆分本阶段真正修改的职责，不进行全仓库重排。

## 2. 最终具体目标清单

- [ ] M1-G01：Backend Registry 可注册并执行 `builtin-agent@1`，未接线时保持 unavailable。
- [ ] M1-G02：执行模式、system prompt 版本、工具 schema、预算及模型快照进入 manifest。
- [ ] M1-G03：native-tool 模式在模型不支持工具时创建期拒绝；不自动切换 legacy-json。
- [ ] M1-G04：每个样本独立消息历史和 workspace，不继承上一个 Case 的内容。
- [ ] M1-G05：所有模型请求、工具请求/结果、拒绝和终止都产生持久证据。
- [ ] M1-G06：工具错误能回灌并在预算内恢复，未知工具不能执行。
- [ ] M1-G07：时长、步数、工具次数、可观察 token 预算生效并有具体停止原因。
- [ ] M1-G08：取消可阻断后续模型/工具调用，运行进程与沙箱清理结果可查。
- [ ] M1-G09：外部副作用可能已经发生时转入 needs_review，不自动重放整个 Agent。
- [ ] M1-G10：产物具有安全相对路径、媒体类型、大小、内容哈希和可用状态。
- [ ] M1-G11：同一 Observation 可产出多个带来源、版本、分母的指标。
- [ ] M1-G12：无期望、证据缺失、不适用、评分器错误不被映射成通过。
- [ ] M1-G13：历史评分保留，读取报告不重算，离线重评分不调用被测模型。
- [ ] M1-G14：模型输入不含 gold、隐藏文件断言、评分规则或本机凭据。
- [ ] M1-G15：Web、CLI、Worker、API 对同一 Run 的状态和分数一致。
- [ ] M1-G16：正常产物、可恢复错误、越权/预算耗尽三组代表性任务通过验收。
- [ ] M1-G17：既有 Direct LLM/GSM8K/Replay 的评分与恢复回归不变。
- [ ] M1-G18：支持声明附版本、环境、fixture/integration/live 证据及已知限制。

## 3. 模块划分与实现范围

| 模块 | 已有触点 / 新增建议 | 输入与输出 | 本阶段实现范围 / 禁止越界 |
|---|---|---|---|
| 评测契约 | 扩展 `packages/contracts/motte_contracts/evidence.py`；新增 `evaluation.py` | 持久 Case 证据 → Observation / MetricResult | 定义证据引用、指标状态、评分配置；不把框架对象放进公共模型 |
| 评分执行 | `packages/evaluators/motte_eval/`；新增 `registry.py`、`observation.py`、`files.py`、`tools.py` | Observation + EvaluatorSpec → MetricResult 列表 | 确定性评分和隔离错误；不创建 Run、不直接调用 Provider |
| 原生运行时 | `packages/agent-runtime/motte_agent/builtin_react.py`、`protocol.py`；新增 `native_tools.py`、`budget.py` | CaseInput + runtime config +受控工具 → AgentResult | 正确历史、动作解析、预算、EventSink；不写数据库 |
| 后端装配 | `packages/sdk-python/motte_sdk/execution_backends.py`、`dispatcher.py`、`service.py`；新增 `agent_backend.py` | 固定 manifest → ExecutionHandle | 依赖装配、样本隔离、证据交给应用层；不建立第二个主状态机 |
| 工具与沙箱 | `packages/sandbox/`、现有 ToolRegistry 所在实现 | ToolRequest + SandboxPolicy → ToolResult | 限权工具、路径边界、超时、清理；不挂载宿主凭据和 Docker socket 给任务 |
| 持久证据 | `packages/trace/`、`packages/storage/` | 事件/文件 → 稳定引用、调用日志 | 扩展现有事务边界和 Artifact 存储；不覆盖历史结果 |
| API/CLI | `apps/api/app/main.py`、`apps/api/app/schemas.py`、`packages/cli/` | 配置/预检/运行命令 →类型化响应 | 在现有 Run 入口扩展；API 不运行工具 |
| Web | `apps/web/src/evalTypes/`；新增 `agent/`；复用 components | API 事实 →操作/监控/结果视图 | 模型选择、预算、逐步事件、文件和多指标；不复制状态映射和设计 token |

新增目录中的 package 依赖只在真正实现时更新。已有通用名如 Observation/Score 优先演进原定义，不并行建立另一份同义模型。

## 4. 关键契约与数据流

### 4.1 提议的字段与语义

| 对象 | 最小字段 | 不变量 |
|---|---|---|
| Observation | schema_version、run_id、case_id、attempt_id、final_output、termination、event_refs、artifact_refs、coverage、evidence_hash | 输入来自已冻结证据；相同 evidence_hash 表示同一评分输入视图 |
| EvaluatorSpec | evaluator_id、version、config、required_evidence、missing_policy、metric_ids | config/hash 在开始评分前固定；未知版本拒绝 |
| MetricResult | metric_id、value、status、passed、unit、evaluator_id/version、evidence_refs、reason、denominator | passed 可空；error/insufficient/not_applicable 不伪造成 0/通过 |
| AgentResult | final_output、termination_reason、observed_usage、artifact_refs、evidence_coverage | Agent 自称完成不是评分通过 |
| ToolResult | call_id、tool_name、status、output_ref、duration、side_effect_summary | 每次调用和回传用 call_id 关联 |

MetricResult 是拟议评估层视图，持久化仍进入现有 ScoreSet，不再创建同名分数事实库。Metric 状态建议为 `scored/insufficient_evidence/evaluator_error/not_applicable`；原有 `no_expectation` 语义通过显式原因和分母政策保留。

### 4.2 一条 Run 的完整链路

1. 公共创建入口验证 Scenario、已发布模型、工具模式和预算，冻结 manifest。
2. Dispatcher 在执行锁内 claim，装配 AgentBackend；为每个 Case 建独立 workspace。
3. 应用层持久化 CaseAttempt；Agent 每次模型/工具动作前后经受控调用包装器写操作日志。
4. Provider 返回结构化响应；Agent 把 assistant 决策和 tool 结果完整加入历史。
5. 终止后收集文件和轨迹，创建冻结 Observation；即使任务失败也尽量采集证据。
6. Evaluator 执行多个指标，创建新 ScoringPass；统一 Run 生命周期最终化。
7. Web/CLI 读取指定 ScoringPass；不能在 GET 时根据最新文件重新评分。

### 4.3 多步调用与恢复

当前 CaseAttempt 是整段样本的事务主线。M1 不把一个 Agent 的多步调用误当作单个无副作用函数。建议在 CaseAttempt 下增加操作日志（实现名称拟为 InvocationRecord）：operation_id、kind、step、prepared/dispatching/settled、请求/结果摘要及引用。该日志是证据与恢复判定，不是另一套 Run 状态机。

调用已 dispatch 但结果未确认时，保留 CaseAttempt 的 indeterminate/Run needs_review。若无法证明剩余步骤可安全恢复，默认停止并要求显式 retry。已获得结果但后续工具可能写文件时，不能仅凭模型响应存在就自动重启循环。M1 不承诺任意 Agent 中途无损续跑。

### 4.4 消息历史与动作模式

legacy-json 保留既有输入协议，但补齐 assistant 历史；行为变化产生新的 runtime/prompt 版本，不改写旧 manifest。native-tool 使用规范工具声明和 call_id，完整保留 assistant tool_calls 与工具响应。一次响应有多个工具调用时 M1 默认顺序执行，顺序进入协议；后续并行化另行版本化。

畸形 JSON、重复 call_id、未知工具、参数 schema 不匹配，产生可区分的 observation；是否允许模型纠正由固定政策控制且消耗步数。不得把任意模型输出拼成 shell 字符串。

## 5. 确定性评分器范围

| 评分器 | 配置 | 成功与失败定义 | 缺证据处理 |
|---|---|---|---|
| exact | expected、显式 normalization | 按固定规则全值比较，不偷偷 strip/lower | 没有 expected 保留 no_expectation |
| contains | expected substring、大小写政策 | 固定字段包含匹配 | 字段不存在为 insufficient |
| regex | pattern、flags、输入上限、执行期限 | 受限匹配；不允许无限回溯阻塞 Worker | pattern 错误为 config error；执行超时为 evaluator_error |
| json-schema | schema 引用/hash、dialect | 解析与 schema 验证分别给原因 | 空/无效 JSON 是被测输出失败，不是评分器故障 |
| file-exists | 受控相对路径 | 指定 Artifact 存在且可读 | 未完整采集时不能因列表无文件就判“不存在” |
| file-content | path、exact/contains/hash/schema | 读取冻结 Artifact，不读运行后可变目录 | 工件被删除或损坏为 insufficient |
| exit-code | 允许退出码 | 指定过程正常结束且码匹配 | 未观测退出码保持 unknown |
| tool-call | 名称、次数、参数 schema、禁止项 | 实际调用满足规则 | 不完整轨迹不能证明“从未调用” |
| no-forbidden-write | 受控 workspace 前后快照与禁止模式 | 完整范围内没有禁止变更 | 只说明已监控范围，不声称宿主无任何副作用 |

文本脱敏可能改变质量输入：评分应读取与 evidence_hash 对应的冻结版本；敏感内容检测在受控内存中产出安全的命中标记，禁止将密钥正文写入证据。脱敏前后不能在未声明情况下使用不同评分口径。

## 6. 预算、错误与安全策略

预算字段：max_steps、max_tool_calls、wall_time_sec、per_call_timeout_sec、max_output_tokens、total_token_limit、observed_cost_limit。配置必须是合理正值且有上限。预算分别标记 `enforced/observed/unknown`；无 tokenizer 或 usage 时不得把总 token 预算标成精确硬限制。

时长使用单调时钟；工具期限必须可中断实际进程，而不只是返回后检查 elapsed。取消请求需写审计并终止受控子进程，收集 cleanup 成功/失败；清理失败要显示残留资源标识，不能误报全部回收。

文件工具仅接受受控相对路径，拒绝绝对路径、`..`、逃逸 symlink、设备文件及超配额文件。模型侧工具目录不包含 gold 和隐藏 checker。默认禁止网络；真实外部工具在 M1 不接入。

## 7. 分批实施任务

每项都是独立可评审工作包；新增测试名称为验收规格，不是声称当前存在。

| 任务 | 消费 / 产出 | 实施内容 | 测试文件（拟新增）与明确预期 |
|---|---|---|---|
| M1-T01 | 现有 Case/Score → Observation/MetricResult | 演进契约，定义 hash 和状态映射，保留旧读兼容 | `tests/contract/test_observation_v1.py`：非法引用拒绝、未知字段政策、旧 Score 可读 |
| M1-T02 | Observation →确定性指标 | 注册上述评分器、分母政策、输入与耗时上限 | `tests/evaluators/test_deterministic_matrix.py`：每种 success/fail/missing/error 各有案例 |
| M1-T03 | 多指标 → ScoringPass | 连接现有评分入口，多指标 key 不冲突，追加不可变历史 | `tests/runtime/test_agent_scoring_passes.py`：rescore 后旧 pass 字节内容不变、模型调用数为零 |
| M1-T04 | 固定模型+工具 → AgentBackend | 注册 backend，填真实模型请求，模式显式，样本隔离 | `tests/runtime/test_builtin_agent_backend.py`：registry 可分发、未知模式付费前拒绝 |
| M1-T05 | 模型响应 →消息/工具循环 | 完整历史、参数校验、错误回灌、多 call_id 关联 | `tests/runtime/test_agent_tool_history.py`：第二轮请求必须包含第一轮 assistant+tool 记录 |
| M1-T06 | 工具请求 →受控文件产物 | 沙箱绑定、路径校验、资源限额、前后快照 | `tests/sandbox/test_agent_workspace_policy.py`：穿越/symlink/超配额拒绝，不影响其他 Case |
| M1-T07 | 多步动作 →调用日志与终止 | 持久调用边界、期限、取消、不确定结果与 cleanup | `tests/runtime/test_agent_interruption.py`：dispatch 后崩溃恢复为 needs_review，调用数不增加 |
| M1-T08 | 产物+事件 → Observation | 脱敏、完整度、截断、稳定引用、哈希验证 | `tests/trace/test_agent_observation_capture.py`：gold/密钥不入轨迹，缺工件可解释 |
| M1-T09 | 公共 Run → Agent 工作区 | API 响应类型、CLI、逐步轨迹和多指标 UI | `tests/api/test_agent_run_flow.py`；`apps/web/src/evalTypes/agent/AgentPages.test.tsx`：禁用原因、取消、retry 子 Run、历史 pass |
| M1-T10 | 代表任务 →交付证据 | 本地集成、真实 Docker、显式 live、兼容矩阵和运维说明 | `tests/integration/test_native_agent_slice.py`：三组任务闭环、原有套件回归 |

顺序：T01→T02/T04；T03 依赖 T02；T05 依赖 T04；T06/T07→T08；T09→T10。每项执行顺序固定为：写明确失败案例 → focused tests 确认缺口 →实现最小范围 → focused +相邻回归 →更新合约/文档 →提交。不得只提交空目录或更改状态标签作为任务完成。

### 验收数据样例（拟新增 fixture，不触发真实模型）

```json
{
  "case_id": "file-report-001",
  "input": "读取 workspace 中的 input.json，统计 enabled=true 的项目数，写入 report.json。",
  "fixture": {"input.json": [{"enabled": true}, {"enabled": false}]},
  "expected": {"report.json": {"enabled_count": 1}},
  "forbidden_paths": ["credentials.toml", "../outside.txt"],
  "limits": {"max_steps": 6, "max_tool_calls": 4, "wall_time_sec": 30}
}
```

上述 fixture/expected 是平台测试输入，不能整体传给模型。Provider 只接收 input 与允许读取的 input.json；隐藏 expected 在评分侧。

## 8. 验收测试矩阵

| ID | 注入条件 | 必须观察的结果 |
|---|---|---|
| M1-A01 | 正常读输入、写 report、final | 文件断言通过，工具和文件证据完整 |
| M1-A02 | 首次读错文件，第二次纠正 | tool_error 回灌，恢复成功但额外调用计入预算 |
| M1-A03 | 未注册工具 | 无工具副作用，记录 tool_denied |
| M1-A04 | 模型只有 final，无实际产物 | execution 可结束，file 指标失败 |
| M1-A05 | 不断提出工具调用 | 步数或工具预算阻断，明确 stop reason |
| M1-A06 | native 模式模型不支持 tools | 创建期结构化错误，模型调用数 0 |
| M1-A07 | 磁盘满/文件超过限额 | 产物不完整、错误分类明确，cleanup 执行 |
| M1-A08 | 模型调用中取消 | 无后续工具执行；取消和调用不确定性均保留 |
| M1-A09 | 写文件后进程崩溃 | 不自动重复危险操作，Run needs_review |
| M1-A10 | 一个评分器异常 | 其他结果保留，异常指标不通过，不覆盖历史 pass |
| M1-A11 | 轨迹截断或 Artifact 丢失 | no-forbidden-write 不因未看到违规就判通过 |
| M1-A12 | 两个 Case 使用同一路径名 | workspace 隔离，结果互不污染 |
| M1-A13 | Provider 不报告 token/cost | null+coverage，不虚构 0 或严格硬预算 |
| M1-A14 | GET 与 rescore 反复请求 | GET 零模型/评分副作用；每次 rescore 新 pass |
| M1-A15 | replay/旧 Direct/GSM8K 数据 | 既有公共契约和合法分母不退化 |

## 9. API、CLI 与 UI 目标

沿用 `/api/v1/runs`、events、cancel、retry、rescore、report、scoring-passes。新增 backend 能力描述和 evaluator 目录时使用类型化响应，字段由 registry 派生，不让前端维护第二份支持列表。

Web 操作页：任务/数据版本、已发布模型、模式、工具集合、预算、预检摘要。监控页：case/step、模型调用、工具参数与结果、终止原因。结果页：Artifact、评分依据、缺失原因、ScoringPass 切换。比较入口先并列同任务结果，正式可比性判定复用 M6-Lite，不先做综合排名。

遵守 `apps/web/DESIGN.md`、Radix 交互原语、Phosphor 图标与 STATUS_META；新增状态不能只写在局部组件。包含键盘操作、加载、空态、权限/能力不支持、失败输入保留、长日志截断提示。

## 10. 验证命令、发布与回退

实现各测试文件后执行：

```bash
uv run pytest -q -m "not live" tests/contract tests/evaluators tests/runtime tests/trace tests/api
uv run pytest -q -m "not live" tests/integration/test_native_agent_slice.py
uv run ruff check .
uv run mypy packages/contracts
pnpm --dir apps/web test
pnpm --dir apps/web build
make check
```

真实 Docker 和 Provider 验证按单独 live 标记/运行说明显式执行；上述路径是新增测试落位要求，不是已运行的命令记录。任何不可用依赖都记为阻塞/未验证。

有 schema 变化时沿现有 migration 链追加版本，SQLite/PG 行为一致。关闭新 backend capability 即可停止创建新 Agent Run；历史记录、证据和 ScoringPass 保持可读。不得通过删表或修改旧 manifest 进行回退。

产出文档（实施时新增/更新）：`docs/operations/native-agent.md`、`docs/protocols/provider-compatibility.md`、`docs/PROGRESS.md`、`docs/verification/M1.md`。M1 验收完成后向 M2/M4/M5 交付 Observation、Evaluator、受控工具和取消契约；向 M6 交付指标状态、覆盖和终止语义。

## 11. 固定来源

- [B1] [BuiltinReAct 源码](https://github.com/BaiZhi967/MoTTEavl/blob/a668d13ee5ea0c3613648f8992ecaa4a148d3855/packages/agent-runtime/motte_agent/builtin_react.py)
- [B2] [当前完整性与恢复约定](https://github.com/BaiZhi967/MoTTEavl/blob/a668d13ee5ea0c3613648f8992ecaa4a148d3855/docs/operations/platform-integrity.md)
- [B3] [当前兼容矩阵](https://github.com/BaiZhi967/MoTTEavl/blob/a668d13ee5ea0c3613648f8992ecaa4a148d3855/docs/protocols/provider-compatibility.md)
