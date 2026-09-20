# M3 Review Round 2 修复记录（2026-09-20）

> 对应报告：[M3 实现 Review — Round 2](M3-review-round2-2026-09-20.md)（13 项：P1 ×7、P2 ×6）。
> 本文件记录**逐项的失败反例 → 修复 → 同一反例复验**，以及仍无法在本机闭环的部分。
> 阶段级结论与 not_run/blocked 清单见 [M3 验证记录](M3.md) 第 7 节。

修复分支：`codex/m3-harbor-terminal-bench`（基线 `046fa5f`）。
"复现"在临时目录/临时库/隔离 API 中执行；"实跑"表示真实固定 Harbor 0.23.0 与
真实 Docker（Linux/arm64，Docker 27.4.0）。**全程零付费模型调用。**

## 摘要

| 编号 | 结论 | 复验证据 |
|---|---|---|
| M3-R2-01 | 已修复（两条取消路径） | `tests/integration/test_harbor_round2_service.py`（3 个取消用例） |
| M3-R2-02 | 已修复 | 同上 `::test_retry_refreezes_child_trial_and_job_identity` |
| M3-R2-03 | 已修复 | 同上（跨 Run 存在 / 不存在 / 计划不符三个用例） |
| M3-R2-04 | 已修复 | `tests/benchmarks/test_harbor_task_compose_policy.py`（间接资源用例） |
| M3-R2-05 | 已修复 | 同上（环境透传）+ `tests/runtime/test_runner_env_boundary.py` + `tests/integration/test_harbor_compose_env_boundary.py`（真实 compose 语义与容器 `Config.Env`） |
| M3-R2-06 | 已修复 | `tests/api/test_terminalbench_round2_fixes.py`（创建 + 预检 + 冻结配置）、Web `R2-06` 用例 |
| M3-R2-07 | 已修复 | 同上（文本脱敏下载、二进制默认拒绝、note 脱敏） |
| M3-R2-08 | 已修复 | `tests/integration/test_harbor_container_ownership.py`（四类证据）+ 真实 Docker 冒名容器用例 |
| M3-R2-09 | 已修复 | `tests/integration/test_harbor_round2_service.py::test_one_invalid_payload_does_not_lose_its_siblings` |
| M3-R2-10 | 已修复 | `tests/evaluators/test_terminalbench_fingerprint.py`（禁止 / 允许两个用例） |
| M3-R2-11 | 已修复 | `tests/benchmarks/test_harbor_agent_mapping.py` + `tests/integration/test_harbor_agent_native.py::test_real_claude_code_execution_options_are_delivered`（真实 Harbor 下发参数） |
| M3-R2-12 | 已修复 | `tests/api/test_terminalbench_round2_fixes.py`（类型校验先于 repository 访问） |
| M3-R2-13 | 已修复 | `tests/sdk/test_terminalbench_content_read.py`（中文 / emoji / 非法 UTF-8 / note 脱敏） |

---

## P1

### M3-R2-01 — 取消在导入前终态化，已完成 Trial 仍丢失

**失败反例（修复前实测）**：`job_entry` 返回前落取消请求，outcome 里明确有 4 条
完成结果，最终 `cancelled/0 Trial/0 scores`；把 `service.py`/`benchmark_plugins.py`/
`trials.py`/`pg_audit_store.py` 暂存回旧版后，本文件的 3 个取消用例全部红
（`assert 0 == 4`）。

**根因（三层）**：
1. `_honor_cancellation` 在导入 outcome **之前**直接返回，采集结果被丢弃；
2. Trial 计划直到结果导入才创建，取消时补处置的 repository 是空的；
3. 取消先行写入的 synthetic 终态会占住 `put_result`，让随后到达的真实冻结结果
   被判冲突而无法导入。

**修复**：
- 执行开始前 `_ensure_trial_plans(run)` 先把冻结计划幂等落库（计划冲突在启动前
  显式失败）；
- 顺序改为"中断 → 冻结并导入已确定结果 → 为未确定单元补处置 → 终态化"：
  导入在 `_honor_cancellation` 之前完成；
- 存储层区分**平台占位**与**真实证据**：带 `synthesized_by` 的占位可以被真实
  冻结结果替换（返回 `replaced_placeholder` 并留替换审计事件
  `trial_placeholders_replaced`），真实证据之间仍然严格冲突、永不覆盖（三后端
  同语义，Memory/SQLite 在 `trials.py`，PG 在 `pg_audit_store.py`）；
- 若取消已由并发实例终态化（`already_terminal`）：仍导入冻结结果、不改写终态、
  不改写任务级快照，并追加一个可审计的 `terminal-import` pass +
  `external_job_results_after_terminal` 事件；
- 被拒绝的计划单元补 `indeterminate` 占位（`synthesized_by`），不写入被拒 payload。

**同一反例复验**（`tests/integration/test_harbor_round2_service.py`）：
- `test_cancellation_keeps_frozen_results_that_arrived_before_the_import`（延迟取消）：
  `cancelled` + 4 个计划单元 + 4 条真实结果（无占位）+ 结论里 4 条分数；
- `test_immediate_cancellation_placeholders_are_replaced_by_real_evidence`
  （同进程 interrupter 立刻终态化）：4 个占位全部被真实证据替换、替换事件有计数、
  `terminal-import` pass 出现；
- `test_cancellation_marks_only_units_without_results`（部分完成）：2 条真实结果 +
  2 个 `cancelled` 占位、分母 4、有效 2、覆盖 0.5；
- `test_cross_process_cancel_request_is_consumed_after_import`：另一实例只落请求，
  Worker 导入后终态化，冻结结果不被占位顶掉。

**边界（如实标注）**：终态不复活、不自动重跑；终态之后落库的证据只通过
`terminal-import` pass 与事件可见，不改变 Run 状态。

### M3-R2-02 — retry 复制父 Run 的 Trial 身份，新 Run 没有自己的结果

**失败反例（修复前实测）**：retry 后子 Run 的冻结 `task_manifest.trials[*].run_id`
仍全是父 ID，父子 Trial ID 集合**不**相交为假（`assert (... and False)`）；
用真实 runner 执行子 Run 后 `completed` 但 0 Trial / 0 score。

**修复**：`RunService.retry` 先冻结子 Run 身份，再调用套件的重新冻结实现
（`motte_sdk.terminalbench.refreeze_for_run`）重新派生 TrialPlan、原生 Job 身份
（`job_name`、`config_hash` 随之重算）与所有引用（`runner_config.plan.trials`、
`runner_config.trials`、`task_manifest.trials`）；任务内容、Profile、`task_files`
与实验条件保持不变，`parent_run_id` 保留。非 Trial 形态（M2 套件）原样返回。

**同一反例复验**：`test_retry_refreezes_child_trial_and_job_identity`：父子 Trial ID
不相交、子计划 `run_id` 全部是子 Run、`job_name` 已更换；子 Run 走完公共执行后
4 个计划单元全部有结果、4 条分数；父 Run 的 Trial 记录仍是"无结果"（未被改写）。

### M3-R2-03 — Trial 未核对当前冻结计划，可写入另一个 Run

**失败反例（修复前实测）**：review Run 的 outcome 使用 victim Run 的 Trial ID
（task_key/repeat_index 正确、不带 run_id）→ 修复前 Run `completed`、0 score、
victim 的 Trial 被写成 `succeeded`；换成不存在的 ID 同样被静默忽略后假完成。

**修复**：
- 行映射阶段：带 `trial_id` 但**不在本 Run 冻结计划**里的行 → 隔离为
  `EXTERNAL_TRIAL_FOREIGN`；payload 自称的 `task_key`/`repeat_index` 与冻结计划
  不一致 → `EXTERNAL_TRIAL_IDENTITY_MISMATCH`；
- 导入阶段（`import_terminal_bench_trials`）：目标 Trial 必须属于本 Run 的冻结
  计划，否则记 `TRIAL_NOT_IN_FROZEN_PLAN` 拒绝；`put_result` 返回
  `unknown_trial` 等未预期状态一律记 `TRIAL_RESULT_NOT_STORED`，绝不静默忽略。

**同一反例复验**：`test_trial_from_another_run_is_quarantined_not_written`
（Run 显式 `failed`、victim 记录逐字不变、自己的计划单元仍无结果）、
`test_unknown_trial_id_is_quarantined_and_run_is_not_falsely_completed`、
`test_payload_disagreeing_with_the_frozen_plan_is_quarantined`。

### M3-R2-04 — Compose 间接挂载与插值路径仍绕过预检

**失败反例（修复前实测）**：命名 volume 的 `driver_opts: {type: none, o: bind,
device: /}`、顶层 `secrets.file=/etc/shadow`、`${HOST_ROOT:-/}:/host`、
`external: true`、未声明的命名 volume —— 修复前公共预检全部 `allowed=True`、
空原因码。

**修复**：`harbor/compose.py` 解析**有效配置**：顶层 `volumes`/`secrets`/`configs`
定义展开；命名 volume 必须能解析（否则 `TASK_COMPOSE_UNRESOLVED_RESOURCE`），
`driver_opts.device` 按宿主 bind 走与直接 bind 相同的禁用路径检查，
`external: true`/非本地 driver → `TASK_COMPOSE_EXTERNAL_RESOURCE`，
`secrets`/`configs` 的 `file:` 源检查禁用路径与"任务目录之外"
（`TASK_COMPOSE_EXTERNAL_SECRET_FILE`）；插值按最坏情形求值
（`${VAR:-default}` 用默认值，`${VAR}`/`$VAR`/`${VAR?err}` 无法确定 →
`TASK_COMPOSE_UNRESOLVED_INTERPOLATION`）。新码都有中文说明。

**同一反例复验**：`tests/benchmarks/test_harbor_task_compose_policy.py` 的间接资源
用例（多形态）全绿，断言 `allowed=False` + 对应原因码 + `model_calls==0` /
`task_starts==0`。

**行为变化（fail-closed 收紧，需知会）**：任务 compose 中在宿主路径位置插值未
设置变量、引用未声明资源、`external: true`、非本地 driver、`volumes_from` 现在
被拒绝（此前放行）。这是刻意的：无法证明安全就不放行。

### M3-R2-05 — Compose 环境透传形式可把 Runner 凭据带进任务

**失败反例（修复前实测）**：`environment: [ANTHROPIC_API_KEY]`、`environment:
{ANTHROPIC_API_KEY: null}`、`secrets.<name>.environment`、`MOTTE_PG_DSN` 全部
通过公共预检；同时实测子进程确实继承了完整宿主环境（`MOTTE_HOST_TOKEN` 出现在
Runner 子进程环境里）。

**修复**：
- compose `environment` 三种形态统一解析为 `(name, value|None)`，凭据形状变量名
  一律 `TASK_COMPOSE_CREDENTIAL_PASSTHROUGH` + `TASK_CONTAINER_HOST_ENV_EXPOSED`
  拒绝；
- 新增 `motte_benchmark.env_boundary`：平台→Runner 的环境变成**显式白名单**
  （运行时非敏感变量 + 冻结 Profile 声明的 `env:NAME` 引用 + 平台注入身份变量），
  `ProcessJobAdapter.start` 按白名单裁剪并在 `env_boundary` 记录逐名来源；出现
  "形状像凭据但无法追溯到声明"的变量则拒绝启动
  （`RUNNER_ENV_BOUNDARY_VIOLATION`）；
- Runner 在 Harbor 运行前把实际会传给 `docker compose` 的变量名写成
  `harbor/env-boundary.json`（只记名字），适配器冻结为证据。

**"哪些变量会进任务容器"的明确答案**：容器只拿到任务 compose 自己声明的东西；
`docker compose` 可见的名字集合是白名单 + 身份注入 + wrapper shell 变量
（实测记录在案），**Agent 凭据（如 `ANTHROPIC_API_KEY`）仍在 Runner 环境里**——
Harbor 的 Agent 从进程环境读取它，删掉会破坏真实 Agent；因此策略是"任务不能
命名/透传它"，而不是"从 Runner 删除它"。

**同一反例复验**：`tests/runtime/test_runner_env_boundary.py`（白名单裁剪 + 未声明
凭据拒绝）、`tests/integration/test_harbor_compose_env_boundary.py`（真实
`docker compose config` 语义 + 真实容器 `Config.Env` 里合成宿主秘密不存在）、
compose 透传用例全绿。

### M3-R2-06 — 真实 Agent API 创建没有必需凭据引用入口

**失败反例（修复前实测）**：带合法 `{"provider": {"ref": "env:ANTHROPIC_API_KEY"}}`
的创建请求得到 422 `REQUEST_FIELD_UNKNOWN`（DTO 不接受 `credentials`）；preflight
只能给 `AGENT_CREDENTIAL_REF_MISSING`，没有传引用的入口。

**修复**：创建 DTO 接受 `credentials`（仅 `{"name": {"ref": "env:VAR"}}`；明文/
`{"value": …}`/非 `env:` 前缀分别 422 且不回显值）；preflight 新增
`credential_refs=name=env:VAR` 查询参数，与创建共用同一解析函数；凭据子树豁免
通用键名扫描（它有自己的更严校验）。Web 侧新增 Agent 选择（oracle / claude-code）、
显式版本输入、可重复的"名称 → 环境变量名"引用行，本地校验缺失项与 `env:` 前缀，
且**没有任何凭据值输入框**；请求体经导出的 `buildTerminalBenchRunRequest` 生成，
`credentials` 只以引用形式出现。

**同一反例复验**：`tests/api/test_terminalbench_round2_fixes.py`（创建成功 → 冻结
Profile 只有引用、`model` 是 `{provider, model}`、原生 `agents[0]` 的
`name/model_name/kwargs.version` 与 Agent 表一致；缺引用仍 422）、Web `R2-06`
7 个用例（含"只提交引用""粘贴哨兵被本地拒绝且不回显""缺版本/模型/凭据不发请求"）。

**边界**：真实模型调用仍需单独授权——本轮只验证"创建 → 冻结 Profile → 原生配置"
贯通，不触发模型。

### M3-R2-07 — 新原始字节下载路由绕过文本脱敏

**失败反例（修复前实测）**：同一含合成哨兵的文本工件，普通内容接口脱敏，而
`/bytes` 返回 200 + 原始哨兵；二进制工件也直接返回原始字节。

**修复**：`/bytes` 对文本工件返回**脱敏后的字节**（`X-Motte-Artifact-Source-Sha256`
保留冻结身份、`X-Motte-Artifact-Redacted: true`）；二进制工件默认拒绝
（422 `ARTIFACT_RAW_EXPORT_DISABLED`，消息说明原因与受控导出方式），只有操作员
显式设置 `MOTTE_ALLOW_RAW_ARTIFACT_EXPORT=1` 才返回原始字节（响应头如实标
`false`）；内容/详情响应的 `note` 等展示字段也过同一脱敏（SDK 侧 `note` 同样
脱敏，CLI/Web 继承），身份与 hash 逐字保留。

**同一反例复验**：`tests/api/test_terminalbench_round2_fixes.py`（文本下载无哨兵、
二进制默认拒绝且响应体无哨兵、note 脱敏、身份字段不变）。

**边界**：平台没有认证/角色体系，所以"受控导出"= "默认拒绝 + 操作员显式开关"，
不是按调用者身份的访问控制；按调用者授权读取属于后续阶段。

---

## P2

### M3-R2-08 — 容器核验忽略 owner token 和 Run，可能停止错误资源
失败反例（假 Docker client）：一个 `motte.job` 匹配但 `motte.run`/`motte.owner`
都不同的容器，`stop_owned(remove=True)` 仍停止/删除它并报 `clean`。
修复：`ContainerOwnership.classify()` 对完整持久化证据（job 标签 + `motte.run` +
`motte.owner` 摘要 + compose project）给出 `match`/`conflict`/`unrelated`；明确
冲突（含"外来 job 标签 + 我们的 project"）一律拒绝操作并报
`HARBOR_CONTAINER_OWNERSHIP_CONFLICT` + 逐证据明细，`list_owned`/`state` 在存在
冲突时返回 `unknown` 而不是 `clean`，project fallback 不能覆盖 owner 不一致。
复验：四类假 client 用例 + 真实受控 Docker 的**冒名容器**用例（冒名容器保留、
清理报 `unknown`、只停真正拥有的容器）。

### M3-R2-09 — 一个非法 Trial 中止整批导入，合法兄弟结果丢失
失败反例（修复前实测）：2×2 全部成功，只让排序第二条 payload 带错 run_id →
只有 1 条分数，Trial 状态 `succeeded/pending/pending/pending`。
修复：导入逐条隔离（`put_result` 异常只影响该条，记录
`TRIAL_RESULT_REJECTED`），返回完整 `accepted`/`invalid` 清单；任务级派生行只
消费**已接受**的记录；被拒绝单元补 `indeterminate` 占位；Run 保持 `failed`
（部分成功不掩盖拒绝）。
复验：`test_one_invalid_payload_does_not_lose_its_siblings`（3 条合法兄弟结果落库、
1 个占位、2 个 responded 任务行、Run `failed`）。

### M3-R2-10 — 比较政策禁止模型变化时，TB 模型差异仍被忽略
失败反例（修复前实测）：两份正式 manifest 只有模型不同（`claude-sonnet-4-5` vs
`claude-opus-4-1`），`allowed_factors` 为空 → `eligible=true`、`quality=true`，
只有 `COST_UNKNOWN`。
修复：新增 `_model_identity()` 按套件实际冻结位置取值（TB 在
`external_benchmark.profile.model`，旧套件在 manifest 顶层）；未允许时
`FACTOR_NOT_ALLOWED:model` 阻断，允许时记入 `allowed_differences`
（`ALLOWED_FACTOR:model`）。
复验：`test_model_change_is_blocked_when_the_policy_forbids_it`（阻断）、
`test_allowed_model_change_is_recorded`（允许且留痕）。

### M3-R2-11 — Agent 接受的部分模型/工具/预算配置没有执行映射
失败反例（修复前实测）：带 `reasoning_level`/`model.parameters`/`tools`/`limits`
的 Profile 与去掉这些设置的 Profile 生成的原生 job 配置**完全相同**（kwargs 只有
`version`）。
修复：新增 `execution_option_plan()`（创建与预检共用的同一判定）：
`model.reasoning_level → reasoning_effort`，`model.parameters`/`limits →
max_turns`/`max_budget_usd`/`max_thinking_tokens`/`fallback_model`，
`tools → allowed_tools`/`disallowed_tools`/`permission_mode`（固定版
`ClaudeCodeOptions` 的字段，由 Harbor 编译成 CLI/env）；其余非空配置一律
`HARBOR_PROFILE_UNSUPPORTED_FIELD`/`HARBOR_PROFILE_INVALID_FIELD_VALUE` 拒绝，
oracle 带任何执行配置也拒绝；映射冻结在 `execution_options.mapped`，且
`agent_config_hash` 覆盖 `limits` 与映射后的选项。
复验：离线映射/拒绝/预检用例 + 真实 Harbor 校验（`ClaudeCodeOptions`
`extra=forbid` 接受 kwargs；校验摘要显示 `--effort medium`、`--max-turns 7`、
`--max-budget-usd 1.25`、`--allowedTools Read,Bash`、
`MAX_THINKING_TOKENS=2048`；并断言"配置不同 → 下发参数不同"）。

### M3-R2-12 — dataset_revision 类型校验晚于数据库访问，仍可返回 500
失败反例（修复前实测）：`dataset_revision: {"bad":"type"}` / `["bad"]` →
`sqlite3.ProgrammingError` → 500。
修复：新增 `_tb_run_fields()` 在任何 repository 访问**之前**完成完整 DTO 校验
（未知键、`dataset_revision` 字符串或 null、`model` 字符串、`n_trials` 整数、
`task_keys` 字符串数组、`timeouts`/`resources`/`credentials` 对象），创建与预检
都先过这一步；preflight 查询参数类型错误同样 4xx。
复验：参数化用例（每个字段的非法类型 → 422 + 结构化 code + `GET /api/v1/runs`
总数为 0），并有正向对照（合法 revision / null / 空串仍能创建）。

### M3-R2-13 — UTF-8 截断切开字符，合法长日志整体变成"二进制"
失败反例（修复前实测）：`("a" * 65535 + "中" + "z").encode()` 配正确 sha →
`verified=true`、`truncated=true`、`encoding=binary`、`text=None`（emoji 同理）。
修复：先按预算截断再回退到**完整字符边界**（增量解码丢弃结尾不完整序列），
保留 `truncated` 标记；真正非法的 UTF-8 仍如实标 `binary` 并给原因；`note`
与 `text` 同一脱敏边界。
复验：`tests/sdk/test_terminalbench_content_read.py`（中文边界、emoji 完整/被切两种
情形、非法 UTF-8、note 脱敏、缺失内容诚实）6 项。

---

## 本轮同时发现并修掉/记录的事项

| 编号 | 事项 | 处理 |
|---|---|---|
| N2-01 | 修复 R2-01 时先按"任务级 Case 行"重算，破坏了 M2 非 Trial 形态的既有映射（11 个 M2 用例红） | 仅在 Trial 形态下重算派生行，M2 语义逐字保持 |
| N2-02 | 测试脚手架按父 Run 的 `run_id` 构造输入却创建另一个 Run 的 Run（计划身份与 Run 身份不一致），导致首轮 6 个用例误红 | 测试改为"按目标 Run 身份构造输入"，与 R01 的身份规则一致 |
| N2-03 | Docker 地址池耗尽（历史 `delete=False` 运行留下 30 个网络）导致真实链路偶发失败 | 清理残留网络；真实链路用例自行回收本 Job 创建的 project 网络（属测试卫生，不是产品缺陷） |
| N2-04 | 真实链路测试此前跑的是上一次部署的桥接快照 | 真实链路与原生校验改为 `PYTHONPATH` 指向仓库源码；`install-harbor` 仍是部署路径（操作指南已注明改动后需重新部署） |

## 未闭环项（不做"看起来完成"的表述）

1. **真实模型/Agent live 验收：blocked。** R2-06 只把"创建 → 冻结 Profile → 原生
   配置"打通并离线验证；真实调用需要单独授权（任务清单、数据集 revision、模型、
   Profile、重复数、预算、期限、命令）。
2. **上游完整任务集（89 / 10 题）：not_run。**
3. **linux/amd64 宿主、受限网络策略、Verifier 可见性变体：not_run。**
4. **无认证/角色体系**：证据导出的"受控"目前等于"默认拒绝 + 操作员开关"；
   按调用者身份授权的读取不在本轮范围（审查报告也明确不扩展认证体系）。
5. **PG 结论来自一次性容器**（已删除）；默认运行中 PG 门控用例 skip，skip 不计通过。
