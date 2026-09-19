# Provider / Agent / Harness / Bridge 兼容矩阵

所有 adapter 遵循统一协议：Provider 侧 `ModelRequest` → envelope（content/usage/metering/cost/canonical/error），Agent 侧 `complete(ModelRequest)`，Harness 侧 `ProcessRunner` + JSONL parser（parser version 随结果记录）。不支持的参数在付费调用之前失败（构造级 + 请求级 + API 创建级三层 strict 预检）。

## 资源关系与注册表（2026-09-15 起）

- **ProviderConnection**（`providers` 资源）：怎么连——`kind`（协议）、`base_url`、`credentials`（凭据 profile 名，缺省与 name 相同）、传输参数（`timeout`/`max_retries`/`backoff_initial_ms`/`backoff_max_ms`）。不再携带 model/price_table（旧 payload 兼容读取）。
- **ModelProfile**（`models` 资源）：调什么，包括 `provider`（连接名）、`model`（API 模型字符串，缺省 = id）、身份策略/别名、采样参数和能力档案。新记录为 `draft`，只允许草稿 PUT；`POST /api/v1/models/{id}/publish` 固定 generation 与 profile hash，只有 `published` 可创建正式 Run；DELETE 转为 `deprecated`。资源键允许路径分隔符。`POST /api/v1/models/{id}/test` 可显式 smoke-test 草稿并做一次受限真实调用，报告脱敏。
- **PriceTable**（`price_tables` 资源，`(model_id, version)` 版本化）：多少钱——Run 创建期解析：manifest 显式 `price_table_version` > 连接 legacy payload > 该模型最新版本（自然排序）。
- Run manifest 引用解析（API 与 CLI 共用 `motte_sdk.resolve`）：`manifest.model` 引用已发布 ModelProfile 时按其 `provider` 取连接；`manifest.provider` 也可以显式给连接名（须与档案一致，否则 422 `MODEL_PROVIDER_CONFLICT`）；inline dict 仅作 legacy 兼容。ResolvedManifest v2 固定 ExecutionBackend、evaluation/scorer、Provider adapter 实现版本，以及 ModelProfile/ProviderConnection generation、hash 和版本化资源快照。
- **适配器注册表**（`motte_provider.registry`）：新增 kind 只需 register 一个 `AdapterSpec`（validate/build/default_key_env/smoke_supported/connection_required_fields）；Worker 分发、API 预检、CLI live-smoke 的 kind 列表均派生自注册表。Web 端 kind 目录 `GET /api/v1/provider_kinds` 同样派生自注册表（replay 等内部 kind 不暴露），附带中文说明与官方默认端点（`anthropic_messages` → `https://api.anthropic.com/v1`、`openai_responses` → `https://api.openai.com/v1`）。
- **凭据**：本地凭据文件 `~/.motte/credentials.toml`（0600，`MOTTE_CREDENTIALS_PATH` 可覆盖；CLI `credentials set/list/remove` 与 Web API 管理——`PUT /api/v1/credentials/{profile}` 写入即弃、响应只回掩码，`GET /api/v1/credentials` 掩码列表）。解析优先级：显式传参 > 凭据文件 profile > 环境变量（`api_key_env`，回退兼容）。密钥本体绝不入库/入 trace。
- 采样参数合并优先级：请求级 > manifest 级 > ModelProfile 档案默认值；全程经 registry strict 校验。

## GSM8K-20 benchmark (offline implementation)

`benchmark import` reads local official-format JSONL only; `benchmark run` and ordinary API/CLI run creation share immutable scenario/dataset/provider preparation. Dataset、Scenario 与 PriceTable 的每个版本都是 insert-only；冲突写入或删除已发布版本返回 HTTP 409。The preset pins first-20 selection, source/provenance hashes, prompt/scorer versions, `max_output_tokens=1024`, and `max_retries=0` (not a monetary cap). Provider-facing cases contain prompts only; gold stays in the evaluation snapshot.

Benchmark runs use strict final-line Decimal scoring and a selected-case denominator. Failed runs retain partial scores, failed-call evidence and not-attempted rows; terminal benchmark rescore is offline. Generic raw-equality/replay behavior remains unchanged. Existing HTTP adapters share the prompt-only projection; synthetic fake-HTTP Worker tests verify the complete path, not official dataset authenticity or live model quality. See [GSM8K operator guide](../operations/gsm8k-smoke.md) for error policy, costs and restart limitations.

## Execution Backend（M1 起）

| Backend | 状态 | 说明 |
|---|---|---|
| `builtin-agent@1` | ✅ 已接线（`safe_to_repeat=false`） | 原生 Agent 文件任务：native-tool / legacy-json 双显式模式；native 模式要求已发布模型 `supports_tools=true`，否则创建期 422（零模型调用），不自动降级。模型请求按已发布 ModelProfile 快照构造（不再硬编码模型名）。操作见 [native-agent.md](../operations/native-agent.md)。 |
| `direct-llm@1` | ✅ 可用 | 直连单轮评测 |
| `replay@1` | ✅ 可用（safe_to_repeat） | 确定性回放 |
| `external-benchmark@1` | ✅ job 模式可用（M2-T07 起）；adapter 未注册时分派层 ADAPTER_UNKNOWN 拒绝 | `ceval-opencompass@1`（C-Eval，进程生命周期+迁移 Parser）；真实 OpenCompass 环境接入 not_run（见 operations/ceval.md） |

## Provider

| Adapter | 状态 | 传输 | strict 预检 | 计量 | 价格/成本 | canonical 脱敏 |
|---|---|---|---|---|---|---|
| `openai_compatible` | ✅ 本地可用 | 自有 HTTPTransport（keyword timeout/429/暂态网络退避/错误分类） | ✅ | ✅ latency/attempts/retry_count | ✅ 版本化快照，未知为 null | ✅ |
| `replay` | ✅ 确定性回放 | 无网络 | — | — | — | — |
| `openai_chat` | ✅ 响应归一化（被 openai_compatible 复用） | 同上 | ✅ | ✅ | ✅ | ✅ |
| `openai_responses` | ✅ 本地可用（离线 fixture 验证；live smoke 待操作者执行） | 同上（Bearer） | ✅ 参数表无 seed/stop | ✅ + response_id/usage_details | ✅ | ✅ |
| `anthropic_messages` | ✅ 本地可用（离线 fixture 验证；live smoke 待操作者执行） | 同上（x-api-key + anthropic-version，529 可重试） | ✅ 参数表无 seed | ✅ + response_id/usage_details | ✅ | ✅ |

### 归一化约定（2026-09-16 起，三个 HTTP 适配器统一）

- **finish_reason**：`end_turn`/`stop_sequence`→`stop`；`max_tokens`/`max_output_tokens`（Responses `incomplete_details.reason`）→`length`；`tool_use`/`function_call`→`tool_calls`；未知值原样透传（保留证据）。
- **usage**：canonical `prompt_tokens`/`completion_tokens`/`total_tokens`；协议扩展进 `usage_details`（`reasoning_tokens`、`cached_tokens`、`cache_read_input_tokens`、`cache_creation_input_tokens`，兼容 Anthropic `cache_creation` 新旧形状）。
- **tool_calls**：canonical `[{id, name, arguments(JSON 字符串)}]`；请求侧 canonical 工具定义为 OpenAI chat 形状（`{type: function, function: {name, description, parameters}}`），各适配器自行转换（Anthropic `{name, description, input_schema}`、Responses 扁平 function 形状）；工具回合历史（assistant `tool_calls` / tool 消息）双向映射。
- **错误映射**：HTTP 状态码分类之外，尽力解析错误体 `error.type`（OpenAI/Anthropic 通行形状）精化 `auth`/`rate_limit`/`server`/`timeout` 分类；Anthropic 529（overloaded）可重试。
- **公共流程**：`BaseHTTPProvider` 持有 complete 全流程与 envelope（计量/成本快照/脱敏/ProviderCallError 证据），适配器只实现请求构造与响应归一化。
- **模型身份**：envelope 分别保存 `requested_model`、`reported_model`、`resolved_model_identity`、原始 evidence、策略和结论。策略为 `report_only` / `require_reported` / `require_match`，alias 只来自版本化 ModelProfile，不做通用字符串裁剪。
- **尚未接入**：流式（SSE）、GET probe（如 `/models`）、真实端点 live smoke 记录。

## Agent

| 运行时 | 状态 | 协议 | 验证 |
|---|---|---|---|
| `builtin-react` | 协议实现；Run backend 未接通 | 文本 JSON 动作协议（tool/final），observation 回灌，步数预算，全程事件 | 离线 runtime 测试；API `execution_ready=false` |
| `pi` | 协议桩 v0.1.0 / v1；执行不可用 | 严格 JSONL probe；默认 prompt 返回 `PI_BACKEND_UNAVAILABLE`，不 echo、不执行输入 | Node 自测 + Python malformed/timeout/process-tree 测试；API `execution_ready=false` |

## Harness

| Harness | 状态 | probe | 传输 | 验证 |
|---|---|---|---|---|
| `claude-cli` | 本机 probe；Run backend 未接通 | `claude --version` + 安装检测（路径/来源/版本） | 预留 CLI 通道 | 目录报告 `protocol_ready` 与 `execution_ready=false` |
| `codex-cli` | 本机 probe；Run backend 未接通 | `codex --version` + 安装检测 | 预留 CLI/app-server 通道 | 目录报告 `protocol_ready` 与 `execution_ready=false` |
| `inspect` | dry-run 占位 | — | — | Inspect Task/Solver/Scorer 映射待接入 |

## Sandbox

| 能力 | 状态 |
|---|---|
| Docker 执行（hardened 容器） | ✅ 离线契约测试 + live 冒烟（`MOTTE_SANDBOX_LIVE=1`） |
| 默认无网络 / 策略分离 / 资源限制 / 恒 cleanup | ✅ |
| 磁盘配额 | ⚠️ 经 tmpfs(/tmp) 实现，根文件系统配额依赖存储驱动 |

## 工具链版本（兼容下限）

| 工具 | 版本 | 锁定位置 |
|---|---|---|
| Python | 3.12 | `.python-version` / CI |
| uv | >=0.11.6,<0.13 | `pyproject.toml [tool.uv]` |
| Node | 24 | `.nvmrc` / `.node-version` |
| pnpm | 9.15.0 | `package.json packageManager` |
| TypeScript（web） | 5.9（openapi-typescript 尚不支持 TS7） | `apps/web/package.json` |
| pi-bridge | 0.1.0 / 协议 v1 | `bridges/pi/package.json` |
| 迁移 | alembic 1.20 / 当前 head `0006_external_jobs` | `alembic.ini` / `migrations/versions/` |

更新本矩阵的时机：新增/变更 Provider、Agent、Harness、bridge 协议或工具链版本时，随同一提交更新。
