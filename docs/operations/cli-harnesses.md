# CLI Harness 操作手册（claude-cli@1 / codex-cli@1）

固定上游（2026-09-20 registry 核对）：`@anthropic-ai/claude-code@2.1.278`、
`@openai/codex@0.155.1`。版本漂移 → 分层就绪 fail closed（协议层给出
漂移原因）。平台不自动安装/登录/升级全局二进制。

## 目录与就绪

```bash
uv run python -m motte_cli runtime list          # installed/protocol/execution 分层 + 原因
uv run python -m motte_cli runtime readiness claude-cli
```

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

- Claude transport：`-p <prompt> --output-format json`（+ --model/
  --max-turns/--permission-mode/--settings）；平台写入干净 settings 文件
  隔离宿主配置；
- Codex transport：`exec --json`（+ -m/-c/--sandbox/--skip-git-repo-check），
  app-server 不是 batch 的 fallback；
- `binary` 可固定绝对路径（离线 fixture/测试注入也走该通道）；
- 进程经 SupervisedProcess（env allowlist、行/总量/空闲/总时长有界、
  interrupt → 宽限 → 进程树终止、残留检测）。

## 证据语义

- exit=0 但缺 final/终态 → insufficient（invalid_state），评分不通过；
- usage/cost 只取原生回报：claude result.usage + total_cost_usd；codex
  turn.completed.usage（费用恒 unknown，不填 0）；模型不在 codex 流中
  回报 → observed_model 为空，不用请求值冒充；
- 命令执行（codex command_execution items）映射为 tool_calls 证据；
- 同输出不同 parser 版本：历史 Artifact/评分不重写（ScoringPass 不可变）。

## 认证（externally-managed）

CLI 原生登录归操作者；平台不读取/提交 auth 文件内容。live 运行时凭据
经受控 env 通道注入（声明式 env 名单进 ConfigSnapshot；不进 manifest）。

## Live 验收（待授权）

每后端：同一受控文件任务（产物断言）+ 一次执行中取消 + 一次非零退出；
记录二进制版本、模型、调用上限、预计费用边界、工作目录、凭据 profile、
清理范围。未授权时保持 pending，不以离线证据冒充。

## 回退

注销对应 backend（或下线 runtime 版本资源）→ 新 Run 拒绝、历史与其余
后端不受影响（`tests/integration/test_external_runtime_slice.py` 覆盖）。
