# M3 Review Round 1 修复记录（2026-09-20）

> 对应报告：[M3 实现 Review — Round 1](M3-review-round1-2026-09-20.md)（26 项：P1 ×10、P2 ×16）。
> 本文件记录**逐项的失败反例 → 修复 → 同一反例复验**，以及无法在本机闭环的部分。
> 阶段级验证结论与 not_run/blocked 清单见 [M3 验证记录](M3.md)。

修复分支：`codex/m3-harbor-terminal-bench`（基线 `08d8f1e`）。
所有"复现"都在临时目录/临时库/隔离 API 中执行；"实跑"表示真实固定 Harbor 0.23.0
与真实 Docker（Linux/arm64 引擎，Docker 27.4.0）。**全程零付费模型调用**。

## 摘要

| 编号 | 结论 | 复验证据（命令或测试） |
|---|---|---|
| R01 | 已修复 | `tests/integration/test_harbor_job_fixture.py`（公共路径 2×2 / 2×3 / verifier-error） |
| R02 | 已修复 | `tests/benchmarks/test_harbor_task_compose_policy.py` |
| R03 | 已修复 | `tests/benchmarks/test_harbor_frozen_tasks.py`、`test_harbor_job_fixture.py::test_real_harbor_docker_calibration` |
| R04 | 已修复 | `tests/integration/test_harbor_live_cancel_and_deadline.py`（真实 Docker + 诱饵容器，实跑通过） |
| R05 | 已修复 | `tests/integration/test_harbor_partial_recovery.py`、`test_harbor_live_cancel_and_deadline.py`（真实 SIGKILL 残留定位） |
| R06 | 已修复 | `tests/benchmarks/test_harbor_verifier_and_task_path.py` |
| R07 | 已修复 | `tests/integration/test_harbor_job_deadline.py`、`test_harbor_live_cancel_and_deadline.py::test_real_job_deadline_cancels_and_keeps_evidence`（真实期限实跑） |
| R08 | 已修复 | `tests/api/test_terminalbench_review_fixes.py::test_trial_detail_redacts_content_but_keeps_identity` |
| R09 | 已修复 | `tests/evaluators/test_terminalbench_fingerprint.py`（生产 `build_run_inputs` 构造） |
| R10 | 实现完成 + 离线原生校验通过；真实调用仍 blocked | `tests/benchmarks/test_harbor_agent_mapping.py`、`tests/integration/test_harbor_agent_native.py`（真实 Harbor 接受该配置，零模型调用） |
| R11 | 已修复 | `tests/integration/test_harbor_evidence_io.py` |
| R12 | 已修复 | `tests/integration/test_harbor_evidence_io.py` |
| R13 | 已修复 | `tests/benchmarks/test_harbor_verifier_and_task_path.py`（同名不同路径任务） |
| R14 | 已修复 | `tests/evaluators/test_harbor_metrics.py::test_first_trial_follows_the_frozen_repeat_order` |
| R15 | 已修复 | `tests/evaluators/test_harbor_metrics.py::test_per_success_cost_requires_complete_known_cost` |
| R16 | 已修复 | `tests/api/test_terminalbench_review_fixes.py`（期限进冻结原生配置）、Web `R16` 用例 + 真实 API 回环 |
| R17 | 已修复 | `tests/cli/test_terminalbench_cli.py`（CLI 与 API 对账） |
| R18 | 已修复 | `tests/evaluators/test_terminalbench_comparison.py`（真实 Gate 入口） |
| R19 | 已修复 | `tests/api/test_terminalbench_review_fixes.py`（终端文本/工件内容/404/不可读）、Web `R19` 用例 |
| R20 | 已修复 | `tests/api/test_terminalbench_review_fixes.py`（4xx 而非 500；预检同判定） |
| R21 | 已修复 | Web `R21` 用例（切换 Task 不改变 Run 级成本卡） |
| R22 | 已修复（含真实 PG 并发复现） | `tests/storage/test_trial_store.py::test_postgres_create_plans_concurrent_first_write` |
| R23 | 已修复（含真实 PG 降级复现） | `tests/storage/test_trials_downgrade_guard.py` |
| R24 | 已修复 | `tests/storage/test_trial_store.py::test_attempt_trial_id_cannot_be_rewritten_by_a_transition` 等 3 个后端 |
| R25 | 已修复 | `tests/storage/test_trial_store.py::test_create_plans_returns_records_detached_from_the_store` |
| R26 | 已修复 | `tests/storage/test_trial_store.py::test_put_result_rejects_result_of_another_trial[memory/sqlite/postgres]` |

---

## P1

### R01 — 正式服务按 Task 覆盖 Trial，Run 完成时仍丢结果（P0）

**失败反例（修复前实测）**：把 2 Task × 2 repeat 的真实脱敏产物经
`RunService.execute_external_job` 导入后，`run_status=completed` 但只产生 2 个
Trial 分数；`service.store.trials.list_for_run()` 里另外 2 个计划单元停在
`pending`。2 Task × 3 repeat 时第二个 Task 的 6 个计划单元全部停在 `pending`
（测试输出 `assert (6 + 0) == 2`）。verifier 错误样本甚至无法走完公共路径。
复现方式：`git stash` 掉 `service.py`/`benchmark_plugins.py` 后运行下述测试，
3 个用例全红（本记录保留该步骤作为反例证据）。

**原因**：`_external_job_case_rows` 以 `task_key` 为字典键，同一 Task 的后一个
repeat 覆盖前一个；失败行还丢掉 Trial payload；任务级 Case 行被当成评分输入，
分母退化为任务数。

**修复**：
- 新增 `_external_job_records`：Trial 形态的 Run **一个计划 Trial 一条记录**
  （冻结计划索引按 `trial_id` 与 `<task_key>#<repeat>` 双键；无 payload 的行按
  计划补出身份与 disposition，重复行进审计不静默丢弃）；非 Trial 形态走
  `_legacy_case_rows`，M2 语义逐字不变。
- 先按 `trial_id` 完整导入全部 Trial 结果，再生成任务级派生聚合行
  （`result.aggregate_only`，不承载 Trial 身份），最后评分。
- 评分输入改为 Trial 存储（`_managed_scoring_rows`），rescore 与首次评分同源。
- 取消/超时/unsupported 用 `_ensure_trial_dispositions` 给未产出的计划单元补
  终态处置，已落盘结果不覆盖。
- 身份错配由存储层拒绝（R26），导入异常收敛为 `TRIAL_IMPORT_INVALID`。

**同一反例复验**：`tests/integration/test_harbor_job_fixture.py`
- `test_trial_results_import_into_the_trial_store`：4 个计划单元全部有结果、
  4 条 Trial 分数（trial_id 各不相同）、每 Task 覆盖 2/2、`valid_trial_coverage=1.0`，
  且 `rescore` 后行数与分母不漂移；
- `test_planned_repeats_without_results_stay_in_the_denominator`：2×3 → 分母 6、
  有效 4、覆盖 4/6、Gate 拒绝；
- `test_verifier_error_trial_keeps_identity_and_evidence`：verifier 错误 Trial 保留
  身份与证据，`denominator=false`、`passed=null`；
- `test_public_run_view_accepts_trial_shaped_case_rows`：公共 `Run` 契约校验
  （真实链路曾因此 500，见下）。

**过程中发现并修掉的两个新问题**（真实链路才能暴露）：
1. 任务级行带 `CaseRun` 契约之外的顶层 `unscored` 键 → `GET /api/v1/runs/{id}`
   响应校验 500。改为把标记放进 `result`，并新增契约校验测试；
2. 上一条同时修掉了 M2 遗留的同类隐患（`status == "unscored"` 的旧行也带该键）。

### R02 — 任务自带 Docker Compose 绕过宿主挂载预检

**失败反例**：在合法任务里放 `environment/docker-compose.yaml`（含
`/var/run/docker.sock`、`/:/host`、`privileged: true`、`network_mode: host`），
公共 preflight 修复前返回 `allowed=True`、空原因码。

**原因**：SDK 只检查声明的 `task_mounts`，而 Harbor 0.23.0 会把任务自带的
compose 作为 overlay 叠加进 `docker compose -f ...`（`docker.py::_docker_compose_paths`），
真实执行配置从未被检查。

**修复**：新增 `harbor/compose.py`（`yaml.safe_load`，解析失败即
`TASK_COMPOSE_UNINSPECTABLE`，读不到不放行）；准备阶段把结论写进
`task_facts[*].compose` 与 `has_compose_file`；预检把命中转成具名原因码
（privileged / host network / host namespace / 危险 capabilities / devices /
security_opt / 越界 bind、build context、env_file / 凭据插值 / include·extends），
并对 `TASK_HOST_PATH_EXPOSED`、`TASK_DOCKER_SOCKET_EXPOSED` 复用既有码。
`reason_messages` 补齐中文说明。

**复验**：`tests/benchmarks/test_harbor_task_compose_policy.py`（7 passed）：
恶意夹具在**零任务启动、零模型调用**时被拒绝（`model_calls=0`、`task_starts=0`）。

### R03 — 排队后的 Task 文件可变化，Runner 不校验冻结内容

**失败反例**：prepare → `build_run_inputs` → 改 `instruction.md` 后，执行仍然读到
被改动的目录（修复前 `DID NOT RAISE`；符号链接替换、增删文件同样不报错）。

**修复**：`plan` 冻结 `task_files`（逐文件 sha256）与 `task_content_hashes`；
`HarborJobAdapter.prepare` 逐字节复制到 `work_dir/frozen-tasks/<task_key>/`
并核对（缺失/多出/内容不符/symlink 都是漂移 → `HARBOR_TASK_CONTENT_DRIFT`，
源缺失 → `HARBOR_TASK_SOURCE_MISSING`），`MOTTE_TASK_ROOT` 指向该副本；
Runner 构造原生配置前再复验一次并写 `harbor/frozen-tasks.json`。

**复验**：`tests/benchmarks/test_harbor_frozen_tasks.py`（8 passed）+ 真实链路
`test_real_harbor_docker_calibration`（实跑通过，`frozen_files_verified` 有值）。

### R04 — 容器所有权只有 metadata，取消/清理没有管理 Harbor 容器

**失败反例（真实 Docker）**：修复前在运行中取消后，本 Job 的容器仍处于
`running`（断言 `assert 'running' != 'running'` 失败）；清理报告 `clean` 却没有
核验过任何容器。

**原因**：`motte.job=...` 只写进句柄元数据，从未注入真实容器；interrupt/cleanup
只委托进程适配器。

**修复**：Runner 通过 `EnvironmentConfig.extra_docker_compose` 注入平台 overlay，
给 Harbor 的 `main` 服务打上 `motte.job` / `motte.run` / `motte.owner`；Runner 把
每个 Trial 的 compose project 名写进定位文件（覆盖任务自带的额外服务）。
新增 `harbor/containers.py::ContainerOwnership`：只认本 Job 的标签或 project，
`interrupt` 停止、`cleanup` 停止/删除并把观察到的容器写进资源账本，`state`
取值 `clean`/`residual`/`unknown`，daemon 不可达时**不报 clean**。

**复验**：`tests/integration/test_harbor_live_cancel_and_deadline.py`（真实 Docker，
实跑通过）：运行中取消 → 本 Job 容器被停止、诱饵容器（`motte.job=unrelated-live-job`）
仍在运行、残留逐项可定位；容器标签实测包含 `motte.job`/`motte.owner`/`motte.run`
（见 R04 清理记录）。另有一次误启 Job 的真实清理：`state: clean`、无关容器未被动过。

### R05 — Job 定位文件写得太晚，中断时丢失已完成 Trial

**失败反例（真实 SIGTERM/SIGKILL）**：修复前进程被硬终止后没有定位文件、没有
完成标记，采集得到 `job_dir_unavailable: True`、`collected trial files: []`，
全部计划单元退化为 `not_attempted`——已经完成的 Trial 证据丢失。

**修复**：`entry.py` 在 `Job.create` 之后、`Job.run()` 之前写定位文件（含真实
trial_names 与 compose projects），注册 TERM/INT handler 在信号里重写定位文件
+ 写 143/130 完成标记后 `os._exit`（不依赖 `finally`）；适配器在"启动中"的定位
文件下也能采集已完成 Trial。

**复验**：`tests/integration/test_harbor_partial_recovery.py`（3 passed）+
`test_harbor_live_cancel_and_deadline.py::test_real_wrapper_crash_leaves_locatable_residue`
（真实 SIGKILL 后定位文件仍在且 `trial_names` 非空，实跑通过）。

### R06 — Verifier 超时但留下 reward 时被判定为有效通过

**失败反例**：`result.json.exception_info.exception_type=VerifierTimeoutError`
且 structured rewards 与 `reward.txt` 均为 1 时，修复前返回
`status=scored/reward=1/error=None`（`assert 'scored' == 'verifier_error'` 失败）。

**修复**：`read_verifier_observation` 先判定 Verifier 异常语义（`verifier_error`，
disposition `indeterminate`），原始 reward 与来源 hash 保留在证据里；非 Verifier
异常（`AgentTimeoutError`）仍走有效评分路径。

**复验**：`tests/benchmarks/test_harbor_verifier_and_task_path.py`（5 passed）：
有 reward 的 Verifier 超时、无 reward 的 Verifier 错误、Agent 超时但 Verifier
正常三种情形分别断言。

### R07 — 公共 Job 超时接受秒数，却没有实际期限

**失败反例**：不同 `job_sec`/`environment_build_sec`/`agent_setup_sec` 都只生成
固定倍率 `1.0`，秒数留在旁路字段；Supervisor 实际读取的 `max_wall_seconds`
没有设置（阻塞 fixture 修复前跑满 5 分钟无人中断）。

**修复**：`agent_setup_sec` → `agents[].override_setup_timeout_sec`；multiplier
一律 1.0 并断言 `effective_timeouts`；`environment_build_sec` 无法精确表达 →
创建时 `HARBOR_TIMEOUT_UNSUPPORTED`（拒绝而非假装执行）；`job_sec` →
`external.limits.max_wall_seconds` 由 Supervisor 强制，且适配器在缺期限时
`HARBOR_JOB_DEADLINE_MISSING` 拒绝启动。

**复验**：阻塞 fixture 在约 2.5 秒内以 `JOB_TIMEOUT` 结束（中断 + 部分分数 +
清理）；真实链路 `test_real_job_deadline_cancels_and_keeps_evidence` 用
`sleep 600` 任务 + `job_sec=25` 实跑通过。

### R08 — Trial 详情接口直接返回含合成凭据的错误消息

**失败反例（实测，修复前）**：把含合成哨兵
`sk-review-sentinel-0123456789abcdef` 的 Verifier 错误保存为合法 TrialResult，
`GET /api/v1/runs/{run_id}/trials/{trial_id}` 原样返回该哨兵
（测试输出明确打印了泄漏内容）。

**修复**：门面层 `redact_display_text` 只对内容字段（message/error/detail/log/
note/reason/output/text…）复用 `motte_trace.redaction`，**身份与 hash 字段
（trial_id/task_key/sha256/artifact_id）逐字保留**——不对整个 detail 调用
`redact_secrets`，否则键名规则会把 `task_key` 一起改写。

**复验**：`test_trial_detail_redacts_content_but_keeps_identity`：哨兵不出现、
出现 `[REDACTED-SECRET]`、`trial_id`/`task_key`/`sha256` 原样。

### R09 — 改变重复数、超时与资源仍被判断为可比

**失败反例**：真实 `build_run_inputs` 构造 baseline `n_trials=1/agent_sec=5/
memory_mb=256` 与 candidate `3/105/1280`，policy 只允许 model 变化时修复前
`eligible=True`、`metric_eligibility.quality=True`。

**修复**：比较不变量补全冻结实验指纹（agent_id/agent_version/n_trials/timeouts/
resources/tools/retries/limits/environment/credentials），并修正取值来源
（`environment_digest`/`runner_version`/`dataset_revision` 从 profile 回退到
external 根）；命中政策显式允许的因子只记录 `ALLOWED_FACTOR:*` 不阻断；
case 集合在缺 `case_ids` 时回退到冻结的 `task_manifest.task_keys`/`selected_tasks`；
`ComparisonPolicy` 契约把上述因子登记为合法因子。

**复验**：`tests/evaluators/test_terminalbench_fingerprint.py`（9 passed，全部用
生产 `build_run_inputs` 构造 manifest）。

### R10 — 模型选择未进入 Runner，真实 Agent 路径尚未实现

**失败反例**：CLI 指定 `--model NONEXISTENT-MODEL` 仍成功 queued；冻结
`external.profile.model={}`；allowlist 仅 oracle，真实 Agent 一律
`HARBOR_AGENT_UNSUPPORTED`。预检还要求必须有模型（oracle 也不例外）。

**修复**：
- `AGENT_SPECS`：`oracle`（固定 1.0.0、零模型）+ `claude-code`（显式钉住 CLI
  版本 → `kwargs.version`、必须模型、必须凭据引用 `env:ANTHROPIC_API_KEY` /
  `ANTHROPIC_AUTH_TOKEN`）；`model` → `model_name` 映射保留，oracle 之外缺模型
  报 `HARBOR_AGENT_MODEL_REQUIRED`；
- API/CLI 把已发布 ModelProfile 解析成 `{provider, model}` 后写入冻结 Profile，
  unknown/未发布模型在创建前 4xx；
- Runner 在启动前解析凭据引用（缺失 → 退出码 5），新增 `--validate-config`
  只校验模式（真实 Harbor 的 `JobConfig` + Agent options 模型，零容器零模型调用）。

**复验**：
- `tests/benchmarks/test_harbor_agent_mapping.py`（13 passed）：映射、钉版本、
  模型/凭据必需性、预检与创建同判定；
- `tests/integration/test_harbor_agent_native.py`（3 passed，真实固定 Harbor）：
  `ClaudeCodeOptions` 接受 `version=2.0.30`、`model_name=anthropic/claude-sonnet-4-5`，
  `config-validation.json` 写出 `options_validated`/`credentials.resolved`；
  缺凭据环境变量时退出码 5 且不留下虚假校验结论。

**仍未闭环（如实标注）**：真实模型调用 **blocked**（需用户单独授权）。因此
R10 的"实现缺项"已关闭，但"真实调用已验证"不在本阶段结论内——这一区分也写进
了兼容矩阵与操作指南。

---

## P2

### R11 — 二进制工件仅保存 hash，没有冻结内容
失败反例：`AttributeError: 'HarborJobAdapter' object has no attribute
'read_output_bytes'`，且外层文件不在证据索引里。
修复：`read_output_bytes` 返回 `(文本, 二进制清单)`，非 UTF-8 文件经
`binary_sink`（SDK 在 `_wire_evidence_sink` 挂上与证据同一 Artifact sink）写
内容寻址 Artifact，索引记 `{encoding, sha256, size_bytes, artifact_id, media_type}`；
sink 缺失/失败如实记 `artifact_id: null` + `note`。
复验：`tests/integration/test_harbor_evidence_io.py`（8 passed，其中二进制用例
2 个）：非 UTF-8 roundtrip、删掉工作目录后仍可恢复、诚实 null 情形、
文本不走二进制通道。

### R12 — Harbor 外层配置采集绕过安全读取边界
失败反例：把 `work/harbor/config.json` 换成指向外部秘密文件的 symlink，修复前
跟随链接并把内容并入 bundle；超限文件同样被读入。
修复：`_read_outer_files` 统一走 `TrustedDir`（拒 symlink、大小限额、`O_NOFOLLOW`
锚定），refuse 码 `HARBOR_OUTPUT_UNREADABLE`/`HARBOR_OUTPUT_TOO_LARGE`，并把
外层文件 sha256/size/encoding 登记进 `evidence-index.json`（`scope: outer`）。
复验：`tests/integration/test_harbor_evidence_io.py`（8 passed）。

### R13 — Parser 丢弃完整 Task 路径，同 basename 任务无法归属
失败反例：`a/same`、`b/same` 两个任务各带完整 `task_id.path`，修复前都落
`HARBOR_TASK_NAME_AMBIGUOUS` 且全部 `not_attempted`。
修复：匹配顺序改为 冻结副本目录 → 规范化相对路径后缀 → 唯一 basename →
trial 名前缀；命中方式记入 `termination.task_matching`，仍有歧义时拒绝。
复验：`test_harbor_verifier_and_task_path.py::test_full_task_path_distinguishes_same_basename_tasks`
（5 passed）。

### R14 — first-trial 实际选择了第一个有效 Trial
失败反例：repeat 0 为 `verifier_error`、repeat 1 为 `reward=1` 时，修复前
`task_pass=True`、reason 为 `first-trial`。
修复：按事前 `repeat_index` 取首个计划 Trial；首个无效 → `task_pass=None`、
reason `first_trial_invalid`（`no_valid_trial` 仍表示"没有任何有效 Trial"）。
复验：`tests/evaluators/test_harbor_metrics.py::test_first_trial_follows_the_frozen_repeat_order`。

### R15 — 全部成本未知时每成功成本仍返回 0
失败反例：所有 Trial `cost_usd=None` 且有成功 Trial 时，修复前
`per_success_usd=0.0`；部分未知时也给出完整单位成本（`assert 0.75 is None`）。
修复：`per_success_usd` 只在成本完整且 successes>0 时给出，否则 `null` 并附
`per_success_usd_basis`；保留明确命名的小计比率
`known_cost_subtotal_per_success_usd` 并在 `note` 声明覆盖范围。
复验：`test_per_success_cost_requires_complete_known_cost`、
`test_cost_keeps_unknown_null_and_zero_success_na`（四种组合）。

### R16 — Web 单 Trial 超时字段被 API 静默忽略
失败反例（实测，修复前）：Web 发 `timeout_sec`/`agent_timeout_sec` 时，前者被
静默忽略；且 API 只认后者。修复前用 `timeouts` 对象请求会得到 422
`MODEL_REQUIRED`（旧实现完全不认这个字段）。
修复：公共 DTO 唯一化（`timeouts`/`resources` 对象 + `agent_id`/`agent_version`/
`n_trials`/`task_keys`/`dataset_revision`/`aggregation`/`model`），未知字段与类型
错误 422；Web 用导出的 `buildTerminalBenchRunRequest` 构造请求体并在本地先校验
（`n_trials` 1..32、秒数 > 0、不再提供 `environment_build_sec`）。
复验：`tests/api/test_terminalbench_review_fixes.py` 断言
`agents[0].override_timeout_sec/override_setup_timeout_sec/verifier.override_timeout_sec`
与 `limits.max_wall_seconds`；Web 侧把**真实提交的 body** 打到真实 API 上核对
冻结配置（`override_cpus/memory_mb/storage_mb/gpus` 与各期限一致）。

### R17 — CLI status 丢掉冻结计划，覆盖率与聚合规则和 API 不一致
失败反例（实测，修复前）：同一临时库冻结 2 Task × 2 Trial、mean-success，只有
一个 Trial 有结果时，CLI 输出 1 Task / 1 Trial / coverage 1.0 且聚合规则错位；
API 为 2 Task / 4 Trial / coverage 0.25。
修复：`status` 先 `get_run` 再把冻结 manifest 传给 `task_rows`，输出与 API
相同的 `aggregation`/`selected_trials`/覆盖率/Gate。
复验：`tests/cli/test_terminalbench_cli.py`（3 passed）：分母、聚合、以及
CLI 与 API 逐字段对账。

### R18 — 比较页配置的 Gate 指标没有后端实现
失败反例：policy 用 `valid_trial_pass_rate` 时，修复前 Gate 报
`unsupported metric: 'valid_trial_pass_rate' (known: accuracy, cost.total_usd)`；
TB Run 还会掉进 accuracy 路径报 `attempted 4 exceeds selected 2`。
修复：`gates.METRIC_REGISTRY` 注册 `valid_trial_pass_rate`（gte/ratio/分母
`valid_trials`）与 `valid_trial_coverage`（gte/ratio/分母 `planned_trials`）；
`coverage.py` 支持 `planned_trials` 分母；`candidate_summary` 识别 TB Run 并从
冻结 aggregate / 该 pass 的 ScoreSet + 冻结计划取同一口径；Gate 结论额外返回
结构化 `coverage_summary`（Web 不再从规则文本里解析数值）。
复验：`tests/evaluators/test_terminalbench_comparison.py`（共享 Gate 入口，
可通过与必须拒绝两类样例）+ Web `R18` 用例。

### R19 — Terminal、轨迹和工件详情没有真实内容读取链路
失败反例（实测，修复前）：详情里没有 `terminal` 字段，工件内容路由不存在
（`KeyError: 'text'`）；Web fixture 自己填了真实 API 从不返回的 `terminal_text`。
修复：冻结 bundle 读取器 `FrozenEvidenceReader`（归属 → 内容寻址 Artifact 优先、
否则 bundle 内路径 → hash 校验 → 有界解码 → 脱敏）；详情新增可读 `terminal`；
新增按引用读取内容的路由与原始字节下载路由（`/bytes`，响应头显式标注
`X-Motte-Artifact-Redacted: false`）；Web 用真实 DTO 形状重建 fixture，处理
截断/二进制/不可读与迟到响应。
复验：`tests/api/test_terminalbench_review_fixes.py`（终端文本可读且脱敏、
内容路由 200、非本 Trial 工件 404、不可读给原因不给假内容）+ Web `R19` 用例
（含"切换 Trial 后晚到内容不渲染"）。

### R20 — 无效创建参数返回 500，preflight 还能放行不支持的 Agent
失败反例（实测，修复前）：不支持 agent_id、`n_trials:"oops"`、不存在 task_key
都返回 500；GET preflight 对不支持 agent 返回 `ok=true`（或 422 `MODEL_REQUIRED`）。
修复：结构化请求校验（未知字段/类型/范围 → 422 具名 code），Agent 能力用
`AGENT_SPECS` + `agent_capability_reasons` 在创建与预检两处同一判定；
`build_run_inputs`/`select_tasks` 的错误统一映射成 4xx。
复验：5 个拒绝分支各自断言 4xx + code，并断言 `GET /api/v1/runs` 总数为 0
（没有留下 Run/Job）。

### R21 — Run 概览成本卡只汇总当前 Task
失败反例（静态）：全 Run 质量概览旁的成本卡使用当前选择 Task 的 trials，
切换 Task 会改变"已知成本/每成功成本"。
修复：Run 卡消费全 Run `aggregate.cost`；Task 范围的小计单独标注「Task 范围」，
无标签的 Task 级单位成本已移除；成本不完整时显示"未知"并给 basis。
复验：Web `R21` 用例（两 Task 不同费用，切换后 Run 卡逐字节不变）+ `R15` 用例
（null 不渲染成 0、小计标注为小计）。

### R22 — PG create_plans 并发首写不能满足幂等契约
失败反例（真实 PostgreSQL 16，两连接 + `pg_locks` 同步屏障）：修复前第二个事务
`UniqueViolation: duplicate key value violates unique constraint "trials_pkey"`。
修复：`INSERT ... ON CONFLICT (trial_id) DO NOTHING`，`rowcount == 0` 时读回并按
共享 `_apply_plan` 判定 identical/conflict，整批一个事务。
复验：`test_postgres_create_plans_concurrent_first_write`（同 payload → identical；
异 payload → conflict 且保留先写入者），在真实 PG 上实跑通过。

### R23 — Migration downgrade 静默删除 Trial 证据
失败反例：修复前 `0008_trials` 无条件 DROP（且有数据时也不拒绝）。
修复：`downgrade_blockers(bind)` 统计 `trials` 行数与 `case_attempts` 中
`trial_id <> ''` 的行数，非空即抛错并给出导出/清理步骤；空库降级照常执行。
复验：`tests/storage/test_trials_downgrade_guard.py`（8 passed，含真实 PG 的
"升级 → 拒绝降级 → 版本不变 → 证据仍在 → 空库降级 → 回到 head"）。

### R24 — Attempt transition 可以改写 trial_id
失败反例（内部接口）：`transition(changes={"trial_id": ""})` 修复前 `DID NOT RAISE`，
随后 `complete(case_run=...)` 可以写任务级 CaseRun；SQL 后端的 payload 与索引列
还可能脱节。
修复：`advance_record` 的身份保护集合加入 `trial_id`；三个后端统一在读取时用
`_indexed_attempt` 校验 payload 与索引列一致（不一致 → `RunConflictError`）。
复验：三个后端的 transition/complete 拒绝测试 + 列漂移测试（memory/sqlite
离线，postgres 实跑）。

### R25 — Memory create_plans 返回内部引用
失败反例：改写返回值的 `result[0]["plan"]["plan"]["repeat_index"]` 后，再次 get
得到被改过的冻结计划；重复提交原计划仍返回 identical。
修复：Memory 出口（created/identical/conflict）与 `_plan_conflict` 全部深拷贝；
SQLite/PG 的 created 分支同样返回副本。
复验：`test_create_plans_returns_records_detached_from_the_store`、
`test_put_result_returns_a_copy_of_the_frozen_result`。

### R26 — put_result 不校验 payload 身份与目标 Trial 一致
失败反例：`put_result("t1", result(trial_id="t2"))` 修复前返回 `stored`，
t1 名下永久保存 t2 的结果。
修复：`_check_result_identity` 在写入前核对 `trial_id` 相等，且结果里若带
`run_id`/`task_key`/`repeat_index` 必须与冻结计划一致；错配抛 `ValueError`
且不改动旧记录。调用侧（SDK 导入）以**记录**的 trial_id 为目标，错配被存储拒绝，
Run 以 `TRIAL_IMPORT_INVALID` 失败。
复验：memory/sqlite 参数化测试 + PG 实跑用例。

---

## 本轮同时发现并修掉的实现缺陷（不在原反馈清单内）

| 编号 | 缺陷 | 触发方式 | 修复 |
|---|---|---|---|
| N-01 | 任务级 Case 行带 `CaseRun` 契约外键 → `GET /api/v1/runs/{id}` 500 | 真实链路（R01 修复后的生产链测试） | 标记移入 `result` + 新增契约校验测试（`test_public_run_view_accepts_trial_shaped_case_rows`） |
| N-02 | `scripts/runner/harbor-entry` 丢弃调用方参数：`--validate-config` 被静默忽略，校验命令变成真实 Job（起过 2 个任务容器） | review 修复期间的校验命令 | wrapper 转发 `"$@"`；校验失败时清理上一次的 `config-validation.json`；误启容器已用平台所有权视图清理（`state: clean`，无关容器未动） |
| N-03 | `validate_agent_config` 用 `AgentFactory.create_agent_from_config` 构造 Agent，`OracleAgent` 因缺少 task_dir/trial_paths 直接失败 → 所有真实 Run 退出码 5 | 真实链路复验 | 改用 Agent 的 options 模型入口 `parse_options(kwargs, os.environ)`（不构造 Agent）：既校验 `kwargs.version`，又不把合法校准 Run 判成配置错误 |
| N-04 | 真实链路测试跑的是**上一次部署**的桥接快照（`install-harbor` 拷贝进 venv），桥接改动不会被验证 | 真实链路复验 | 真实链路测试与校验命令通过 `PYTHONPATH` 指向仓库源码；部署路径仍由 `install-harbor` 承担，操作指南已注明"改桥接层后必须重新部署" |

## 未闭环项（不做"看起来完成"的表述）

1. **真实模型/Agent live 验收：blocked。** `claude-code` 的实现与离线原生配置
   校验已完成（真实 Harbor 接受该配置，零模型调用），但真实调用需用户显式授权：
   可审阅任务清单、数据集 revision、模型、Profile、重复数、预算、期限与命令。
   本记录不声称"真实 Agent 路径已验收"。
2. **上游完整任务集（89 / 10 题）：not_run。**
3. **linux/amd64 宿主、受限网络策略、Verifier 可见性变体：not_run。**
4. **PG 全部结论来自一次性容器（已删除）**；本机默认无 `MOTTE_PG_DSN`，
   PG 门控用例在默认运行中 skip（skip 不计通过）。真实 PG 两连接并发、降级
   拒绝与 identity 校验均在一次性容器上实跑过。
5. **二进制证据下载路由不做脱敏**：`/bytes` 返回冻结原始字节（响应头已标注），
   文本视图才脱敏。若后续要求"连原始字节也必须脱敏"，需要额外的内容扫描层，
   本轮未实现。
