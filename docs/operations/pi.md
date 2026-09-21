# Pi Runtime 操作手册（pi-agent@1）

上游：`@mariozechner/pi-agent-core@0.73.1`（MIT，Node ≥20）+
`@mariozechner/pi-ai@0.73.1`（faux scripted model / 真实 provider 注入点）+
`openai`（pi-ai 的 openai-completions provider 依赖）。安装：
`pnpm --dir bridges/pi install`（平台不自动安装/升级）。协议：v2
（`motte_agent/pi.py` 客户端严格校验：身份/序列/字段 allowlist/行与总量
有界/进程树清理）。

## 分层就绪

```bash
uv run python -m motte_cli runtime readiness pi-agent
```

- installed：node ≥20 且 bridges/pi 内包版本 == 0.73.1；
- protocol_ready：bridge probe 返回 v2（`PiAgentRuntime().probe()`；
  readiness 端点自动执行该零成本探测）；
- execution_ready：真实 scripted 工具任务 + 取消 + 错误证据
  （`tests/protocol/test_pi_real_adapter.py` / `tests/integration/test_pi_run_backend.py`）。
  不由 --version/安装推导。

## 模型传输（两种，显式二选一）

### scripted（离线，无网络）

```json
{
  "runtime": "pi-agent@1",
  "runtime_profile": {
    "runtime": "pi-agent@1",
    "native_settings": {
      "model": "scripted-1",
      "script": [[{"type": "toolCall", "name": "write_file",
                    "arguments": {"path": "answer.txt", "content": "..."}}],
                 [{"type": "text", "text": "done"}]],
      "max_steps": 8
    }
  }
}
```

usage 诚实保持未上报（scripted 无真实计量；不填 0）。

### http（真实模型；离线可用本地 fake HTTP 验证）

```json
{
  "runtime": "pi-agent@1",
  "runtime_profile": {
    "runtime": "pi-agent@1",
    "native_settings": {
      "model": "gpt-4o-mini",
      "provider": {
        "api": "openai-completions",
        "base_url": "https://api.example.com/v1",
        "api_key_env": "MOTTE_PI_MODEL_KEY"
      }
    }
  }
}
```

- `api` 走 pi-ai 真实 provider 分发（openai-completions / openai-responses /
  anthropic-messages / google-generative-ai / mistral-conversations）；
- **凭据只经环境名下发**（`api_key_env`）：平台 worker 进程环境持有该
  变量，bridge 读环境后经 `streamSimple` 的 `options.apiKey` 注入；秘密
  原文永不跨协议；
- `api_key_env` 声明的环境缺失 → init 即失败（SESSION_INIT_FAILED）；
  未声明时交给 pi-ai 的 provider 环境解析（如 provider=openai 读
  OPENAI_API_KEY）；
- `script` 与 `provider` 互斥（同时出现 → RUNTIME_MODEL_CONFIG_REQUIRED）；
- 真实流的原生计量如实上报（`usage.source=native-model`）。桥在本次运行内观察
  原生 SSE 的 usage 字段，区分明确上报的零与 SDK 默认零；多轮按模型调用累计。
  任一调用缺少原生计量时，整体计量保持未上报，不能把部分总和当完整用量。
  SDK 必需的价格字段采用零占位，仅供 SDK 处理 token，绝不当作已观测费用；
- SDK 的 `error` / `aborted` 消息分别成为失败 / 取消终态，保留错误原因与已收到文本；
- 离线验证：`tests/protocol/test_pi_http_provider.py` 用本地 fake OpenAI
  SSE 服务器走完整真实路径（认证头、工具往返、文本收口）。

## 执行边界

- 每 CaseAttempt 独立 session/workspace（默认
  `var/pi-workspaces/<run>/<case>`；profile `workspace.source=pinned-path`
  时锚定到操作者指定根，仍按 run/case 隔离）；
- 工具边界在 bridge 侧强制（tools.mjs：相对路径 only、symlink 拒绝、
  嵌套路径创建完整目标、**全部配额写盘前检查**——被拒的写不落盘）；
- 工具集合 = runtime 快照声明工具 ∩ bridge 沙箱工具（声明之外的请求在
  预检即拒绝，不静默放行）；
- 预算是**强制边界**（M4 review R08）：`budgets.max_steps` /
  `max_tool_calls` 在下一次超额动作之前阻止执行，额度内的最后回答正常完成；
  `max_tool_calls=0` 禁止首个工具调用。预算键、有限数与边界由公共校验器验证，
  未知或无法兑现的预算（如费用）明确拒绝。预算终态映射为
  `max_steps` / `max_tool_calls`（不冒充 final_answer，也不是取消）；
  `budgets.total_timeout` 是单调时钟总期限——持续产出事件也会被切断，
  终态 wall_time、证据照常冻结（R09）；
- 无法兑现的声明具名拒绝：`credential_refs` → `RUNTIME_CREDENTIALS_
  UNRESOLVED`（bridge 无凭据注入通道前不静默降级认证）；
- SDK 版本门（M4 review R18）：bridge 报告的 sdk_version 与快照 pinned
  upstream（0.73.1）不符 → `RUNTIME_VERSION_DRIFT`，session 关闭、fail
  closed；
- 事件 `pi_*` 经 RunService 持久 trace（`operation_id`、bridge 原生 `source_seq` +
  parser_version 对账，M4 review R14），调用记录也保留 operation_id；
  正常与异常路径均先关闭 bridge 并确认进程及读取线程停止，再冻结 workspace
  快照与产物，最后清理。停止无法确认时，证据标记不完整并保留工作区和残留记录。

## 取消与恢复

- 执行中取消：`POST /runs/{id}/cancel` → watcher 线程 interrupt bridge →
  `interrupted` 先于 `finished(cancelled)`；**interrupt 在运行中即被处理**
  （不排队到任务完成后，M4 review R03），后续工具步骤不再执行；
- Worker 崩溃：恢复只观察（session.py `recover_session` /
  `recover_runtime_sessions`）；结果不可证 → needs_review；PID 复用按
  命令行 + 创建时间核对，绝不误杀；显式 retry 新建子 Run，不自动重放。

## Live 验收（待授权）

范围：真实模型（http 传输）驱动同一文件任务；预算上限、取消与费用
观察。执行前须：固定模型与调用上限、费用边界、工作目录、环境名凭据
清单、清理范围。未授权时不运行；状态保持 pending。

## 回退

`eb.unregister_backend("pi-agent", "1")`（或下线 runtime_versions 资源）→
新 Run 拒绝；历史 Run/事件/Artifact/评分继续可读；`var/pi-workspaces`
存量目录先确认进程停止再清理，不触碰操作者 home/config/auth。
