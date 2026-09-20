# Task 与 Trial 身份协议（Harbor / Terminal-Bench）

> 状态：**契约、存储与 migration `0008_trials` 已实现并有测试**；PG 实机
> 往返随 `MOTTE_PG_DSN` 门控（本机未设，**not_run**，CI postgres service 执行）。
> 本协议回答"什么算同一个任务/同一次实验重复"，不描述 Harbor 如何执行任务。

## 1. 分层身份

| 层 | 身份 | 说明 |
|---|---|---|
| Task | `task_key` | 受控任务目录内容的稳定身份；一个 Task = 一个逻辑 Case（`case_id == task_key`） |
| Trial | `trial_id` | **事前计划**的实验重复；同一 Run 同一 Task 内 `repeat_index` 唯一 |
| Attempt | `attempt_no` | 传输/执行层的操作记录；重试只增加 attempt，不产生新 Trial |
| Job | `job_id` + `launch_token` | 外部作业与启动身份；每次启动新 token，不可复用 |
| Pass | `scoring_pass_id` | 不可变评分批次（M1/M2 语义不变） |

## 2. TaskIdentity

字段（`packages/contracts/motte_contracts/trial.py`，frozen 契约）：

| 字段 | 含义 |
|---|---|
| `source_id` | 任务来源标识（本地或固定来源） |
| `dataset_revision` | 数据集 revision；占位值（`latest`/`tbd`/`placeholder` 等）拒绝 |
| `normalized_relative_path` | 受控根目录内的 POSIX 相对路径（展示名另存，不参与唯一键） |
| `task_content_hash` | 任务目录内容 hash（见 2.2） |
| `task_key` | 由上述字段派生的稳定键（见 2.1） |
| `upstream_task_id` | `task.toml` 声明的上游身份（`task.name` + `task.version`，可空） |

### 2.1 task_key 派生与校验

`task_key = canonical_hash({schema_version, source_id, dataset_revision,
normalized_relative_path, task_content_hash, upstream_task_id})`。

`canonical_hash` = 规范 JSON（键排序、紧凑分隔符、UTF-8）的 sha256，带
`sha256:` 前缀。`task_key` 是派生字段但作为**存储字段**保存：`TaskIdentity`
在**每次反序列化**时重算并比对，不一致即报错（`task_key does not match the
canonical identity hash`），因此持久化身份不可能与内容 hash 悄悄漂移，也不会
产生两个"看起来都合法"的身份。

### 2.2 task_content_hash

- 对该任务目录下**全部常规文件**取 `{相对 POSIX 路径: 文件字节 sha256}`，
  排序后做规范 JSON 的 sha256。
- **时间戳与权限位不参与**：只改 mtime 不改变身份；内容变化 = 新身份。
- 目录无常规文件 → `TASK_EMPTY`；单文件 8 MiB / 单任务总量 256 MiB / 文件数
  2048 / 单路径 512 字符 / 扫描条目 20000 / 任务数 512 为准备限额；超限与路径
  越界是**整次准备失败**（`TASK_LIMIT_EXCEEDED`、`TASK_PATH_TOO_LONG`、
  `TASK_PATH_ESCAPE`），不降级成"某个候选不合法"后继续。
- 读取只读且不执行：逐组件 `O_NOFOLLOW` 打开受控根（`TrustedDir`），根级
  symlink、父链替换、目录内 symlink 逃逸一律拒绝；`task.toml` 只被解析，
  任务包脚本从不执行、不 docker build、不发模型请求。
- 路径归一化：Windows 分隔符转 POSIX；拒绝绝对路径、盘符、`..`、含分隔符/
  冒号/NUL 的组件；拼接后再断言仍在受控根内（`resolve_within`）。
- 同字节重复准备得到同一 `manifest_hash`；内容变化必须换新 revision，旧身份
  不被覆盖（`verify_task_manifest` 复验并列出 `TASK_CONTENT_CHANGED` 与差异
  文件）。

## 3. 同名不同路径永不合并

Harbor 的 `task_name` 只是**目录 basename**，不足以定位任务。平台的处理：

1. 平台把**显式任务列表**交给 Harbor（`job.tasks` 条目为受控相对路径，
   `datasets: []`），不把整个目录当 dataset 由上游自行发现；
2. 解析侧优先用 `task_id.path` 的末段匹配冻结计划里的相对路径；
3. 路径不可用才退回 basename 映射，且同一 basename 命中多个计划任务时
   **拒绝猜测**：该 Trial 记 `unmapped`（`HARBOR_TASK_NAME_AMBIGUOUS`），
   不归属任何任务、不合并身份；
4. 无法归属的 Trial 目录记 `HARBOR_TRIAL_UNMAPPED`；Harbor 产出的 Trial 多于
   冻结计划时，多出的记 `HARBOR_TRIAL_UNPLANNED`（保留为审计证据、不参与评分）。

`task_key_of` 只做相对路径精确匹配，不做 basename 近似。

## 4. TrialPlan

| 字段 | 说明 |
|---|---|
| `trial_id` | `"trial-" + canonical_hash({schema_version, run_id, task_key, repeat_index, seed, agent_config_hash, environment_hash})[:32]` |
| `run_id` | 所属 Run（计划与最终 Run 身份必须一致） |
| `task_key` | 被重复的 Task |
| `repeat_index` | 事前计划的重复序号（0..n_trials-1），同一 Task 内唯一 |
| `seed` | 可空；参与计划身份 |
| `agent_config_hash` | Agent/模型/工具/期限的冻结 hash |
| `environment_hash` | 环境类型/资源/Harbor 版本与拓扑的冻结 hash |

规则：

- `repeat_index` 是**计划重复**：传输重试、操作员 retry、基础设施重试都
  **不改变**它，也不产生新的 Trial；操作员 retry 仍创建新 Run。
- 计划覆盖必须完整：Task 集合与计划不一致 → `HARBOR_PLAN_TASK_MISMATCH`；
  每 Task 的 Trial 数不等于 `n_trials` 或 `repeat_index` 未覆盖
  `0..n_trials-1` → `HARBOR_PLAN_INCOMPLETE`。
- 计划在创建时冻结进 manifest 并随配置落盘；采集前先按冻结计划建 Trial 记录，
  因此"某个 Trial 没跑成"会留下 `pending`/`not_attempted`，不会从覆盖分母消失。

### 4.1 结果与上游身份

`TrialResult` 携带 `trial_id`（平台身份）与 `source_trial_id`（**Harbor 自己的
Trial UUID**），两者不可混用：前者由计划派生、跨重复稳定，后者用于把证据映射
回上游目录。`VerifierObservation` 的状态与处置：

| Verifier 状态 | 处置（disposition） | 语义 |
|---|---|---|
| `scored` + reward > 0 | `succeeded` | 质量通过 |
| `scored` + reward = 0 | `failed` | **有效失败**，不是缺失 |
| `missing_verifier_evidence` | `indeterminate` | 缺 reward 文件；不默认 0 或 1 |
| `verifier_protocol_error` | `indeterminate` | 畸形/类型或范围不符/多来源冲突；保留原始 hash |
| `verifier_error` | `indeterminate` | Verifier 超时/解析失败，独立于 Agent 失败 |
| 未观测到 | `not_attempted` / `cancelled` | 保留计划单元并如实标注 |

契约层强制：`succeeded`/`failed` 必须来自 `scored`，非 `scored` 不得冒充质量
结论；reward 必须是有限数字（`bool`/字符串/`null`/NaN/Inf 拒绝）；非 `scored`
的观测不得携带 reward。

## 5. Attempt / Job / Pass 身份分离

- 一个 Task 一条**逻辑 CaseRun**（`case_id == task_key`）；Trial 是它的子记录，
  存在 `trials` 表，不写进 `case_runs` 的多行。
- `case_attempts` 增加可空 `trial_id`（migration 默认 `''`）：trial 维度的
  attempt 把冲突域从"整个 Case"收窄到"同一 Trial"，**trial 作用域的
  CaseAttempt 不得写任务级 CaseRun**（否则一次重复先完成就会写死整个 Task）。
- `attempt_no` 仍是 `(run, case)` 内全序编号（既有唯一键不变），
  `idempotency_key` 为 `{run_id}:{case_id}:{trial_id or '-'}:{attempt_no}`：
  传输重试产生新 attempt、不产生新 Trial（M3-G04）。
- **旧语义保持**：没有 Trial 的历史 Run/attempt 不伪造重复记录，按 M2 语义
  读取（`trial_id` 为空串）；ScoreSet 复用既有 trial 维度，不重建评分表。
- Job 层：一个 Run 只启动一个 Job；`launch_token` 每次启动新生成，
  `launch_identity` 与 PID 分开保存，恢复不能仅凭 PID 认定原任务。

## 6. 存储语义（Memory / SQLite / PostgreSQL 同语义）

| 操作 | 结果 | 规则 |
|---|---|---|
| `create_plans` | `created` / `identical` / `conflict` | 同 `trial_id` 同内容 = no-op；同 `trial_id` 异内容 = 冲突，**保留先写入的计划**并返回两份摘要。并发首写用原子插入冲突处理（`ON CONFLICT DO NOTHING` 后核对内容），两个事务同时首写不会变成唯一键异常 |
| `put_result` | `stored` / `identical` / `conflict` | 同 `trial_id` 同 `source_hash` 同内容 = `identical`；`source_hash` 或内容不同 = `conflict`。写入前逐项核对**目标 Trial 身份**：`result.trial_id` 必须等于目标，结果里若带 `run_id`/`task_key`/`repeat_index` 必须与冻结计划一致，错配抛 `ValueError` 且不改变旧记录 |
| 终态结果 | 不可覆盖 | 已落盘结果永不改写（M3-A10：原始证据变化/重复采集必须显式失败） |
| `list_for_run` | 全量计划单元 | 含尚无结果的 `pending` 行，覆盖统计不靠缺行推断 |
| 出口对象 | 深拷贝 | Memory 后端返回的每个出口（created/identical/conflict）都与存储内部对象解耦，调用方改写返回值不影响已冻结记录及其 hash |

Trial 导入按冻结 manifest 的计划先建计划、再逐条幂等落结果；冲突使 Job 结局
转 failed（`EXTERNAL_IMPORT_CONFLICT`），停止最终化并保留两份摘要。

### 6.1 采集 → 导入的映射（一 Job 多 Trial）

- Runner 的每一行是**一个计划 Trial**，导入以 `trial_id` 为键：同一 Task 的多个
  重复各自落库，**不按 `task_key` 建字典**（那会让第二个重复覆盖第一个，Run
  完成时仍丢结果）。
- 失败/取消/未尝试的行也要有完整处置：行里没有 payload 时按冻结计划补出身份与
  disposition（`not_attempted` / `indeterminate`），错误进审计；"没有证据"与
  "没有记录"是两件事。
- 任务级 `case_runs` 行是**派生聚合**（一行一个逻辑 Task，`result.aggregate_only`
  标记，不承载 Trial 身份）；质量评分只消费 Trial 层结果，覆盖分母来自计划。
- 取消/超时/unsupported 终态会给尚未产出结果计划单元补终态处置
  （`_ensure_trial_dispositions`），已落盘的结果绝不覆盖。
- 旧语义（非 Trial 形态的 M2 外部套件）逐字保持：一行一 Case、`call_failed` /
  `not_attempted` 的既有形状不变。

## 7. Migration `0008_trials`

- 新增 `trials` 表（`trial_id` 主键，`run_id`/`task_key`/`repeat_index`/
  `status`/`plan_hash`/`payload`/`result_payload`/`created_at`/`finished_at`），
  索引 `(run_id, task_key, repeat_index)`（PostgreSQL）/ `(run_id)`、
  `(run_id, task_key)`（SQLite）。
- `case_attempts` 增加 `trial_id TEXT NOT NULL DEFAULT ''`（旧行即为空串），
  并新增 `(run_id, case_id, trial_id)` 索引。
- SQLite 在首次打开既有库时**就地补列**（`ALTER TABLE ... ADD COLUMN`），
  不重建、不丢既有计划；PostgreSQL 走 alembic。
- `downgrade` **拒绝在有 Trial 证据时执行**：`downgrade_blockers(bind)` 会统计
  `trials` 行数与 `case_attempts` 中 `trial_id <> ''` 的行数，非空则抛错并给出
  导出/清理步骤（先导出这两处数据、清理后才能降级）；空库降级照常执行，
  且不触碰任何评分与 `case_attempts` 既有行。
