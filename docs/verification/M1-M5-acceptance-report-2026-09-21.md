# M1–M5 产品级验收测试报告（2026-09-21）

执行人：开发 Agent（自动化）。对象：`E:/Dev/MoTTEavl`，分支 `codex/m5-scenarios-skills-judges`，
HEAD `63b8f2f73af5122a1e820abadd087e16e3ca618f`。清单见
`docs/verification/M1-M5-acceptance-plan-2026-09-21.md`。
原始证据：`var/m5-accept/evidence/*.json`（逐条含命令、原始输出、时间）。

---

## 1. 结论摘要

**M1–M5 尚未达到可验收完成。** 三条独立原因：

| # | 结论 | 严重度 |
|---|---|---|
| 1 | **现有 SQLite 部署无法升级到本次代码**：`score_sets`/`trials`/`external_jobs` 共 9 个列缺失，且升级函数有错误短路条件。结果：**在这台机器的现有数据库上，任何 Run 都无法结算**（M1/M2/M3/M5 全线）。已给出确定性复现。 | 阻断 |
| 2 | **M5 三条核心交付没有公共入口或与运行时证据形状不兼容**：Fixture 版本无法发布（无路由/无 CLI）→ 业务状态断言主流程不可运行；Skill 版本无法发布 → 注入/三臂不可达；Judge 只认 `result.observation`，而 Scenario Run 写的是 `frozen_observation` → Judge 对 Scenario Run 一律拒收。 | 阻断 |
| 3 | **Web 的 Judge 页与场景步骤页对着不存在的端点**（`GET /api/v1/judges` 405、`GET /runs/{id}/steps` 404 等），两页永久显示「能力不可用」。 | 高 |

同时**成立**的正面项（有真实证据）：真实 Provider 调用可用；成本未知保持 unknown；
Workflow 发布/幂等/冲突与纯预检正确；**在 schema 健康的数据库上，M5 场景引擎端到端跑通**
（多轮追问 → 断言 → checkpoint → 冻结 Observation → 评分 pass）；legacy 转换只读且拒绝近似发布；
能力不足的页面/接口都给出具名阻断而不是空成功。

### 判定口径
- PASS = 亲自执行且结果符合期望；FAIL = 执行后不符合；REFUSED-OK = 期望就是具名拒绝且确实具名；
- not_run = 环境/授权缺失，**不计为通过**；
- 「PASS 但无效」= 命令成功但失败原因不是被测目标（见 A04）。

---

## 2. 环境基准（实测）

| 项 | 实测 |
|---|---|
| API / Web / Worker | 8000 健康；5173 可达；`python -m apps.worker.motte_worker` 2 个进程 |
| 已发布模型 | `deepseek-v4.1-flash`（ctx 1e6，supports_tools=true，system_messages=false）、`gpt-5.6-luna` |
| Provider / 凭据 | kind `openai_compatible`，https://6a.g-bits.com/v1，profile `6a` |
| 价格表 | `/api/v1/price_tables` → total 0（**无价格表 → 成本必须未知**） |
| scenario target | 仅 `builtin-agent`（multi_turn、tool_modes real/mock/replay/deny、skill_injection） |
| runtime 就绪 | pi-agent@1 installed/protocol_ready、**execution_ready=false**；claude-cli/codex-cli/codex-app-server 均 **protocol_ready=false** |
| `make doctor` | ok（claude 2.1.268、codex 0.148.0、pi bridge 存在） |
| 起始 Run 数 | 13（用户既有） |

---

## 3. 逐里程碑结果

### M1 原生 Agent 与通用评分闭环

| 项 | 结果 | 证据 |
|---|---|---|
| 自编文件任务数据集导入 + 分页 | PASS | `motte agent-tasks import` → `acc-file-report@1`，5 个 case，`cases_sha256`/`dataset_fingerprint` 落库；`GET /agent-tasks/cases?dataset=...` total=5 |
| 创建期预检（两模型） | PASS | `POST /agent-tasks/runs/dry-run` → 200，backend=builtin-agent，selected_cases=1 |
| 未知模型创建期拒绝（零调用） | PASS | 422 `RUN_CONFIG_INVALID: model not found` |
| 真实模型驱动 Agent 完成文件任务 | **FAIL** | 3 条 Run 均在 2s 内 failed，0 次模型调用 |
| 工具失败回灌 / 越权 / 无限循环 | **无效** | 见下 |
| 失败可定位到哪一步 | **FAIL** | UI 与 API 只显示 `'NoneType' object has no attribute 'get'` |
| 无 gold 泄漏 | not_run | 无成功 Run 可查 |

**A04「PASS 但无效」必须点出**：`Run B` 确实以 failed 结束，但失败原因不是预算强制，而是
工作区不可用——**这条不能算 M1-C（预算与终止）通过**。

真实失败链（事件流原文）：

```
seq 4 case_call_failed {"case_id":"acc-001","result":{"error":{"class":"network",
  "message":"[Errno 13] Permission denied: 'var\\agent-workspaces'"}}}
seq 6 scoring
seq 7 failed {"error":{"message":"'NoneType' object has no attribute 'get'","type":"AttributeError"}}
```

### M2 C-Eval 与正式 LLM Benchmark

| 项 | 结果 | 证据 |
|---|---|---|
| direct-llm / GSM8K 资源可见 | PASS | direct-llm 3 内置 + 2 已导入；gsm8k-test-full@2 1319 题，license=MIT，revision=b0bb162abedc |
| dry-run 预检（零调用） | PASS | 200，selected_count=2，`max_input_tokens_upper_bound=120`，`case_ids_sha256` |
| 小规模真实运行（现有库） | **FAIL** | `OperationalError: no such column: repeat_index`，3s 失败，scores=0 |
| 小规模真实运行（健康 schema） | PASS | completed，accuracy 0.5，attempted=2，judged=2，denominator=judged_cases，1 正确 |
| C-Eval 端到端 | not_run | 预检具名阻断：`DATASET_UNPREPARED`、`RUNNER_NOT_CONNECTED`、`MODEL_IDENTITY_MISSING`、`SPLIT_NOT_IN_DATASET:val`；本机无 C-Eval 数据 |
| 能力不足不冒充可运行（Web） | PASS | /ceval 显示「Catalog 状态：registered；阻塞：DATASET_UNPREPARED、RUNNER_NOT_CONNECTED、PROFILE_NOT_VALIDATED」 |

### M3 Harbor / Terminal-Bench

| 项 | 结果 | 证据 |
|---|---|---|
| 未准备即具名拒绝 | REFUSED-OK | API 与 Web 均为 `DATASET_UNPREPARED: no Terminal-Bench task set is prepared yet`；Web 显示「数据集版本 未准备」「任务数 未知」「Runner 未知」 |
| 容器依赖 | not_run | 本机**未安装 docker**；无任务集可准备，M3 live 全链路未执行 |
| Task/Trial 契约 | not_run | 无 Run 可查；`trials` 表另见缺陷 F-01 |

### M4 Pi 与 CLI Harness

| 项 | 结果 | 证据 |
|---|---|---|
| 分层就绪 | PASS | pi-agent：installed 0.73.1、protocol_ready=true、**execution_ready=false**，理由「requires a real scripted task/cancel evidence … never inferred from --version」 |
| CLI harness 就绪 | PASS（负面事实） | claude-cli 2.1.268 / codex-cli 0.148.0 / codex-app-server 0.148.0 全部 **protocol_ready=false** |
| Pi 目标 Run 具名拒绝 | REFUSED-OK | 422 `RUNTIME_NOT_FOUND: runtime not found: pi-agent@1`（注意：`/api/v1/runtimes` 里明明有该 runtime，措辞误导，真实原因是未发布 runtime profile） |
| CLI harness 真实小任务 | not_run | 平台自身 `protocol_ready=false`，且未获操作者对其个人配额的授权 |
| Inspect 只读导入 / 交互命令 | not_run | 本轮时间与额度预算内未展开；无证据即为无证据 |

### M5 场景、Skill、Judge

| 项 | 结果 | 证据 |
|---|---|---|
| Workflow 发布 / 同内容幂等 / 异内容 409 | PASS | 201 两次同 hash；改内容 → 409 `RESOURCE_CONFLICT`；带 fixture_refs 的 Workflow 也能发布 |
| 纯预检 `scenario validate` | PASS | `{"ok":true,"publishable":true,"executed":false,...}`，step_count=3，零调用 |
| legacy DSL 只读转换 | PASS | 200，`executed=false`、`runs_executed=0`、`publishable=false`、blocking=`LEGACY_LIMITS_MISSING`，映射 15 条 |
| Scenario Run 端到端（现有库） | **FAIL** | 引擎 6.5s 内真的跑完两轮并产出 `frozen_observation` + 2 个 artifact，但结算 `OperationalError: table score_sets has no column named trial_id` → failed、scores=[]、无 pass |
| Scenario Run 端到端（健康 schema） | PASS | completed；pass `workflow-assertions@1`；2 条指标：`no-forbidden-action` passed=true、`goal` passed=false（**不发明 pass**，质量失败≠执行失败） |
| 多轮行为真实发生 | PASS | 第 1 轮模型回答「请问您要在哪个城市预订，以及参会人数是多少？」；第 2 轮给出结论并写入 checkpoint |
| Fixture 业务主流程 | **FAIL（结构）** | 无任何公共入口发布 FixtureVersion（`POST /api/v1/fixtures` → 404，CLI 无 fixture 命令）→ 引用它的 Workflow 创建 Run 时 422 `WORKFLOW_FIXTURE_MISSING: references unpublished fixture acc-order-state@1` |
| Skill 静态校验 | PASS | `POST /skills/validate` → 200，`ok=true`、`validation_scope=static`、`executed=false`、`resource_bytes_verified=false`；executable 缺入口 → 422 `SKILL_INVALID`（具名） |
| Skill 发布 | **FAIL（结构）** | `POST /api/v1/skills` → 422（该路由是 v0 内存注册表，要 `name`/`entrypoint`）；`GET /skills/versions` total=0；代码注释自认「发布（POST）暂不暴露」 |
| 三臂对照 | not_run | 依赖已发布 Skill，被上一行阻断 |
| Judge 预检（零费用） | 部分 | 拒收形状错误与缺证据都返回具名码且**零作业零调用**；但正例不可达（下一条） |
| Judge 作业（真实调用） | **FAIL / not_run** | Scenario Run → 422 `JUDGE_EVIDENCE_INVALID`；基准 Run → 422 `JUDGE_EVIDENCE_MISSING: no saved observation`。两条路都不通，因此**没有发生任何付费 Judge 调用** |
| 零模型调用证明 | PASS | 预检 + history 前后 invocations 2 → 2；`judge history` 返回 `{"items":[]}` |
| 校准资格 | not_run | 无 ≥30 条人工复核资料；按文档保持 experimental、gate_eligible=false |
| 多指标 Gate | not_run | 需要一条已发布的 Judge pass 作为输入；被 Judge 提交阻断 |
| Web：Workflow 页 | PASS | /scenario 列出 `acc-clarify-flow@1 published`、`acc-order-cancel@1 published`，编辑器标注「只读校验不发布、不建 Run、不产生调用与费用」 |
| Web：Skill 页 | 部分 | 显示「暂无已发布 Skill」并提示「先发布」——**而发布入口不存在** |
| Web：Judge 页 | **FAIL** | 「Judge 清单 能力不可用：服务端不允许该调用（HTTP 405）」 |
| Web：场景步骤页 | **FAIL** | 「逐步证据 能力不可用：服务端未注册该端点（HTTP 404）」，Workflow 字段显示「未知」 |

---

## 4. 缺陷清单（按严重度）

### F-01（阻断）SQLite 部署无可用升级路径 → 任何 Run 都无法结算

- **位置**：`packages/storage/motte_storage/run_store.py:601-605`

  ```python
  def _upgrade_score_sets(connection):
      columns = {row[1] for row in connection.execute("PRAGMA table_info(score_sets)")}
      if "metric_id" in columns:      # ← 错误短路
          return
  ```

  真实的旧表形是 `(scoring_pass_id, case_id, metric_id, ordinal, payload)`：**已经有 `metric_id`，
  但没有 `trial_id/evaluator_id/evaluator_version`**。于是升级被跳过，插入时缺列。`trials` 与
  `external_jobs` 根本没有升级函数。

- **确定性复现**（`var/m5-accept/repro_upgrade.py`）：

  ```
  shape A: no metric_id: → 8 列，upgraded=True
  shape B: metric_id present: → 仍是 5 列，upgraded=False
      insert with trial_id -> OperationalError table score_sets has no column named trial_id
  ```

- **实测漂移**（`var/m5-accept/schema_full_diff.py`，对比新库 vs `var/runs.db`）：

  ```
  DRIFT external_jobs | live_missing: job_id, launch_token
  DRIFT score_sets    | live_missing: trial_id, evaluator_id, evaluator_version
  DRIFT trials        | live_missing: trial_id, repeat_index, plan_hash, created_at
  ```

- **影响面**：M1（agent 评分）、M2（`no such column: repeat_index`）、M3（trials）、M5（score_sets）。
  在本机的现状是：**新 Run 跑得完，结算不了**——引擎、模型调用、证据采集都发生，最后一步写分数时失败。
- **为何测试没抓住**：所有测试都新建 store；缺一条「用旧形状库启动」的升级回归。
- **可用绕过（已验证）**：`var/m5-accept/repair_db.py` 把漂移表按新形状重建并保留同行数据；
  修复后的副本上 M1/M2/M5 全部跑通（本轮证据即建立在它之上）。
- **建议**：修正短路条件为检查完整目标列集；给 `trials`/`external_jobs` 补升级函数；
  加一条「旧形状库 → 启动 → 写入成功」的迁移测试。

### F-02（高）case 结果为 NULL 时评分崩溃，真实原因被覆盖

- **位置**：`packages/sdk-python/motte_sdk/agent_tasks.py:282`

  ```python
  observation = row.get("result", {}).get("observation") if row else None
  ```

  `result` 键**存在但为 None** 时，`dict.get(key, default)` 返回 None，随后 `.get` 抛 AttributeError。
  `not_attempted` 的 Case 行正是 `result=None`（前一个 Case 触发 stop_run 后未尝试的题）。

- **确定性复现**（`var/m5-accept/repro_score.py`，直接读真实库行）：
  用真实的 `acc-001 result={"error":...}` + `acc-002/acc-004 result=null` 调
  `agent_tasks_scores` → `AttributeError: 'NoneType' object has no attribute 'get'`，
  栈顶就是 `agent_tasks.py:282`。
- **影响**：整条 Run 的评分 pass 丢失；Run 级错误被替换成无关的 AttributeError；
  M1-E「展示失败发生在哪一步」在 API 与 Web 上都答不出来（UI 原文：
  「运行错误： 'NoneType' object has no attribute 'get'」）；为 `not_attempted` 准备的
  `case_not_attempted` 缺口指标分支实际不可达。
- **建议**：`result = row.get("result") or {}`，并补一条「一个 Case 硬失败、其余 not_attempted」的评分测试。

### F-03（高）M1 的 agent 工作区是 POSIX-only，Windows 上必然失败且错误分类误导

- **位置**：`packages/sandbox/motte_sandbox/workspace.py:143`（`os.open(anchor, O_RDONLY|O_DIRECTORY)`），
  以及 112/122/131 的 `dir_fd=` 用法。
- **实测**：`os.open(<目录>, O_RDONLY)` 在本机对**任何**目录都 `PermissionError [Errno 13]`
  （`E:\Dev\MoTTEavl`、`%TEMP%`、`C:\Windows` 全部如此）→ 这是 Windows 平台行为，不是 ACL 问题。
- **对照**：M5 的 `packages/scenario-runtime/motte_scenario/state.py:39` 明确写了
  `_DIR_FD_SUPPORTED = os.name == "posix" ...` 并提供同规则的便携实现。**同一仓库里已有正确模式，M1 未采用。**
- **附带**：该 `PermissionError` 被 `classify_exception` 归类为 `"class": "network"`，对操作者是误导。
- **建议**：M1 workspace 复用 M5 的便携分支；错误分类里把本地文件系统失败从 network 拆开。

### F-04（高）Fixture 版本没有公共发布入口

- `POST /api/v1/fixtures` → **404**；`motte --help` 无 fixture 命令；`publish_fixture` 只被测试调用。
- 后果：`docs/operations/scenarios.md` §2 声称的 `ResourceStore.publish_fixture` 发布路径**没有用户入口**，
  引用 fixture 的 Workflow 只能发布、不能运行（422 `WORKFLOW_FIXTURE_MISSING`）。
  M5 的「业务状态断言 / Fixture 初始化·快照·清理」主流程因此在产品面不可达。

### F-05（高）Skill 版本没有公共发布入口

- `apps/api/app/main.py:1885-1888` 注释自认「发布（POST）暂不暴露」；`POST /api/v1/skills` 是 v0 内存注册表
  （要求 `name`/`entrypoint`，与 M5 `SkillVersion` 不是同一对象）；CLI 无 skill 子命令。
- 后果：Skill 注入、三臂对照、`skill_injection=true` 的 Workflow 全部不可达；
  Web 却提示「先发布」一个 Skill。

### F-06（高）Judge 证据解析只认 `observation`，Scenario Run 写的是 `frozen_observation`

- **位置**：`packages/sdk-python/motte_sdk/scoring_jobs.py:191`（`raw = result.get("observation")`）。
- 谁写 `result["observation"]`：`agent_backend.py:402`、`cli_runtime.py:403`、`pi_runtime.py:505`、
  `inspect_import.py:62`。Scenario 路径写的是 `frozen_observation`（`docs/operations/scenarios.md` §4.4），
  基准路径**两者都不写**。
- 实测：Scenario Run → 422 `JUDGE_EVIDENCE_INVALID`（17 条 FrozenObservation 校验错，input_value 是
  `{'checkpoints':..., 'workflow':...}`）；direct-llm Run → 422 `JUDGE_EVIDENCE_MISSING: no saved observation`。
- 后果：**公共 Judge 入口在本部署上对任何 Run 都拒收**，R8「已接公共入口」在真实数据上不成立。
- **建议**：`frozen_observation` 优先回退；并补一条「Scenario Run + Judge 提交」的集成测试。

### F-07（高）Web Judge 页对着不存在的 API 契约

`apps/web/src/api/client.ts` 调用的路径与真实路由不符（实测状态码）：

| 客户端调用 | 真实结果 | 真实路由 |
|---|---|---|
| `GET /api/v1/judges` | **405** | 无集合 GET（只有 POST） |
| `POST /api/v1/judges/jobs` | **405** | `POST /api/v1/judges` |
| `GET /api/v1/judges/jobs/{id}` | 404 | `GET /api/v1/judges/{job_id}` |
| `GET /api/v1/judges/{judgeId}` | 404 | 无 |
| `GET /api/v1/judges/{judgeId}/calibration` | 404 | 无 |
| `POST /api/v1/judges/jobs/{id}/cancel` | 404 | `POST /api/v1/judges/{job_id}/cancel` |

客户端还建模了 `JudgeSpecView`（judge_id@version 的「已发布 Judge」资源），服务端没有这个概念。
`openapi-check` 与 `web-test` 都不校验手写的路径字符串，所以这条漂移穿过了全部门禁。

### F-08（高）场景步骤页依赖未注册端点

- `apps/web/src/api/client.ts:1276` → `GET /api/v1/runs/{runId}/steps`；真实路由表里没有它 → 404。
- 后果：M5 的步骤/checkpoint 下钻页永久显示「逐步证据 能力不可用（HTTP 404）」「Workflow 未知」，
  Run 的步骤证据无法在 UI 查看。

### F-09（中）CLI `scenario targets` 与已部署事实不一致

- `motte scenario targets` 退出码 0，stdout 打印「（没有注册的 scenario target adapter；逐步骤 Scenario Run 会在创建期被拒绝）」，
  而同一时刻 `GET /api/v1/scenario-targets` 返回 `builtin-agent` 可用，且同一条 CLI 的
  `scenario run` 成功创建了 Run。是列子命令的导入/装配不一致，不是能力缺失。

### F-10（中）Judge 公共入口接受的 spec 形状与库契约不一致且未文档化

- 把 `build_judge_spec(...)` 的 `JudgeSpec.model_dump()` 直接提交 → 422 `extra_forbidden`
  （`spec.mode`、`profile_sha256`、`prompt_id`、`spec.budget.price_known`、`spec.budget.price_table_version`、`spec.schema_version`）；
  正确的公开形状是 `{run_id, mode, spec{judge_profile_id, model, rubric_id, rubric_version, criteria, budget}, case_ids, authorisation, …}`
  （`apps/api/app/schemas.py:333-383`）。`docs/operations/judges.md` 没写这个形状，用户只能靠 422 逐条试。

### F-11（低）未登记 fixture 工具时，工具步骤的失败原因链较长

属已知缺口（`docs/operations/scenarios.md` §5.2 已声明「生产环境仍需按 fixture 登记真实业务工具实现」），
此处只作为验收事实记录：**当前部署没有登记任何 fixture 工具实现**（`register_scenario_tools` 只被测试调用），
因此真实业务状态变更类场景无法在本部署执行。

---

## 5. not_run 清单（缺失项写清楚，不折算成通过）

| 项 | 缺失条件 |
|---|---|
| M1 真实模型文件任务、A07 无泄漏核查 | F-03：Windows 上 agent 工作区不可用 |
| M2 C-Eval 端到端、M3 Terminal-Bench 全链路 | 本机无 docker；C-Eval/TB 数据集未准备、Runner 未连接 |
| M4 claude-cli / codex-cli / codex-app-server 真实任务 | 平台自报 `protocol_ready=false`；且未获操作者个人配额授权 |
| M4 Inspect 导入、交互式命令、Pi 真实任务 | 本轮预算内未展开；Pi 另有 `execution_ready=false` 门禁 |
| M5 三臂对照、Judge 真实作业、校准资格、多指标 Gate | 分别被 F-05、F-06、无人工标注资料、F-06 阻断 |
| PostgreSQL 语义、Linux 平台复跑 | `MOTTE_PG_DSN` 未设置；本机为 Windows（沿用上一轮结论） |

---

## 6. 费用与"零调用"证据

- **真实 Provider 调用共 9 次**（1 次 live-smoke + 2 次 live 场景 + 2 次修复库场景 + 2 次 live 基准 + 2 次修复库基准），
  模型均为 `deepseek-v4.1-flash`，全部走用户配置的 `6a` profile。
- live-smoke 计量：prompt 36 / completion 14 / total **50 tokens**，latency 750ms，identity `exact_match`。
- **成本始终未知**：`/api/v1/price_tables` 为空 → 每次响应 `cost: null`、`price_table_version: null`。
  平台没有把未知补成 0。**本轮没有发生任何付费 Judge 调用**（Judge 提交在 F-06 处被拒）。
- 零调用路径：`judge preflight` + `judge history` 前后 subject Run 的 invocations 计数 2 → 2 不变；
  所有 GET/list/validate/dry-run/targets 调用均未产生 invocation。

---

## 7. 我编制的测试数据与清理

| 资源 | 标识 | 位置 |
|---|---|---|
| M1 文件任务数据集 | `acc-file-report@1`（5 case） | 现有库 |
| M5 Workflow | `acc-clarify-flow@1`、`acc-order-cancel@1` | 现有库 |
| M5 Fixture 草稿 | `acc-order-state@1`（只能作为文件存在） | `var/m5-accept/data/` |
| M5 Scenario | `acc-clarify-flow@1`、`acc-order-cancel@1` | 现有库 |
| 我创建的 Run | `run-2a5a8f18…`、`run-136c50db…`、`run-5da7888e…`、`run-61974679…`、`run-d3e88087…` | 现有库（13 → 18） |
| 修复库副本 | `var/m5-accept/repaired.db`（另含 2 条 completed Run） | 独立文件，**未改动用户数据库** |

清理：删除上述 Run 需要走产品入口（`DELETE` 未提供 Run 删除），因此未删除；
它们都是带 `acc-` 前缀的可识别测试实体。所有脚本与证据在 `var/m5-accept/`，可直接删除。

---

## 8. 与「既有测试全绿」的关系（口径分离）

上一轮我已确认：M5 新增测试文件零失败、全套非 live 用例 `+373 passed`、`make lint/openapi-check/web-build` 全绿。
**这与本报告的结论不矛盾**：那些测试全部使用新建的 store、内存资源仓库与 ScriptedProvider；
本报告发现的 F-01/F-04/F-05/F-06/F-07/F-08 全部发生在「真实部署 + 既有数据库 + 真实 HTTP 入口」这一层。
换句话说：**单包测试通过 ≠ 产品入口可达**，而 M5 的验收要求恰恰是后者（「已接公共入口」）。

---

## 9. 建议的下一步（按性价比排序）

1. **修 F-01**（4 处升级条件 + 一条旧库升级回归）——这是让现有部署重新可用、产出所有后续证据的前提。
2. **修 F-02**（一行 `or {}` + 一条评分回归）——恢复失败可定位。
3. **接通 F-04/F-05**（fixture 与 skill 的发布入口，CLI 或 API 各一条）——M5 主流程与受控实验变量的前提。
4. **修 F-06**（`frozen_observation` 回退 + Scenario×Judge 集成测试）——让 Judge 真正可被提交。
5. **修 F-07/F-08**（Web 端点对齐；把路径纳入契约检查，例如由生成类型驱动 URL）。
6. 之后再补：M3 的 docker/数据集、M4 的 runtime profile 与 CLI harness 协议就绪、M5 的 ≥30 条人工校准资料。

在上述 1–5 完成并复跑本报告清单之前，**不建议把 M5 标为完成或合并**。
