# Provider / Agent / Harness / Bridge 兼容矩阵

所有 adapter 遵循统一协议：Provider 侧 `ModelRequest` → envelope（content/usage/metering/cost/canonical/error），Agent 侧 `complete(ModelRequest)`，Harness 侧 `ProcessRunner` + JSONL parser（parser version 随结果记录）。不支持的参数在付费调用之前失败（构造级 + 请求级 + API 创建级三层 strict 预检）。

## Provider

| Adapter | 状态 | 传输 | strict 预检 | 计量 | 价格/成本 | canonical 脱敏 |
|---|---|---|---|---|---|---|
| `openai_compatible` | ✅ 本地可用 | 自有 HTTPTransport（keyword timeout/429/暂态网络退避/错误分类） | ✅ | ✅ latency/attempts/retry_count | ✅ 版本化快照，未知为 null | ✅ |
| `replay` | ✅ 确定性回放 | 无网络 | — | — | — | — |
| `openai_chat` | ✅ 响应归一化（被 openai_compatible 复用） | 同上 | ✅ | ✅ | ✅ | ✅ |
| `openai_responses` | 🚧 openai_chat 别名 shim，真适配器待接入 | — | — | — | — | — |
| `anthropic_messages` | 🚧 openai_chat 别名 shim，真适配器待接入 | — | — | — | — | — |

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
