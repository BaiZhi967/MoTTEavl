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
  pgid / Windows Job Object）。Windows 用 CREATE_SUSPENDED 创建，绑定
  KILL_ON_JOB_CLOSE Job 后用保留的进程 HANDLE 恢复执行；绑定失败先终止
  尚未执行的目标，禁止退化为事后枚举。父进程退出后 Job 仍拥有后代；
- 管道排空与回调屏障均有时限；截止后停止接收新回调并冻结字节缓冲。
  Python 无法强制取消已经进入的任意回调：回调宽限期后仍未返回会标记
  `callback_unconfirmed` / truncated。CLI 将 stderr 回调限制为内存排队，
  待 wait 返回后才在调用线程持久事件。清理不能确认时 Run 进入
  needs_review，保留 workspace，不生成完整 Observation、不删除现场；
- 取消：运行中的 Run 取消请求由执行器轮询探测，命中即打断受控进程并等
  清理结束后冻结证据（R04）——迟到副作用不落地；
- 预算：`runtime_profile.budgets.total_timeout` / `idle_timeout` 编译进
  监督上限（R07），只接受有限正数；未知预算键及费用/工具等当前无法
  强制的预算拒绝。环境 `MOTTE_CLI_TOTAL_TIMEOUT` / `MOTTE_CLI_IDLE_TIMEOUT`
  同样拒绝空串、NaN、无穷、零和负值；无法兑现的 credential_refs 在 build 门具名拒绝
  （`RUNTIME_CREDENTIALS_UNRESOLVED`）。

## 证据语义

- 终态由原生 terminal × process outcome 合成（M4 review R06）：非零退出
  绝不产生 final_answer；进程 timeout → wall_time；截断/无效 UTF-8 →
  invalid_state + coverage partial；
- **原始 stdout/stderr 冻结**为 harness-raw 证据产物（redacted），parser
  结果带 `raw_ref` 指针（M4 review R14）。原始证据路径为
  `<backend>/<run>/<case>/evidence/{stdout,stderr}/<sha256>`，任务产物为
  `<backend>/<run>/<case>/files/<relative-path>/<sha256>`。类型分区与内容
  哈希阻止 raw-stdout/raw-stderr 文件或后续采集覆盖既有证据；
- Session 记录（真实 CaseAttempt ID、start_token、PID/创建身份、cleanup、
  终态）落盘 `var/runtime-sessions/`，可用 `MOTTE_RUNTIME_SESSION_ROOT`
  指定共享目录。revision 比较与替换持有 OS 文件锁，损坏记录和过期 revision
  均拒绝写入。单独调用 harness、未绑定 RunService 时 attempt_id 为 null，
  不伪造平台 attempt；
- Worker 在恢复/领取任务前调用 `recover_runtime_sessions()`，将 session、
  PID 身份与清理证据加入 needs_review 的 error.details.runtime_sessions。
  同一 Run 有 session 证据时禁止按 safe_to_repeat 自动重放未决 attempt。
  prepared token 也不能证明未启动（存在 spawn 后未记 PID 的崩溃窗口），
  保守标记 needs_review；既有进程只观察，PID 身份不符不杀进程；
- usage/cost 只取原生回报：claude result.usage + total_cost_usd；codex
  turn.completed.usage（费用恒 unknown，不填 0）；模型不在 codex 流中
  回报 → observed_model 为空，不用请求值冒充；
- 工具轨迹完整度（M4 review R13）：Codex command_execution、file_change、
  mcp_tool_call、web_search 按原生 item ID 归并 started/updated/completed，
  工具名称与原生参数保留；未知 item、缺 ID、未完成工具或缺终态会降低
  tool_trajectory。完整轨迹表示可见工具调用完整，不证明任意 shell/MCP
  的全部文件副作用可见；**claude 单对象结果没有工具轨迹**（`absent`）→ coverage 不
  冒充 complete，"禁止工具调用"类否定断言按 insufficient 处理，文件类
  断言仍按 workspace 快照证据评分；
- 同输出不同 parser 版本：历史 Artifact/评分不重写（ScoringPass 不可变）；
  codex parser 现为 `codex-jsonl-v2`（官方顶层 `type` 事件形态，R02）。

## 受控认证与有效配置

每 Case 使用独立 native home，不继承操作者的登录文件。Claude profile 可声明
`credential_refs: ["ANTHROPIC_API_KEY"]`，Codex batch 可声明
`credential_refs: ["CODEX_API_KEY"]`；值只从 Worker 对应环境变量注入子进程。
未知引用在静态预检拒绝，声明但缺值在启动前报 `RUNTIME_CREDENTIALS_UNRESOLVED`。
不声明引用时不注入密钥；这可用于离线假进程测试，不能推断真实模型认证就绪。
app-server 的同名引用通过私有 account/login/start 传递，见后文。

Claude 2.1.278 以 `--bare --setting-sources '' --strict-mcp-config
--disable-slash-commands` 运行；Codex 0.155.1 batch 使用 `--ignore-user-config
--ignore-rules --ephemeral`。这些 flags 已用对应固定版本的原生 `--help` 核对，
不是基于主机 PATH 上不同版本推测。Codex API key 通道参见
[固定版本 exec 源码](https://github.com/openai/codex/blob/rust-v0.155.1/codex-rs/exec/src/lib.rs)。

session 与结果的 config_snapshot 记录受控目录、binary hash、请求配置 hash、
Git revision/patch/status hash、规则/Skill/MCP 配置候选文件 hash、env 名称与凭据存在性。
不记录秘密值；已知凭据回显在输出、错误、产物字节/文件名和快照键中脱敏。
脱敏产物保留 redacted 标记，内容评分为证据不足，不将替换后的字节当作原始内容。

发现候选不代表原生实际加载。系统/托管配置、真实加载规则/Skill/MCP/plugin
和间接启动的二进制仍可能未知，因此 reproducibility 为 partial。
Runner 设置 `MOTTE_RUNTIME_REPRODUCIBILITY=strict` 会在启动前拒绝这类运行；
不能声称隔离环境、秘密脱敏或 CLI 自身沙箱等同平台强制工具政策。
扫描有文件/目录/字节/时限上限；Git 外部 diff/textconv/fsmonitor 禁用，发现
任何 filter 配置或无法完整读取配置时不执行工作树 diff/status，保留 hash unknown。

## Live 验收（待授权）

每后端：同一受控文件任务（产物断言）+ 一次执行中取消 + 一次非零退出；
记录二进制版本、模型、调用上限、预计费用边界、工作目录、凭据 profile、
清理范围。未授权时保持 pending，不以离线证据冒充。

## 回退

注销对应 backend（或下线 runtime 版本资源）→ 新 Run 拒绝、历史与其余
后端不受影响（`tests/integration/test_external_runtime_slice.py` 覆盖）。

## 监督器双向协议接口

`SupervisedProcess.send_line(str)` 在 start 后、终态前发送一行 UTF-8，
自动补换行；拒绝内嵌 CR/LF，含末尾换行的字节数不得超过 max_line_bytes。
并发发送串行化，写入最长等待 idle_timeout 与剩余 total_timeout 的较小值；
peer 不消费输入时终止受控进程并抛 STDIN_TIMEOUT，不永久阻塞 Worker。
读写错误与生命周期错误使用 SupervisedProcessError.code 返回；调用方仍须
wait 收集终态、冻结输出并清理所有权。stdout/stderr 回调必须短且无阻塞操作，
持久化与外部副作用放在调用线程并检查会话生命周期。


## Codex app-server 交互通道（独立 RuntimeVersion 2）

`codex-app-server@2` 使用固定 `@openai/codex@0.155.1` 的 stdio RPC。
旧 `@1` 资源保留历史内容且没有消费者；新建运行应选 `@2`。
原生设置支持 `model`、真实可执行文件 `binary`、`sandbox`（默认 read-only，
可设 workspace-write）和 `approval_policy`（默认 on-request，可设 untrusted）。
`.cmd`/`.bat` 不隐式包装 shell。启动先核对实际 --version，初始化后再核对
上游 thread/start 返回的模型、目录、审批人/策略与沙箱，漂移不发送初始任务。

每个真实 CaseAttempt 创建独立进程、workspace、临时 HOME/CODEX_HOME、
平台 session 与原生 thread/turn。完整的有效原生配置证据由 native_config
保存候选配置哈希、二进制哈希、Git 状态哈希与未知加载项；reproducibility
保持 partial，严格策略拒绝执行。模型字段区分 effective_model 与未观测的
observed_model，不把选择的模型冒充实际遥测。

仅显式 `credential_refs: ["CODEX_API_KEY"]` 从 Worker 环境解析该引用。
值通过固定协议 `account/login/start {type: apiKey, apiKey: ...}` 的私有请求
交付，不置于 argv、子进程环境或持久快照；不读取宿主登录文件、不发起 OAuth。
无引用允许离线假进程测试，但不会继承宿主认证。原生程序可能在其受控临时
home 内保存认证材料；该目录不属于任务产物，清理确认前不销毁它。

`GET /api/v1/runs/{run_id}/sessions` 返回当前 session/control_revision 与
待批动作摘要、approval_id、request_hash、期限。POST `/messages` 必须绑定
case_id、session_id、expected_session_revision（即 control_revision）和
客户端生成的 dedupe_key。user_message 另含 content；approve/reject 的 payload
必须为 `{approval_id, request_hash}`；interrupt 不携带内容。提交者记为明确的
anonymous-local，端点没有声称新增身份认证系统。

HTTP 202 只表示 queued 持久收据。GET `/commands` 公开 authoritative 状态：
queued、delivered、acknowledged、rejected、expired、delivery_unknown/failed。
相同幂等键且相同意图返回已有 ID/期限/状态；改动意图返回 409。数据库事务在
Run、session、命令间检查控制版本、截止时刻与所有权，再占用单个原生请求。
Memory/SQLite/PostgreSQL 使用相同逻辑；SQLite BEGIN IMMEDIATE，PG 行锁。
提交、派发和状态更新的审计与写入同事务。0010 迁移遇到历史重复去重键会失败，
不会删除旧命令。不存在跨数据库和 stdin 的原子提交；该窗口明确记未知且不重发。

消息使用 turn/steer 的 expectedTurnId；取消使用 turn/interrupt。批准和拒绝
回答原始服务端请求的精确类型 ID，仅允许 accept/decline，不发送“批准”提示文本。
审批绑定原始参数、文件提议、原生 thread/turn/item、平台 session/start_token
及过期时间。请求内容变化、旧 revision、错误哈希或已清除请求均不授权。
serverRequest/resolved 的 ack_evidence.kind=request_resolved 只证明请求已
解决/清除，decision_outcome=not_proven；不代表已执行，更不代表工具成功。
终态/清理竞态保持 delivery_unknown。消息和取消的确认分别是 message_accepted
和 interrupt_requested。无 ack、断连、重启都不自动重放。

Run 取消或 interrupt 命令先留下取消意图，调用原生中断并在宽限后收回进程树。
清理前不冻结工作区。不能证明清理时保留现场并标 needs_review；迟到通知仅审计。
Worker 在独占恢复锁下将 delivered 转未知、旧 queued 转拒绝/过期、session
转 abandoned。新的显式 retry 创建新的 Run/session，旧命令不能漂移到新会话。

每个人工命令保留 attempted/effective/possible 干预归类。ScoringPass.summary
冻结 interventions，重评分继承它，报告证据哈希包含该冻结摘要。比较默认拒绝
改变或未知的人工干预；只有显式允许 intervention 因子才放开并记录理由。
离线 schema、真实假子进程和 API/SQLite/Worker 测试证明通道接线；真实付费任务、
真实身份认证及真实模型取消仍需独立 live 验收，不能由这些测试推断 execution_ready。
