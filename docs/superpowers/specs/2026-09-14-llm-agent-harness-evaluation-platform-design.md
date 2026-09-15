# MoTTEavl 评测平台设计

日期：2026-09-14  
状态：已确认设计（持续更新）  
范围：单机、单用户、长期维护的 LLM / Agent / Skill / Harness 评测平台

## 1. 项目目标与边界

MoTTEavl 用于统一评测以下对象：

1. 直接调用云端 LLM API，评测模型本身。
2. LLM + 项目提供的基础 Agent + Docker 沙箱，评测多步任务执行。
3. Agent + Skill，评测 Skill 的输入输出、工具选择、权限和副作用。
4. LLM + 外部 Harness，评测完整 Agent/Harness 的可观察行为和最终产物。

项目从当前仓库重新设计和开发。其他评测项目只作为参考，不复用其代码或目录结构。

交付形式为 Web + CLI，共享同一个 Python 评测 SDK 和运行协议。项目不设计多用户、多租户、RBAC 或跨用户资源共享。单机、单用户是产品边界，不降低协议、测试、审计和发布标准。

开发按完整产品路线分阶段交付，不采用临时原型或一次性实现。每个阶段都有明确的契约、验收门和可发布成果。

## 2. 已确认技术基线

| 部分 | 选择 |
| --- | --- |
| 评测核心、SDK、CLI、API、Worker | Python |
| API | FastAPI + Pydantic v2 |
| Web | TypeScript + React + Vite |
| Python 依赖与工作区 | uv workspace |
| Web / Pi bridge 依赖与工作区 | pnpm workspace |
| 任务队列 | Redis + Celery |
| 默认元数据存储 | PostgreSQL |
| 默认工件存储 | 本地 artifact 目录 |
| 可选对象存储 | MinIO |
| 宿主平台 | Linux/WSL2 一等公民；Windows 原生通过 Docker Desktop 支持并标注能力降级 |
| 沙箱 | Docker；Linux/WSL2 使用 rootless/非 root 能力，Windows 使用 Docker Desktop 等价安全配置 |
| 可观测性 | 平台事件协议 + OpenTelemetry |
| 模型适配实现 | 自有协议；LiteLLM 作为可替换的传输、重试和计量实现 |
| 首个外部基础 Agent | Pi Agent |
| 首批 Harness | Claude CLI、Codex CLI |
| 后续评测 Harness | Inspect AI |

具体 Python、Node、SDK 和 CLI 版本在实现前锁定，并写入依赖锁文件与兼容矩阵。

## 3. 架构分层

### 3.1 Control Plane

FastAPI 管理 Provider、模型目录、数据集、Scenario、Agent、Skill、Harness、运行和结果。Web 与 CLI 通过相同的公共契约访问这些资源。

CLI 同时支持本地执行与远程 API 模式。两种模式共享 SDK、参数校验和输出格式。

### 3.2 Execution Plane

Worker 负责运行状态迁移和 `prepare → execute → collect → score → aggregate → report` 生命周期。每次运行读取固定版本的 Scenario，并保存全部解析后的实际配置。

API 提交命令，Worker 执行命令。API 进程不直接执行不可信代码或沙箱命令。

### 3.3 Runtime Plane

运行时通过稳定接口接入：

- DirectLLMRuntime：直接模型调用。
- AgentRuntime：项目基础 Agent、Pi 和其他 Agent 框架。
- Skill Runtime：Skill 加载、fixture、权限和测试编排。
- HarnessAdapter：Claude CLI、Codex CLI、Inspect AI 和未来的外部执行器。

Skill 可以注入 Agent 执行，也可以通过 fixture 进行独立验证；它不要求独立的 LLM 调用循环。

### 3.4 Evidence Plane

所有执行器统一输出 `Run / TraceEvent / Observation / Artifact / Score`。Web、报表和 Evaluator 不读取框架私有对象。

平台保存标准化事件和经过脱敏的原始协议证据，覆盖模型调用、工具调用、沙箱命令、文件变化、重试、错误和评分依据。

## 4. Canonical Evaluation IR

公共协议包括：

- ScenarioSpec：任务、输入、数据集、执行器、环境、评分器和预算。
- Case：单个评测样本。
- Run / CaseRun：整次运行与样本运行。
- TraceEvent：带顺序和父子关系的执行事件。
- Observation：供评分器使用的输出、轨迹和产物视图。
- Artifact：文件、patch、日志、图片等产物。
- Score：指标值、通过状态、评分器版本和证据引用。
- ResolvedManifest：运行时实际生效的配置与依赖版本。

公共协议独立于 LiteLLM、Pi、Claude、Codex、Inspect 和数据库实现。

## 5. Provider 与模型配置

### 5.1 协议

首批支持三种协议族，并提供一个 OpenAI-compatible 兼容入口：

- `openai_chat`：OpenAI Chat Completions。
- `openai_responses`：OpenAI Responses API。
- `anthropic_messages`：Anthropic Messages API。
- `openai_compatible`：Chat Completions 兼容入口，结合厂商 profile 处理差异。

ProviderConnection 描述协议、base URL、认证引用和连接状态。ModelProfile 描述具体模型的能力与限制。同一家 Provider 的不同模型可以有不同能力；同一模型的不同接入端点也可以存在差异。

### 5.2 模型能力

每个 ModelProfile 配置以下信息：

| 分组 | 字段 |
| --- | --- |
| 身份 | provider、model ID、展示名称、协议、profile 版本 |
| 输入模态 | text、image、audio、video、file |
| 输出模态 | text、audio、image |
| 工具能力 | 工具调用、并行工具调用、强制工具选择、厂商托管工具、MCP |
| 输出能力 | 结构化输出、JSON Schema、流式输出、logprobs |
| 复现与缓存 | seed、prompt cache |
| Token 限制 | 上下文窗口、最大输入 token、最大输出 token |
| 其他限制 | 每次响应工具数量、图像数量/尺寸、请求体大小（适用时） |
| 推理 | 模式、等级、默认值、token budget 和原生参数映射 |
| 参数约束 | 支持范围、固定值、默认值、条件限制、不支持状态 |
| 来源 | 官方 API、官方文档、探针、手工覆盖、更新时间 |

视觉支持由输入模态中的 image 推导；Web 仍提供直观的视觉能力开关。上下文窗口、最大输入和最大输出分别记录，不能假定三者具有相同的厂商语义。

### 5.2.1 价格与成本

成本评测需要独立的、版本化的 PriceTable。ModelProfile 只引用当前生效的 price table，不把价格永久写死在模型能力字段中。

PriceTable 至少包含输入 token、输出 token、缓存命中输入、缓存写入输入、推理 token、托管工具调用和批量折扣等计价项；每项包含币种、单位、阶梯、有效期、来源 URL、来源类型和观察时间。无法确认的价格不用于预算硬门禁，只显示为未知。

每次 ModelResponse 保存 `price_table_version`、计费 token 分解和计算后的成本。成本计算使用请求发生时的价格版本，价格更新不改变历史 Run。

### 5.3 推理配置

ReasoningProfile 同时描述控制方式和允许值：

- 无推理控制。
- 推理开关：enabled / disabled。
- 推理等级：按具体模型声明允许的原生等级。
- 手动 reasoning token budget。
- adaptive thinking 及其 effort 配置。

平台提供 none、minimal、low、medium、high、xhigh、max 等常见标签，但模型实际支持的枚举以版本化 profile 为准，不强制所有厂商拥有相同等级。不同厂商的同名等级不视为等价算力。

原生映射示例：

- OpenAI Responses：`reasoning.effort`。
- Anthropic：`thinking.type`，以及适用模型的 `output_config.effort` 或 `budget_tokens`。
- GLM：`thinking.type`，以及适用模型的 `reasoning_effort`。
- Kimi：适用模型的 `thinking.type`，同时执行思考模式下的采样和工具约束。
- DeepSeek：按模型和协议描述 thinking/non-thinking 模式及其他原生控制项。

以上映射按官方文档和适配器版本维护，不能只根据厂商名称猜测。

### 5.4 请求参数与约束

ModelRequest 表达模型、消息/输入、system、temperature、top_p、max_output_tokens、stop、seed、tools、tool_choice、response_format、reasoning、metadata 和 provider_options。

ParameterProfile 描述 temperature、top_p、top_k、输出 token 上限、stop、seed、n、tool_choice、response_format 和厂商扩展的范围、固定值与条件关系。例如启用 thinking 后可能不允许某些采样值或强制工具调用。

参数策略：

- strict：不支持或冲突的参数在运行前拒绝。
- warn：允许显式选择的兼容处理，记录所有删改字段。
- passthrough：厂商扩展通过原生字段传递，仍记录实际请求。

正式评测默认 strict，不允许静默丢弃参数。

### 5.5 Profile 来源与证据

支持官方 Models API、官方文档缓存、capability probe 和手动覆盖。字段保存来源 URL、观察时间、当前生效值及冲突信息。

已讨论的默认合并次序为：手动覆盖 > Provider API > capability probe > 官方文档缓存。探针仅能更新它实际验证过的能力，不能从一次成功调用推断上下文或输出的硬上限。文档不明确或来源冲突的字段保留不确定性，不填入猜测值。

Profile 更新生成新版本，不改变历史 Run。历史运行绑定 profile hash 和实际参数。

### 5.6 调用证据

每次模型调用保存 canonical request/response、脱敏后的 provider request/response、usage、耗时、重试次数、错误分类和 profile hash。

三种协议保留自身结构：Chat 的 choices/tool_calls、Responses 的 items/events、Anthropic 的 content blocks/tool_use/tool_result。不能通过扁平化文本丢失多轮工具调用所需信息。

## 6. Agent 与 Pi 适配器

AgentRuntime 是平台稳定接口，接受任务、模型配置、工具、Skill、沙箱、预算和 EventSink，返回 AgentResult。

默认策略为自有薄运行时与框架适配器并存：

- BuiltinReActRuntime：平台可控基线，循环执行模型调用、工具分发、状态更新和终止判断。
- PiAgentRuntime：首个外部基础 Agent 适配器。
- 后续可接 LangGraph、OpenAI Agents SDK、PydanticAI 和外部 Agent。

Pi 使用 TypeScript Node bridge，不移植其源码到 Python。Pi 的 agent-core 负责 Agent loop，pi-ai 或对应 Provider 实现负责模型调用；平台协议记录实际经过的模型路径。使用完整 coding-agent 的 session/Skill 能力时，由 bridge 封装对应 SDK。

调用关系：

```text
Python Eval SDK → PiAgentRuntime → Node bridge → Pi Agent
       ▲                                  │
       │       tool_result                │ tool_call / events
       └── ToolRegistry ← Docker Sandbox ──┘
```

Bridge 注入模型、prompt、工具和 Skill，将 Pi 事件转换为平台事件。工具执行经 ToolRegistry 和 SandboxPolicy；bridge 不直接写数据库。

桥接协议包括 init、ready、run、tool_result、interrupt、model_call、tool_call、state、artifact、final、error。每条消息带 protocol、version、run_id、seq、type、payload。

Pi 的上游包名和版本以实施时核验的官方发布为准；兼容矩阵同时记录包、bridge 和事件解析器版本。

Agent 预算包括步数、token、时长和成本，终止原因标准化。初次运行创建独立状态，恢复和回放使用显式配置。

## 7. Skill 设计

Skill 以版本化 manifest 管理，包含名称、版本、说明、内容、依赖、权限、fixture 和适用的输入/输出约束。Skill 可以包含指令与辅助资源；对于可执行入口，另外声明其输入输出 schema 和执行权限。

Skill 支持：

- 独立 fixture 测试。
- 注入 Agent 的行为测试。
- 不同模型、Agent 和 Skill 版本的成对比较。
- 输入输出校验、工具选择、参数正确性、调用顺序、文件副作用和目标达成评分。

工具策略统一为 real、mock、replay、deny。Skill 的声明不会自动赋予权限，实际执行仍受平台策略约束。

## 8. Claude / Codex / Inspect Harness

首批 Harness 为 Claude CLI 和 Codex CLI：

- ClaudeCliHarness：使用 headless print 和 stream-json，读取结构化事件，收集最终输出、usage、session metadata 和 workspace diff。
- CodexCliHarness：优先 app-server stdio JSON-RPC，以 exec 批处理作为补充模式。
- InspectHarness：后续适配 Task/Solver/Scorer、sandbox 和评测日志。

Harness 生命周期：prepare、start、send、events、interrupt、collect、cleanup。

Harness 的双向交互使用 `HarnessChannel`。本地 CLI 模式通过受控 stdin/stdout 管道传递消息；Codex app-server 使用 stdio JSON-RPC；Web 端通过 `POST /runs/{id}/messages` 写入命令队列，并通过 SSE 接收回执。Worker 是唯一的转发方，负责把用户消息送到活动会话，并记录 `user_message`、`harness_request` 和 `harness_response` 事件。已结束或不支持交互的 Run 拒绝发送消息。

适配器负责进程、事件解析、取消和工件收集，不负责评分。CLI 的具体参数以固定版本的帮助和协议 schema 为准，不能依赖长期不变的命令行示例。

每次运行记录 executable/package 版本、parser 版本、启动参数、受控环境、工作目录、沙箱镜像和退出原因。

Claude/Codex 保留自己的 Agent 行为和工具策略。平台依据可观察事件、工具调用、文件变更、命令结果和最终输出评分，不假定可获取所有内部状态或隐藏推理。

## 9. Scenario 与执行生命周期

Scenario 使用 YAML/JSON，载入后形成不可变的 ScenarioSpec。字段包括 id/version、mode、dataset、model、agent、skills、harness、sandbox、evaluators 和 limits。

DatasetVersion 使用 JSONL 作为规范磁盘格式；每行是一个 Case：

```json
{
  "case_id": "case-001",
  "input": {
    "messages": [{"role": "user", "content": "..."}]
  },
  "expected": {"json_schema": "schemas/result.json"},
  "metadata": {"split": "test", "tags": ["extraction"]}
}
```

`case_id` 在一个 DatasetVersion 内唯一；`input` 必须匹配 Scenario mode 所要求的输入联合类型；`expected` 是评测器可消费的声明，不直接注入模型上下文；`metadata` 不参与模型输入，除非 Scenario 显式引用。DatasetVersion 记录文件 hash、行数、编码、schema 版本和来源。CLI 的 create/validate/version 都针对该 JSONL 格式，并拒绝重复 id、无效 JSON、缺少 input 或无法解析的 expected。

mode 提供 llm、agent、agent_skill、llm_harness 等使用入口。具体执行器、模型、Skill 和沙箱通过引用组合，不把所有模式的无关字段变成必填项。

运行生命周期：

1. prepare：固定 Scenario、数据集、模型 profile、参数、Skill/Harness 版本及环境。
2. execute：执行样本并产生标准事件。
3. collect：收集最终输出、轨迹和工件。
4. score：运行评分器。
5. aggregate：按样本、模型、执行器和重复次数汇总。
6. report：Web 展示，CLI 支持 JSON/JSONL 和报告导出。

ResolvedManifest 包含模型配置、数据集版本、Agent/Skill/Harness 版本、Git revision、镜像 digest、随机种子和预算。记录这些信息支持复核与受控重跑，不承诺云端模型输出完全确定。

## 10. 沙箱与评分

沙箱采用 Docker，独立工作目录，限制 CPU、内存、PID、磁盘和 TTL。Linux/WSL2 使用 rootless 或非 root 与只读 rootfs；Windows Docker Desktop 使用等价的非特权、只读和资源限制能力，并在 capability matrix 中标注差异。默认禁止任务工具任意出网，模型 API 等必要通信通过显式策略配置。

SandboxPolicy 分成三层：

1. `network_policy`：none、allowlist 或 unrestricted（仅显式开发配置）；决定网络命名空间和出口。
2. `command_policy`：按可执行文件和参数模式 allow/deny；只约束可执行命令，不替代网络控制。
3. `resource_policy`：CPU、内存、PID、磁盘、输出字节和 TTL。

命令允许不代表网络允许；网络拒绝也不等同于命令拒绝。平台分别记录三层策略和实际结果。

Sandbox 接口提供 create、exec、collect、destroy。Worker 管理生命周期，API 不运行任务命令。

评分分层：

- 确定性：JSON Schema、exact match、正则、退出码、单元测试、文件 diff。
- 轨迹：工具选择、参数、顺序、步骤覆盖、状态变化和预算。
- 模型评审：rubric、pairwise、criteria judge，记录模型和 prompt 版本。
- 人工复核：基于证据确认或修订评分。

Score 包含指标、值、通过状态、评分器版本、证据引用和适用的 judge metadata。模型评审校准、重复运行、pass@k、置信区间和回归门禁进入完整开发路线。

证据策略由 `EvidencePolicy` 控制：

```yaml
retention:
  trace_days: 90
  raw_provider_days: 30
  artifact_days: 90
  keep_failed: true
  keep_pinned: true
```

默认保留 90 天的标准 Trace 和工件、30 天的原始 Provider payload；失败 Run 保留，用户标记的 Run 不自动清理。清理由 Worker 执行并记录删除事件，删除前保留 hash、大小和元数据审计记录。以下字段无论出现在 header、环境变量、JSON key 还是嵌套 provider raw 中都必须脱敏：`authorization`、`x-api-key`、`api-key`、`api_key`、`access-token`、`refresh-token`、`cookie`、`set-cookie`、`proxy-authorization`、`ANTHROPIC_API_KEY`、`OPENAI_API_KEY`、`MOONSHOT_API_KEY`、`ZHIPUAI_API_KEY`、`DEEPSEEK_API_KEY`、以及配置的自定义 secret key。脱敏值统一替换为 `[REDACTED]`；原始 secret 不进入数据库、artifact、SSE 或 CLI 输出。

## 11. 数据与状态

核心实体：

- ProviderConnection、ModelProfile、PriceTable 与来源记录。
- Dataset、DatasetVersion、Case。
- Scenario、ScenarioVersion。
- AgentDefinition、AgentVersion。
- Skill、SkillVersion。
- HarnessDefinition、HarnessVersion。
- Run、CaseRun、ResolvedManifest。
- TraceEvent、Observation、Artifact。
- Score、EvaluationReport。

默认 PostgreSQL 保存元数据与查询所需 JSON；本地 artifact 目录保存大型工件，MinIO 为可选实现。Redis 负责任务队列和实时传输。SQLite 与内存/本地队列用于开发配置，但不作为并发运行的默认配置。API、Worker 和 migration 使用同一份项目运行时镜像构建，不能依赖裸 Python 基础镜像在启动时恰好存在项目依赖。

Run 主流程状态：

```text
created → validating → queued → preparing → running
        → collecting → scoring → aggregating → completed
```

其他结束或阻止执行状态包括 failed、cancelled、unsupported、profile_stale。

`profile_stale` 只表示排队或准备阶段发现当前 ModelProfile 已过期、被撤销或与 Provider 探测结果冲突。它不会自动标记已完成的历史 Run；历史 Run 永远引用原 profile 版本。处于 `profile_stale` 的 Run 不得调用模型，用户刷新或选择新 profile 后创建新的 retry Run。`rescore_run` 不受 profile_stale 影响，因为它不重新调用模型。

Run 内 Case 默认按受控并发执行。Scenario limits 包含 `case_concurrency`、Provider `requests_per_minute`、`max_in_flight`、重试次数、退避初始值、退避上限和抖动。默认值为 `case_concurrency=4`、`max_in_flight=4`、`max_retries=2`、`backoff_initial_ms=500`、`backoff_max_ms=30000`、`jitter_ratio=0.2`；`requests_per_minute` 优先使用 Provider 声明值，未知时采用 60。Worker 以 Provider 连接为粒度实施令牌桶和并发信号量；429、显式 retry-after、网络暂态错误按策略指数退避，参数错误、认证错误和能力不支持不重试。每次等待、重试和最终限流都写入事件。

- cancel_run：请求取消，记录实际停止结果。
- retry_run：创建新的运行尝试，保留来源关系。
- rescore_run：基于已收集证据重新评分，不重新调用模型。
- replay_run：使用录制的模型/工具响应重放；不支持的执行器明确声明限制。

## 12. API、CLI 与 Web

### API

公开版本从 `/api/v1` 开始，资源包括 providers、models、datasets、scenarios、agents、skills、harnesses、runs、reports、health。

主要操作包括 Provider 测试与模型同步，资源版本管理，Run 创建/取消/重试/重评分/回放，以及事件、工件和分数读取。

创建 Run 时同步完成 Scenario、Dataset、ModelProfile、参数和执行器能力校验；校验通过后以 HTTP 202 返回并持久化为 `queued`，再由 Worker 执行 `preparing` 及后续状态。校验失败返回 HTTP 422 和结构化错误，不创建可执行 Run。

实时单向事件使用 SSE。运行中的 Harness 消息通过 `POST /runs/{id}/messages` 进入 Worker 命令队列；需要持续双向 RPC 的 Codex app-server 仍由 Worker 维护 stdio 通道，Web 只通过 API 收发平台消息和 approval 决策。

### CLI

```text
motte provider add|list|test|sync-models
motte model list|show|refresh|override
motte dataset create|validate|version
motte scenario validate|run|list|show
motte agent list|inspect
motte skill list|validate|test
motte harness list|probe
motte run start|status|events|cancel|retry|rescore|replay
motte report show|compare|export
motte doctor
```

命令支持 table、json、jsonl 输出。本地执行与 `--server` 模式共享契约。

### Web

页面包括 Providers、Models、Datasets、Scenarios、Agents、Skills、Harnesses、Runs、Trace、Compare、Reports。

模型页面分为能力、限制、推理与采样三部分，显示字段来源与更新时间。Scenario 编辑器提供 schema 和能力预校验。Trace 页面展示模型调用、工具调用、沙箱命令、文件差异与证据。首版 UI 语言为简体中文，协议字段、CLI JSON 和错误码保持英文稳定标识。

不设计用户登录与 RBAC。认证秘密使用环境变量或本机 keyring，Web 仅显示 credential reference。单机网络暴露与本地访问保护在专门的安全设计中定义。

## 13. Monorepo 结构

```text
MoTTEavl/
├─ apps/
│  ├─ api/
│  ├─ worker/
│  └─ web/
├─ packages/
│  ├─ contracts/
│  ├─ sdk-python/
│  ├─ cli/
│  ├─ provider-runtime/
│  ├─ agent-runtime/
│  ├─ harness-runtime/
│  ├─ skill-runtime/
│  ├─ sandbox/
│  ├─ evaluators/
│  ├─ trace/
│  └─ storage/
├─ bridges/pi/
├─ migrations/
├─ scenarios/
├─ skills/
├─ harnesses/
├─ tests/
│  ├─ contract/
│  ├─ provider/
│  ├─ runtime/
│  ├─ storage/
│  ├─ trace/
│  ├─ sandbox/
│  ├─ harness/
│  ├─ evaluators/
│  ├─ api/
│  ├─ cli/
│  ├─ integration/
│  └─ fixtures/
├─ docs/
├─ infra/
├─ pyproject.toml
├─ pnpm-workspace.yaml
└─ README.md
```

contracts 不依赖 Provider SDK、Agent 框架或数据库。具体存储、追踪、模型和沙箱实现依赖 contracts；执行器和评分器依赖这些接口；应用层组装实现。Web 使用生成的 TypeScript contract，Pi bridge 使用独立的版本化 JSONL 协议。

## 14. 版本与兼容性

- Python 包、TypeScript bridge、CLI 使用 SemVer。
- API 从 v1 开始，破坏性变更通过新版本与迁移说明交付。
- Event、Scenario 和 Bridge 有独立 schema_version。
- 生成 OpenAPI、JSON Schema 和 TypeScript 类型，并在 CI 检查破坏性变更。
- 数据库使用显式迁移；升级前备份，回退通过匹配的应用版本和备份恢复。
- Agent、Skill、Evaluator、Harness 和数据集显式版本化。
- 适配器维护版本兼容范围；未知或不兼容版本先探测验证，不伪装成成功执行。
- CLI 命令提供弃用周期和迁移说明。

## 15. 完整开发路线

| 阶段 | 交付范围 | 验收结果 |
| --- | --- | --- |
| 1 基础契约与工程底座 | 工作区、公共协议、错误/取消/幂等、存储迁移、API/CLI/Web 调用链、CI | 两个入口可创建并追踪完整 Run，结果可保存和查看 |
| 2 Provider Runtime | 三类原生协议、兼容厂商 profile、流式/工具/usage/错误、录制回放 | 同一 Scenario 跨 Provider 运行，保留实际协议差异 |
| 3 Pi Agent Runtime | Node bridge、工具与 Skill 注入、预算、Docker 执行、轨迹 | Pi 完成受控任务，行为与工件可以独立评分 |
| 4 Skill 评测 | manifest、fixture、版本/权限、注入测试、成对比较 | Skill 可跨模型/Agent 比较，结论具有可复核证据 |
| 5 Claude/Codex Harness | stream-json、app-server、exec、进程/会话/取消/产物 | Pi、Claude、Codex 可以在同任务条件下比较 |
| 6 高级评测 | Inspect、确定性/轨迹/judge、校准、重复运行、统计、门禁、离线重评分 | 已保存的运行可更换评分器重新评估 |
| 7 稳定发布 | 完整 Web/CLI、文档、性能/并发/恢复、依赖安全、发布/回退 | 单机产品具有可验证的安装、升级、运行和恢复流程 |

阶段与实施任务采用“先水平底座、后垂直交付”的混合顺序：

| 可发布边界 | 对应实施任务 | 交付内容 |
| --- | --- | --- |
| 基础运行切片 | 1–5 | 契约、存储、Trace、直接 Run Executor、最小 API/CLI、录制 Provider |
| Provider 评测版本 | 6 | Chat/Responses/Anthropic/兼容入口和真实 Provider smoke test |
| Agent 评测版本 | 7–8 | BuiltinReAct、Pi bridge、Skill、Docker 工具闭环 |
| Harness 评测版本 | 9 | Claude/Codex 双向 Harness 与 Inspect 适配 |
| 评测与报告版本 | 10–11 | Evaluator、统计、回归门禁、完整 API/CLI/Web |
| 稳定发布版本 | 12 | 安装、升级、备份、恢复、性能、依赖安全和发布产物 |

基础运行切片在 Task 5 结束时必须能通过两个入口创建并追踪一个录制 Provider 的完整 Run；后续任务在该切片上垂直扩展，不等到全计划末尾才出现第一条端到端路径。

## 16. 文档维护约定

- 本文是已经讨论确认的设计基线，后续确认的设计同步更新文档。
- 每次变更记录日期、决策内容和影响，不依靠聊天历史推断当前方案。
- 跨模块的重要取舍另存 ADR，并由本文引用。
- `docs/superpowers/specs/` 和 `docs/superpowers/plans/` 保存流程产物；`docs/README.md` 作为长期文档入口，链接到当前 spec、plan 和 ADR，避免复制正文产生漂移。
- 尚未详细讨论的安全、测试、可观测性和发布机制另行成文，完成设计后再编写可执行实现计划。
- 设计文档只记录决定和约束；实现状态通过开发计划和验证报告管理。

## 17. 官方参考资料

访问与讨论日期：2026-09-14。模型、参数和 CLI 版本持续变化，具体能力必须以固定版本的 profile 和实际验证为准。

- [OpenAI 模型目录](https://developers.openai.com/api/docs/models)
- [OpenAI Chat API](https://developers.openai.com/api/reference/resources/chat)
- [OpenAI Responses API](https://platform.openai.com/docs/api-reference/responses)
- [Anthropic 模型 API](https://platform.claude.com/docs/en/api/models)
- [Anthropic 模型概览](https://platform.claude.com/docs/en/models/overview)
- [Anthropic Messages API](https://platform.claude.com/docs/en/api/messages/create)
- [GLM 模型概览](https://docs.bigmodel.cn/cn/guide/start/model-overview)
- [Kimi 参数参考](https://platform.kimi.com/docs/api/models-overview)
- [Kimi Chat API](https://platform.kimi.com/docs/api/chat)
- [DeepSeek 模型信息](https://api-docs.deepseek.com/quick_start/pricing)
- [Pi 仓库](https://github.com/badlogic/pi-mono)
- [Pi SDK](https://pi.dev/docs/latest/sdk)
- [Claude Code CLI](https://code.claude.com/docs/en/cli-usage)
- [Claude Code headless](https://code.claude.com/docs/en/headless)
- [Codex app-server](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md)
- [Inspect Tasks](https://inspect.aisi.org.uk/tasks.html)
- [Inspect Sandboxing](https://inspect.aisi.org.uk/sandboxing.html)
- [OpenTelemetry 语义约定](https://opentelemetry.io/docs/concepts/semantic-conventions/)

## 18. 决策记录

| 日期 | 决策 |
| --- | --- |
| 2026-09-14 | 当前仓库从零设计，其他项目仅供参考 |
| 2026-09-14 | Web + CLI，Python 核心与 TypeScript Web/bridge |
| 2026-09-14 | 单机、单用户，不考虑多用户和多租户 |
| 2026-09-14 | 云端 API 优先，支持 Chat、Responses、Anthropic 与兼容厂商 |
| 2026-09-14 | 自有公共 IR，不以第三方框架数据模型作为公共协议 |
| 2026-09-14 | Docker 沙箱、PostgreSQL/Redis、本地工件与可选 MinIO |
| 2026-09-14 | 自有 Agent 基线；Pi 为首个外部基础 Agent 适配器 |
| 2026-09-14 | Claude CLI、Codex CLI 为首批 Harness；Inspect 后续接入 |
| 2026-09-14 | 模型级配置工具、模态、token 限制、推理与参数约束 |
| 2026-09-14 | 按长期稳定产品完整规划；后续确认内容持续文档化 |
| 2026-09-14 | Linux/WSL2 一等公民；Windows Docker Desktop 支持并记录能力降级 |
| 2026-09-14 | DatasetVersion 规范磁盘格式为 JSONL，每行一个带唯一 case_id 的 Case |
| 2026-09-14 | PriceTable 独立版本化并作为成本计算来源 |
| 2026-09-14 | Run 创建同步校验，成功后 HTTP 202 持久化为 queued |
| 2026-09-14 | Harness 双向消息经 Worker 命令队列和 `/runs/{id}/messages`，SSE 负责回传 |
| 2026-09-14 | 运行并发、限流、重试、证据保留和脱敏规则写入 ADR |
