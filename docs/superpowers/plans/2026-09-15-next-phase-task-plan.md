# 下一阶段修复计划：从可运行原型到可复核执行链

日期：2026-09-15。当前基线：`b09953c`，后续修复在当前工作树继续推进。

本文替代早期“从基础骨架开始”的计划。当前代码已经具备 SQLite/replay/API/Web 基础闭环，因此剩余工作以真实行为、跨平台能力和生产可启动性为中心。

## 当前基线

已验证：

- SQLite Run 状态机、持久化 Trace、replay、retry、rescore 和 Worker loop。
- PostgreSQL Alembic schema、RunStore 和条件式集成测试。
- OpenAI-compatible envelope、计量、价格快照和 strict 参数预检。
- Docker Sandbox 的离线策略测试与显式 live 测试入口。
- Web 构建、Vitest、TypeScript strict 类型检查和 OpenAPI 生成。

仍未达到稳定交付标准：

- OpenAI Responses 与 Anthropic Messages 仍是 shim。
- Pi bridge 仍是 echo 实现，尚未调用真实 Pi Agent。
- Codex app-server JSON-RPC、Harness send/approval 尚未完成。
- Inspect、复杂 Evaluator、统计置信区间和回归门禁仍是骨架。
- Windows CLI Harness 需要完整进程树/PTY 测试。
- Compose 已有统一镜像定义，但需要真实 Docker build/up 验证。

## P0：先建立可证明的 Direct LLM 链路

### P0-1 Provider transport

- 已完成 keyword timeout 调用约定。
- 已支持 408/429/5xx、Retry-After 秒数或 HTTP date、有限指数退避；认证、参数和能力错误不重试。
- 已增加本地 HTTP server 与 fake opener 测试。
- 保存脱敏 canonical request/response、usage、latency、attempts 和 error class。

验收：本地 HTTP fixture 能产生完整 envelope；live smoke 仍需显式命令才能触发真实费用。

### P0-2 Run 创建校验

- 已完成 direct-llm 缺 Provider 的结构化 422、inline/provider-name 校验、未知 Scenario 拒绝和明文凭据拒绝。
- 已完成缺 Provider 时 Worker 安全失败，禁止伪造成功结果。
- `POST /runs/{id}/messages` 已有终态检查和事件入口；持久化命令队列仍待实现。
- 仍需创建 Run 前解析 Scenario、DatasetVersion、ModelProfile、Agent、Skill、Sandbox 和 limits。
- API、CLI、Worker 共用同一个 validation service。

验收：无 Provider、未知 Scenario、缺 Case、能力不足、明文凭据均在付费调用前失败。

### P0-3 运行镜像

- 使用根目录 Dockerfile 构建统一 `motteavl:local` 镜像。
- Compose 的 migration/API/Worker 使用该镜像，不通过源码 volume 覆盖已安装环境。
- 在 Linux/WSL2 上执行真实 `docker compose build`、`up`、migration、health 和 replay Run。

验收：新 clone 不需要手工 pip install，Compose 能启动并完成 replay Run。

## P1：真实适配器与跨平台运行

1. OpenAI Responses：独立 request/response/stream normalization，保留 response id、工具调用和 reasoning usage。
2. Anthropic Messages：独立 headers、system、content blocks、tool use/result 和 usage normalization。
3. Pi Agent：Node bridge 接入固定版本的 Pi packages，协议层保持稳定，真实模型调用由 ProviderAdapter 注入。
4. Claude：`stream-json` 事件解析、approval、send、session artifact 和取消。
5. Codex：优先 app-server stdio JSON-RPC，保留 `exec --json` batch fallback。
6. Windows：Job Object/进程树终止、ConPTY/pipe 能力探针和平台矩阵测试。

每个适配器必须有 probe、版本记录、脱敏 raw evidence、取消/超时、错误映射和 replay fixture。

## P1：Evaluator 与回归门禁

- JSON Schema、exact、regex、exit code、file diff、trajectory、tool sequence evaluator。
- LLM judge 使用固定 judge profile、prompt/rubric 版本和独立 evidence。
- 聚合支持 missing/unsupported policy、quantile、置信区间、pass@k、成本和 latency。
- RegressionGate 比较 pinned baseline，输出结构化报告和非零 CLI exit code。
- InspectHarness 映射 Dataset/Agent/Scorer/Sandbox 并导入 TraceEvent。

## P2：生产质量

- Run ID 使用数据库 sequence/UUID，消除并发 `max(id)+1` 竞态。
- 资源版本默认不可变；更新必须显式创建新版本。
- 各 workspace package 声明真实运行时依赖，可独立构建 wheel。
- `mypy` 从 contracts 扩展到 storage/provider/sdk/runtime/api。
- API 请求/响应改用 Pydantic schema，OpenAPI 成为真实 contract。
- 端到端 Docker、PostgreSQL、Redis、Web 和 Harness 测试纳入 CI；付费 live smoke 保持手工入口。

## 验收顺序

1. Direct replay + SQLite：API/CLI/Worker/SSE/Report 全链路。
2. Direct OpenAI-compatible：本地 HTTP fixture 与显式 live smoke。
3. Compose + PostgreSQL：空库 migration 后完整 Run。
4. BuiltinReAct + Docker ToolRegistry。
5. Pi、Claude、Codex 真实适配器。
6. Evaluator、Inspect、回归门禁和 Web 类型化 API。

每项完成后更新 `docs/PROGRESS.md`、`docs/protocols/provider-compatibility.md` 和本计划，并运行对应 focused tests。
