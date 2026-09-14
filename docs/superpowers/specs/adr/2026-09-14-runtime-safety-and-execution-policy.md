# ADR: Runtime Safety, Evidence Retention, and Execution Scheduling

日期：2026-09-14  
状态：已确认约束

## 决策

MoTTEavl 面向单机、单用户运行，但任务执行必须按受控服务边界实现。Linux/WSL2 是一等公民；Windows 原生通过 Docker Desktop 支持，并在运行 manifest 中记录能力降级。

### 宿主能力

| 能力 | Linux/WSL2 | Windows + Docker Desktop |
| --- | --- | --- |
| rootless/非 root 容器 | rootless 优先，非 root 兜底 | 非特权容器，记录无法提供 rootless 的差异 |
| 网络隔离 | Docker network namespace，none/allowlist | Docker Desktop network policy，实际出口能力写入 manifest |
| 进程组/取消 | POSIX process group | Job Object/进程树终止，适配器提供统一结果 |
| PTY | 原生 PTY | ConPTY/管道，记录终端能力 |
| CLI Harness | 原生 stdout/stdin | PowerShell/ConPTY 适配，未知行为先 probe |

安全验收不能把 Windows 的降级能力报告成 Linux 等价能力。Provider API 的必要网络访问通过显式 allowlist，不允许沙箱任意出网。

### EvidencePolicy

```yaml
retention:
  trace_days: 90
  raw_provider_days: 30
  artifact_days: 90
  keep_failed: true
  keep_pinned: true
```

标准 Trace 和工件默认保留 90 天，原始 Provider payload 默认保留 30 天；失败 Run 保留；被 pin 的 Run 永不自动清理。清理由 Worker 执行，删除前保留 hash、大小和删除审计事件。

脱敏键清单覆盖 HTTP header、环境变量、JSON key 和 provider raw 嵌套字段：

```text
authorization, x-api-key, api-key, api_key, access-token,
refresh-token, cookie, set-cookie, proxy-authorization,
OPENAI_API_KEY, ANTHROPIC_API_KEY, MOONSHOT_API_KEY,
ZHIPUAI_API_KEY, DEEPSEEK_API_KEY, custom configured secret keys
```

脱敏结果统一为 `[REDACTED]`，secret 不得出现在 API、SSE、CLI、数据库、trace 或 artifact。

### 并发、限流与重试

每个 Scenario 可配置 `case_concurrency`、Provider `requests_per_minute`、`max_in_flight`、`max_retries`、`backoff_initial_ms`、`backoff_max_ms` 和 `jitter_ratio`。默认值为 `case_concurrency=4`、`max_in_flight=4`、`max_retries=2`、`backoff_initial_ms=500`、`backoff_max_ms=30000`、`jitter_ratio=0.2`；`requests_per_minute` 优先采用 Provider 声明值，未知时采用 60。Worker 以 ProviderConnection 为粒度维护令牌桶和并发信号量。

429、retry-after 和网络暂态错误指数退避；参数错误、认证错误、上下文超限、能力不支持和策略拒绝不重试。所有等待、重试、限流和最终错误写入 TraceEvent。默认值由实现配置固定并显示在 resolved manifest 中。

### profile_stale

排队或准备阶段发现 ModelProfile 过期、撤销或与探测结果冲突时，Run 进入 `profile_stale`，禁止模型调用。历史完成 Run 不会被标记 stale，因为它绑定原 profile 版本。刷新 profile 后通过 `retry_run` 创建新尝试；`rescore_run` 不受影响。

### Run 创建与双向 Harness

API 同步完成 Scenario、Dataset、ModelProfile、参数和执行器能力校验；成功后 HTTP 202 直接持久化为 `queued`，失败返回 422。

运行中的 Harness 消息经 `POST /api/v1/runs/{id}/messages` 进入 Worker 命令队列。Worker 将消息写入受控 stdin 或 Codex app-server JSON-RPC 通道，并发出 `user_message`、`harness_request` 和 `harness_response` 事件。SSE 只负责事件回传；已结束或不支持交互的 Run 拒绝写入。

## 影响

该决策使安全策略、跨平台差异、成本与并发边界、状态迁移和双向 Harness 都成为可测试的公共行为。任何新增运行器必须声明宿主能力、策略实现、取消方式、版本探针和事件映射。
