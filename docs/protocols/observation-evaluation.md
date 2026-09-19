# Observation 与通用评分协议（M1）

状态：M1 已实现（`implemented`），仓库内离线验证通过；真实模型 live 证据见 `docs/verification/M1.md`。
契约来源：`packages/contracts/motte_contracts/evaluation.py`、`packages/contracts/motte_contracts/evidence.py`。

## 1. 冻结 Observation

Agent Case 终止后，平台从持久证据构建 `FrozenObservation`（schema_version=1）：

| 字段 | 语义 |
|---|---|
| `run_id` / `case_id` / `attempt_id` | 归属身份；不复用 legacy `Observation.name` 冒充 Case 身份 |
| `final_output` | 模型最终输出（评分输入原文；展示层另行脱敏） |
| `termination` | `{reason, detail}`；reason ∈ final_answer / max_steps / max_tool_calls / wall_time / token_limit / cost_limit / cancelled / error / invalid_state |
| `event_refs` | 指向持久 trace 事件的 `(kind=event, run_id, locator=seq)` 引用 |
| `artifact_refs` | 产物清单：安全相对路径、媒体类型、大小、sha256、available、truncated |
| `tool_calls` | 冻结工具轨迹（参数已脱敏）：call_id、tool_name、status（succeeded/failed/denied）、step |
| `workspace` | before/after 文件清单（fixture 物化后取 before）与 `complete` 标志 |
| `processes` | 观测到的进程退出码（M1 文件任务为空；未观测保持 unknown） |
| `coverage` | 采集完整度：`complete=false` 时 `missing` 说明缺口，评分不得把"没看到"当"不存在" |
| `usage` | Provider 报告的 token/cost；`reported=false` 时不虚构（预算强制能力同步降为 unknown） |
| `evidence_hash` | 评分输入视图的 `sha256`；`recorded_at`/`evidence_hash` 为审计字段不参与 hash |

不变量：评分只读冻结视图与其引用的 Artifact 字节；终止后修改 workspace 不改变评分。
旧 `Observation(name/value/source)` 保留为兼容读模型，二者不互换。

## 2. 多指标与 ScoreSet

- 每指标一个 `MetricResult`：`metric_id`、`status`、`value`/`passed`（互斥可空）、`unit`、`evaluator_id/version`、`reason`、`denominator`、`details`。
- 状态机：`scored` / `insufficient_evidence` / `evaluator_error` / `not_applicable`。
  - 非 `scored` 不得携带 `passed` 或 `value`（禁止把未知/缺失/异常映射成 0 或通过）。
  - 无期望（no_expectation）→ `not_applicable` + `denominator=false`，保留 Direct LLM 历史语义。
- 持久化落 `score_sets`，复合键 `(scoring_pass_id, case_id, trial_id, metric_id, evaluator_id, evaluator_version)`；SQL 侧无值维度一律用 `''` 规范化，避免 NULL 唯一性差异。`trial_id` 预留给 M3，M1 写入恒为 `''`。
- legacy 单指标行与多指标行不得混入同一 ScoringPass；单指标读取 API（`get(pass, case)`）遇到多指标行显式报歧义，绝不取第一行。
- ScoringPass 追加不可变：rescore 生成新 pass；旧 pass 字节内容不变；GET/report 零执行与评分副作用。

## 3. 确定性评分器（agent-deterministic@1）

封闭注册表（`motte_eval.observation`），无动态 import。逐指标隔离异常；配置含上限：

| kind | 关键配置 | 缺证据语义 |
|---|---|---|
| exact | expected、normalization（strip/lowercase，显式声明才生效） | 无 expected → not_applicable(no_expectation)；无 final_output → insufficient |
| contains | expected、case_policy | 字段缺失 → insufficient(final_output_missing) |
| regex | pattern、flags、max_input_bytes、timeout_sec | pattern 错误 → evaluator_error(config_error)；超时/超限 → evaluator_error(regex_timeout/input_too_large)。匹配在可终止的子进程内执行 |
| json-schema | schema（Draft 2020-12） | 输出非法 JSON → scored=False(invalid_json)（被测失败，不是评分器故障）。schema 在可终止的子进程内校验：病态 `pattern` 超时 → evaluator_error(schema_timeout)，远程 $ref 一律阻断 |
| file-exists | path | 采集不完整 → insufficient(capture_incomplete)；条目不可用 → insufficient(artifact_unavailable) |
| file-content | path + mode（exact/contains/hash/schema） | 工件缺失/损坏（hash 不符）/截断/超限 → insufficient；schema 模式同样走可终止子进程 |
| exit-code | allowed、label | 未观测 → insufficient(exit_code_unobserved / process_not_observed) |
| tool-call | tool、min/max_calls、args_schema、forbidden | 轨迹不完整 → insufficient(tool_trajectory_incomplete)，不能证明"从未调用"；args_schema 在可终止子进程内校验 |
| no-forbidden-write | forbidden（fnmatch）、ignore_preexisting | 快照不完整 → insufficient(workspace_snapshot_incomplete)；结论只覆盖受控 workspace 范围（`details.scope`）。轨迹完整时结合写入记录：成功的 `write_file` 命中禁写路径即违规（"写入后恢复原内容"不因终态一致而通过）；轨迹不完整退回快照口径 |

执行边界：`max_input_bytes` / `max_artifact_bytes`（默认 1MB/10MB，上限 16MB/64MB）、`regex_timeout_sec`（默认 2s，上限 10s）、`eval_deadline_sec`（默认 30s，上限 120s）。超期剩余指标记 `evaluator_error(eval_deadline_exceeded)`，不静默跳过。

分母政策：`scored` 计入分母；`not_applicable` / `insufficient_evidence` / `evaluator_error` 排除并在聚合并分开计数（`aggregate_metric_results`）。

## 4. 脱敏边界

- 事件 / SSE / invocation 摘要：键名（`api_key` 等）+ 值形状（`sk-…`、`ghp_…`、`AKIA…`、`Bearer …`、`xoxb-…`）双重脱敏，命中以 `[REDACTED-SECRET]` 标记，不保留正文。
- 评分输入（final_output、Artifact 字节）为冻结原文，以 evidence_hash 绑定；展示层（artifact content API）另行值形状脱敏。两套口径的差异是声明性行为，不是未声明的口径漂移。
- gold / 隐藏断言（expected、forbidden_paths、评分规则）不进入模型输入与工具可见空间；provider case 投影只含 `input`。

## 5. InvocationRecord（调用日志）

每次模型/工具调用持久化 `prepared → dispatching → settled` 边界（存储 `agent_invocations`，SQLite/InMemory/PG + migration 0005）：

- `settled` 必须有 `outcome`（succeeded/failed/indeterminate）与 `settled_at`；未 settled 的 dispatching 表示副作用可能已发生。
- 请求/结果摘要一律脱敏并有界；这是 CaseAttempt 之下的证据与恢复判定，不是第二套 Run 状态机。
- builtin-agent 声明 `safe_to_repeat=false`：worker 重启时 dispatching 尝试进入 needs_review，绝不自动重放；瞬时 Provider 失败不进入 second-chance 补跑。

## 6. 消费方

- M2/M4/M5 消费 `FrozenObservation`/`MetricResult`/`AgentResult`/受控工具与 InvocationRecord 约定。
- M3 使用预留 `trial_id` 评分维度扩展 `score_sets` 复合键。
- M6 消费指标状态、coverage 与终止语义做比较与门禁（M1 的 Web 并列阅读页明确不是正式可比性结论）。
