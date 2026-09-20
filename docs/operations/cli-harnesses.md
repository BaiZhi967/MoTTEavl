# CLI Harness 操作手册（claude-cli@1 / codex-cli@1）

固定上游（2026-09-20 registry 核对）：`@anthropic-ai/claude-code@2.1.278`、
`@openai/codex@0.155.1`。平台不自动安装/登录/升级全局二进制。

## 版本门（spawn 前强制）

dispatch 在启动进程**之前**按实际二进制跑零费用 `--version`，与 manifest
快照的 pinned upstream 比对（M4 review R18）：

- 探测失败/输出不可解析 → `RUNTIME_BINARY_VERSION_UNKNOWN`（Run 标记
  unsupported，具名错误，不执行）；
- 版本漂移 → `RUNTIME_VERSION_DRIFT`（fail closed；对齐二进制或发布新
  runtime 版本号，不放宽矩阵）；
- 离线 fixture（`.py` binary）同样过门：fixture 需应答 `--version` 并
  报告 pinned 版本（测试 fixture 即此形态）。

## 目录与就绪

```bash
uv run python -m motte_cli runtime list          # installed/protocol/execution 分层 + 原因
uv run python -m motte_cli runtime readiness claude-cli
```

readiness 走真实零成本探测（bridge probe / `--version`，M4 review R19），
不再把"探测输入未提供"当结论。

## Batch 执行（agent-tasks 场景）

```json
{
  "runtime": "claude-cli@1",
  "runtime_profile": {
    "runtime": "claude-cli@1",
    "native_settings": {"model": "claude-sonnet-4-5", "max_turns": 8,
                         "permission_mode": "acceptEdits", "binary": "claude"}
  },
  "runtime_accept_unenforced_tools": true
}
```

```json
{
  "runtime": "codex-cli@1",
  "runtime_profile": {
    "runtime": "codex-cli@1",
    "native_settings": {"model": "gpt-5-codex", "sandbox": "workspace-write",
                        "codex_config": {"c_sandbox_mode": "workspace-write"},
                        "binary": "codex"}
  },
  "runtime_accept_unenforced_tools": true
}
```

- Claude transport：`-p <prompt> --output-format json`（+ --model/
  --max-turns/--permission-mode/--settings）；平台写入干净 settings 文件
  隔离宿主配置；
- Codex transport：`exec --json`（+ -m/-c/--sandbox/--skip-git-repo-check），
  app-server 不是 batch 的 fallback；
- `binary` 可固定绝对路径（离线 fixture/测试注入也走该通道，同样过版本门）；
- 进程经 SupervisedProcess：env allowlist、行/总量/空闲/总时长有界、
  interrupt → 宽限 → 进程树终止、**OS 级持久所有权**（POSIX session
  pgid / Windows Job Object——父进程退出后孤儿后代仍会被清理，M4 review
  R05）、进程退出后有界排空管道（慢消费者不吞尾帧，R12）；
- 取消：运行中的 Run 取消请求由执行器轮询探测，命中即打断受控进程并等
  清理结束后冻结证据（R04）——迟到副作用不落地；
- 预算：`runtime_profile.budgets.total_timeout` / `idle_timeout` 编译进
  监督上限（R07）；无法兑现的声明（credential_refs）在 build 门具名拒绝
  （`RUNTIME_CREDENTIALS_UNRESOLVED`）。

## 证据语义

- 终态由原生 terminal × process outcome 合成（M4 review R06）：非零退出
  绝不产生 final_answer；进程 timeout → wall_time；截断/无效 UTF-8 →
  invalid_state + coverage partial；
- **原始 stdout/stderr 冻结**为 harness-raw 证据产物（redacted），parser
  结果带 `raw_ref` 指针（M4 review R14）；session 记录（start_token →
  PID/创建身份 → 终态）落盘 `var/runtime-sessions/`，Worker 崩溃后用
  `recover_runtime_sessions()` 只读观察（M4 review R15）；
- usage/cost 只取原生回报：claude result.usage + total_cost_usd；codex
  turn.completed.usage（费用恒 unknown，不填 0）；模型不在 codex 流中
  回报 → observed_model 为空，不用请求值冒充；
- 工具轨迹完整度（M4 review R13）：codex 事件流完整 → `tool_trajectory:
  complete`；**claude 单对象结果没有工具轨迹**（`absent`）→ coverage 不
  冒充 complete，"禁止工具调用"类否定断言按 insufficient 处理，文件类
  断言仍按 workspace 快照证据评分；
- 同输出不同 parser 版本：历史 Artifact/评分不重写（ScoringPass 不可变）；
  codex parser 现为 `codex-jsonl-v2`（官方顶层 `type` 事件形态，R02）。

## 认证（externally-managed）

CLI 原生登录归操作者；平台不读取/提交 auth 文件内容。**当前版本没有向
CLI 进程注入凭据的受控通道**：声明了 `credential_refs` 的 profile 会在
build 门被 `RUNTIME_CREDENTIALS_UNRESOLVED` 具名拒绝（不静默降级认证），
凭据通道接线后此限制解除。

## Live 验收（待授权）

每后端：同一受控文件任务（产物断言）+ 一次执行中取消 + 一次非零退出；
记录二进制版本、模型、调用上限、预计费用边界、工作目录、凭据 profile、
清理范围。未授权时保持 pending，不以离线证据冒充。

## 回退

注销对应 backend（或下线 runtime 版本资源）→ 新 Run 拒绝、历史与其余
后端不受影响（`tests/integration/test_external_runtime_slice.py` 覆盖）。
