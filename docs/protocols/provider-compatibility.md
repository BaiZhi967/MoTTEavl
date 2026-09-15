# Provider / Agent / Harness / Bridge 兼容矩阵

所有 adapter 遵循统一协议：Provider 侧 `ModelRequest` → envelope（content/usage/metering/cost/canonical/error），Agent 侧 `complete(ModelRequest)`，Harness 侧 `ProcessRunner` + JSONL parser（parser version 随结果记录）。不支持的参数在付费调用之前失败（构造级 + 请求级 + API 创建级三层 strict 预检）。

## 资源关系与注册表（2026-09-15 起）

- **ProviderConnection**（`providers` 资源）：怎么连——`kind`（协议）、`base_url`、`credentials`（凭据 profile 名，缺省与 name 相同）、传输参数（`timeout`/`max_retries`/`backoff_initial_ms`/`backoff_max_ms`）。不再携带 model/price_table（旧 payload 兼容读取）。
- **ModelProfile**（`models` 资源）：调什么——`provider`（连接名，创建时校验存在）、`model`（API 模型字符串，缺省 = id）、采样参数默认值（`parameters`）与能力档案。
- **PriceTable**（`price_tables` 资源，`(model_id, version)` 版本化）：多少钱——Run 创建期解析：manifest 显式 `price_table_version` > 连接 legacy payload > 该模型最新版本（自然排序）。
- Run manifest 引用解析（API 与 CLI 共用 `motte_sdk.resolve`）：`manifest.model` 引用 ModelProfile 时按其 `provider` 取连接；`manifest.provider` 也可以显式给连接名（须与档案一致，否则 422 `MODEL_PROVIDER_CONFLICT`）；inline dict 为 legacy 全量配置。展开结果以快照形式写入 run。
- **适配器注册表**（`motte_provider.registry`）：新增 kind 只需 register 一个 `AdapterSpec`（validate/build/default_key_env/smoke_supported/connection_required_fields）；Worker 分发、API 预检、CLI live-smoke 的 kind 列表均派生自注册表。
- **凭据**：本地凭据文件 `~/.motte/credentials.toml`（0600，`MOTTE_CREDENTIALS_PATH` 可覆盖；CLI `credentials set/list/remove` 管理）。解析优先级：显式传参 > 凭据文件 profile > 环境变量（`api_key_env`，回退兼容）。密钥本体绝不入库/入 trace。
- 采样参数合并优先级：请求级 > manifest 级 > ModelProfile 档案默认值；全程经 registry strict 校验。

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
- **尚未接入**：流式（SSE）、GET probe（如 `/models`）、真实端点 live smoke 记录。

## Agent

| 运行时 | 状态 | 协议 | 验证 |
|---|---|---|---|
| `builtin-react` | ✅ 完整 | 文本 JSON 动作协议（tool/final），observation 回灌，步数预算，全程事件 | 离线测试（fake complete） |
| `pi` | ⚠️ bridge v0.1.0 / 协议 v1 | NDJSON 双向（probe→version，prompt→started/output/finished，malformed→error），当前为确定性 echo | Node 自测 + Python 驱动测试；真实 Pi runtime 待接入 |

## Harness

| Harness | 状态 | probe | 传输 | 验证 |
|---|---|---|---|---|
| `claude-cli` | ⚠️ CLI 通道（Linux/WSL2） | `claude --version` + 安装检测（路径/来源/版本） | `claude -p <prompt> --output-format json` | Linux fake binary 全流程；Windows 进程适配仍需补齐 |
| `codex-cli` | ⚠️ CLI 通道（Linux/WSL2） | `codex --version` + 安装检测 | `codex exec --json` | Linux fake binary 全流程；app-server JSON-RPC 与 Windows 进程适配待接入 |
| `inspect` | 🚧 dry-run 占位 | — | — | Inspect Task/Solver/Scorer 映射待接入 |

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
| uv | >=0.11.6,<0.12 | `pyproject.toml [tool.uv]` |
| Node | 24 | `.nvmrc` / `.node-version` |
| pnpm | 9.15.0 | `package.json packageManager` |
| TypeScript（web） | 5.9（openapi-typescript 尚不支持 TS7） | `apps/web/package.json` |
| pi-bridge | 0.1.0 / 协议 v1 | `bridges/pi/package.json` |
| 迁移 | alembic 1.20 / 当前 head `0001_initial` | `alembic.ini` / `migrations/versions/` |

更新本矩阵的时机：新增/变更 Provider、Agent、Harness、bridge 协议或工具链版本时，随同一提交更新。
