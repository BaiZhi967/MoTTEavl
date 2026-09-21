# Skill 运维说明（M5）

状态：T06（版本与安全导入）、T07（注入 / 有效权限 / 受控 executable fixture）与
T08（no-skill / skill-v1 / skill-v2 三臂对照）均已接通并有行为测试。判定证据是
**真实捕获的 Agent 请求**、真实执行的入口副作用，以及普通 Worker 跑出的三个普通
Run；不是计划里的字段。

## 1. 三种形态与验证范围

| kind | entrypoint | 验证范围 | 能证明什么 | 不能证明什么 |
|---|---|---|---|---|
| instruction | 不需要 | static | manifest/指令/schema/权限声明有效；渲染内容按声明顺序进入真实 Agent 请求 | 指令能完成任务 |
| instruction_with_resources | 不需要 | static | 资源 hash 与依赖 pin 固定；资源落位可按内容地址复核 | 指令能完成任务 |
| executable | **必须** | executable-fixture | 入口在受控输入下的输出、schema 合规与副作用 | 所有模型都会正确使用 |

纯指令 Skill 即使"读了一个文件"，也只是**指令**：它没有受控入口、不产生
`executable` 声明，执行侧也没有可消费的 fixture（`EXECUTABLE_SKILL_ENTRYPOINT_REQUIRED`）。
固定 Agent/模型/任务下的实际效果是第四种范围 agent-behaviour，只有真的跑了行为测试
才能这样标注（`compile_injection(..., behaviour_tested=True)`）。

## 2. 注入：从冻结声明到真实 Agent 请求

- 创建期（`motte_sdk.resolve._freeze_skill_injection`）按 `manifest.skills` 的
  `name@version` 解析**已发布**版本（draft/缺失/弃用具名拒绝），核验
  `instruction_ref` 的内容寻址字节，再调 `motte_skill.injection.compile_injection`
  并把结果冻结到 `manifest.resource_snapshots.skill_injection`：
  `refs` / `plan_hash` / `injection_digest` / `declaration`（entries 带渲染文本、
  `rendered_hash`、资源 refs、指令 token 估算）/ `executables`。客户端提交
  `resource_snapshots` 或预置 `skill_injection` 一律拒绝（`SNAPSHOT_RESERVED`）。
- 执行期（`motte_sdk.scenario_target` 的 builtin-agent 会话）用
  `plan_from_declaration` 重建计划并**重算** plan_hash、injection_digest 与每条
  `rendered_hash`；再用 `compose_agent_system_prompt` 按声明顺序把渲染文本拼进
  真实 `ModelRequest.system`。声明被改写 → `INJECTION_DECLARATION_TAMPERED`；
  声明了 skills 却拿不到冻结声明 → `SCENARIO_SKILL_INJECTION_MISSING`，绝不静默
  降级成"没有 Skill"。
- builtin-agent 的能力声明 `skill_injection=True`：Workflow 的
  `target_requirements.skill_injection` 可以要求它。
- `injection_mode=native-loader` 的 Skill **不能**由上下文注入器交付：
  `compose_agent_system_prompt` 具名拒绝（`INJECTION_ADAPTER_UNSUPPORTED`），
  因为原生加载实际送入的内容不可观测（`observability=partial`）。它需要可核验的
  原生加载清单，而不是把文本塞进 system prompt 冒充已生效。
- 顺序是身份：交换两条 Skill 的顺序会改变 `plan_hash` 与渲染段落顺序。

## 3. 有效权限与唯一执行网关

有效权限 = platform ∩ scenario ∩ target ∩ Skill 请求，deny 优先：

- **platform 是服务端硬边界**，不由调用方声明：业务工具集与模式来自冻结的
  fixture/target/workflow 快照，读路径、写路径、凭据与网络默认全关（空列表 = 全部拒绝）。
- Skill 只贡献它**显式声明**的字段；未声明即不约束——绝不会因为挂了一个 Skill 就把
  平台已授予的工具清空。一旦声明就只能收窄（工具集取交集，模式取最保守）。
- 交集结果进入**唯一执行网关** `motte_scenario.executor.FixturePortAdapter`：
  每次工具调用先调 `select_for_execution`。请求比授权更强的模式
  （`real` 在 `mock` 授权下）→ `INJECTION_TOOL_MODE_ESCALATION`；未授权工具 →
  `INJECTION_TOOL_NOT_GRANTED`；两者都不落地、不调真实 handler。
- 目标自身调用的 bridge mode 也取自有效权限（不再固定 `real`）；混合授权取最保守模式。
- 绝对路径与开放网络在契约层就不可声明（`SkillPermissions.network` 只有 `none`）。

## 4. executable fixture（受控入口）

协议（`motte_scenario.executable_fixture`）：

- 受控输入按 `input_schema` 校验后写到执行 cwd 的 `inputs.json`；
- 入口把结果 JSON 打到 stdout（最后一行）：`output` 必填，可选
  `tools_used` / `network` / `filesystem_write` 声明它用掉的权限；
- 入口文件与其余带脚本后缀的 token 必须在已发布资源里（发布期 argv 语法）。

机制：复用既有受控进程 `motte_harness.SupervisedProcess`（cwd/env/argv、
`total_timeout`、interrupt → 宽限期 → 进程树终止、residual pid 上报），
不复制进程终止实现，也不新建调度器；一个 `(run, case, attempt)` 一个受控根，
清理前核验所有权标记。

| 事实 | 结果 |
|---|---|
| 输出不合 `output_schema` / 不是 JSON / 无输出 | `EXECUTABLE_SKILL_OUTPUT_INVALID`，`passed=False` |
| 声明了有效权限之外的 `tools_used` / `network=true` / 越界写入 | `EXECUTABLE_SKILL_PERMISSION_VIOLATION`，`passed=False` |
| 受控根之外（anchor 内）出现新文件 | `EXECUTABLE_SKILL_WORKSPACE_ESCAPE`，`passed=False` |
| 期限耗尽 | `EXECUTABLE_SKILL_TIMEOUT`，`passed=False` |
| 退出码非 0 / 残留进程 | `EXECUTABLE_SKILL_EXIT_CODE` / `EXECUTABLE_SKILL_STOP_UNCONFIRMED`（`passed=None`） |
| close(interrupted=True) 或残留进程 | `status=retained`、`passed=None`：**绝不给"已执行通过"** |
| 确认停止且无残留 | `status=cleaned`，`executed`/`passed` 如实取自实际结局 |

诚实边界：本模块没有容器/OS 级文件系统隔离能力，因此"是否真的没写别处"只能证伪到
可观测范围（受控根之外的 anchor 内新增文件、以及入口自己声明的写入）。未在
`env_allowlist` 里的宿主变量不会进入子进程；网络能力平台侧恒为 `none`。

## 5. 三臂对照（T08）

- `plan_skill_ablation(base_manifest, arms, budget_policy, case_keys)` 固定除 Skill
  之外的逐字段条件（含**实际预算额度**）并生成计划 hash；`skill_snapshot` 只存在于
  **计划身份**里——`ResolvedManifest` 不接受它作为顶层键，真实冻结快照由创建期写进
  `resource_snapshots.skill_injection`。
- `run_skill_ablation(plan, base_manifest, create_run=...)` 把计划展开成三条**普通
  Run**（调用方既有的创建入口），并校验返回的 Run 真的是这条臂的冻结身份，否则
  `ABLATION_ARM_IDENTITY_MISMATCH`。没有第二套调度器。
- `bind_ablation_references(execution, run_view, report_ref)` 在 Run 执行并发布评分后
  **一次性固定**每臂的 pass / report 引用，后续报告只读这些引用，不追随 current。
- `build_ablation_report(plan, execution, evidence, comparison=...)` 输出：
  逐 Case 配对（每条臂的 status / passed / failure_class / 工具次数）、逐臂覆盖、
  指令 token overhead、成本与 delta、以及归因结论：
  - 缺结果、取消、未终止、指标不可判或 Workflow 未完成 → 覆盖不足，收益结论
    直接阻断（`gains[*].complete=False`），绝不只挑成功 Case 声称收益；
  - 成本未知保持 unknown（`per_arm[*].known=false`、`delta_vs_baseline_usd=null`），
    不补 0；指令 token 单列且 `instruction_tokens_billed=false`，不重复计费；
  - `comparison(arm_id)` 由既有比较服务提供（R4）：预算/Agent/runtime 等条件不同 →
    `blocking` 非空 → `attribution.eligible=false`。`COST_UNKNOWN` 单列为
    `cost_reasons` + `cost_comparable=false`：费用维度不可归因，但不冒充条件相同、
    也不把行为可比性一并否定。
- 已知实现细节：零人工介入的 ScoringPass 现在把 `interventions.condition_hash` 记为
  `None`（空列表的 hash 会被消费者读成"可能有人工介入"，从而**永远**阻断纯 Skill
  归因）。有真实介入时该字段与既有语义不变。

## 6. 命令

```bash
uv run pytest -q -m "not live" tests/skill/test_skill_injection_fixture.py \
    tests/integration/test_skill_ablation.py tests/skill
uv run pytest -q -m "not live" tests/integration tests/scenario
uv run pytest -q -m "not live" tests/storage/test_scenario_skill_resources.py \
    tests/storage/test_resource_store.py
uv run ruff check .
```

回归证据：

- `tests/skill/test_skill_injection_fixture.py`：渲染内容/顺序/hash 进入真实
  `ModelRequest`、私有真值不进入请求、mock 不升 real、路径与网络只能收窄、
  执行网关拒绝 real 步骤、资源落位与字节漂移、纯指令不谎称已执行、executable
  fixture 的 cwd/env/argv/期限/清理与越权失败。
- `tests/integration/test_skill_ablation.py`：公共资源 → 三条普通 Run → 普通 Worker
  → 固定 pass 引用 → 配对报告（初态一致、互不影响、成本未知、指令 token 单列、
  条件差异阻断归因、覆盖不足阻断收益）。
- `tests/skill/test_skill_versions.py`：发布边界、argv 语法、缓存隔离、持久内容存储。
- `tests/storage/test_scenario_skill_resources.py`：Memory/SQLite 两后端的资源字节与弃用语义。
