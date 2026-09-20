# Pi Runtime 操作手册（pi-agent@1）

上游：`@mariozechner/pi-agent-core@0.73.1`（MIT，Node ≥20）+
`@mariozechner/pi-ai@0.73.1`（faux scripted model / streamSimple 注入点）。
安装：`pnpm --dir bridges/pi install`（平台不自动安装/升级）。协议：v2
（`motte_agent/pi.py` 客户端严格校验：身份/序列/字段 allowlist/行与总量
有界/进程树清理）。

## 分层就绪

```bash
uv run python -m motte_cli runtime readiness pi-agent
```

- installed：node ≥20 且 bridges/pi 内包版本 == 0.73.1；
- protocol_ready：bridge probe 返回 v2（`PiAgentRuntime().probe()`）；
- execution_ready：真实 scripted 工具任务 + 取消 + 错误证据
  （`tests/protocol/test_pi_real_adapter.py` / `tests/integration/test_pi_run_backend.py`）。
  不由 --version/安装推导。

## 离线执行（受控 scripted model）

manifest 形态（agent-tasks 场景）：

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

- 每 CaseAttempt 独立 session/workspace（`var/pi-workspaces/<run>/<case>`）；
- 工具边界在 bridge 侧强制（tools.mjs：相对路径 only、symlink 拒绝、配额）；
- 事件 `pi_*` 经 RunService 持久 trace（source_seq/身份/coverage）；
- usage 诚实保持未上报（scripted 无真实计量；不填 0）。

## 取消与恢复

- 执行中取消：`POST /runs/{id}/cancel` → watcher 线程 interrupt bridge →
  `interrupted` → `finished(cancelled)`；
- Worker 崩溃：恢复只观察（session.py `recover_session`）；结果不可证 →
  needs_review；显式 retry 新建子 Run，不自动重放。

## Live 验收（待授权）

范围：真实模型经平台/运行器 Provider 驱动同一文件任务；预算上限、
凭据引用（bridge env 注入通道）、取消与费用观察。执行前须：
固定模型与调用上限、费用边界、工作目录、凭据 profile 名、清理范围。
未授权时不运行；状态保持 pending。

## 回退

`eb.unregister_backend("pi-agent", "1")`（或下线 runtime_versions 资源）→
新 Run 拒绝；历史 Run/事件/Artifact/评分继续可读；`var/pi-workspaces`
存量目录先确认进程停止再清理，不触碰操作者 home/config/auth。
