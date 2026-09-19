# External Jobs 协议（job-based Benchmark 执行）

> 状态：**契约 + 进程适配器 + Job 持久化已接通（M2-T01/T02/T03）**。
> C-Eval 数据/配置/Parser 与公共入口在 T04–T07 接入；在此之前
> `external-benchmark@1` 后端保持 `available=false`，创建 Run 时即拒绝，
> 不会产生模型调用。

## 1. 执行模式

每个执行后端（`packages/sdk-python/motte_sdk/execution_backends.py`）声明
`execution_mode`，随 ResolvedManifest 的 `execution` descriptor 一起固定：

| 模式 | 语义 | 后端 |
|---|---|---|
| `sample` | 逐 Case 调用 `ExecutionHandle.invoke(case_id)`，既有路径不变 | direct-llm、replay、builtin-agent |
| `job` | 一次 Run 只启动**一个**外部 Job，覆盖全部 selected case，不逐题 invoke | external-benchmark（job 适配器） |

规则：

- 旧 manifest 不含 `execution_mode` 时按 `sample` 读取（`ExecutionSpec` 缺省值），
  历史 Run 投影（`legacy_execution`）同样补 `sample`。
- manifest 显式请求的 `execution_mode` 与后端注册值冲突时，`resolve_execution`
  抛 `EXECUTION_MODE_CONFLICT`。
- job 模式的 `ExecutionHandle` 提供 `run_job(dispatched_run) -> outcome` 入口；
  Dispatcher（`packages/sdk-python/motte_sdk/dispatcher.py`）据此改走
  `RunService.execute_external_job`，整个 Run 只调用一次 `run_job`。
- API/CLI 只创建 Run（排队），不启动 Job；启动只发生在 Worker 分派。

## 2. 数据契约

契约定义在 `packages/contracts/motte_contracts/external_job.py`（Pydantic，
frozen、`extra=forbid`）：

| 契约 | 固定字段 | 关键不变量 |
|---|---|---|
| `ExternalJobSpec` | run_id、adapter_id/version、runner_version、execution_config_hash、dataset_revision、selected_case_ids、profile、work_root、environment_digest、limits、retry_policy | 版本类字段非空且**拒绝占位值**（`latest`/`tbd`/`placeholder` 等）；`selected_case_ids` 非空且唯一；`profile.benchmark_id/benchmark_version` 必填 |
| `ExternalJobHandle` | job_id、run_id、launch_token、external_id、owned_resources、launch_identity、work_dir、created_at、status、collection_cursor | `launch_token` 每次启动新生成（`new_launch_token()`）、不可复用；保存 PID 时必须同时保存 `launch_identity`（启动身份），恢复不能仅凭 PID 判定是原任务 |
| `NormalizedCaseResult` | stable_case_key、source_case_id、output_ref、native_score_refs、status、error_category、usage、evidence_coverage | status ∈ succeeded/failed/not_attempted/unscored；未观测到的 usage/费用/身份如实 unknown |
| `ImportBatch` | job_id、source_artifact_hash、parser_version、record_keys、checkpoint、conflicts | record_keys 非空且唯一；幂等键 = job_id + source_record_key + parser_version |
| `ExternalRetryPolicy` | runner、provider_transport、operator | 三种重试分开记录，缺省 0；配置转换不叠加默认重试 |

`ExternalJobStatus`：`prepared → launching → active → collecting → settled`，
另有 `failed / cancelled / indeterminate`。Job 状态描述外部作业子过程，
**不替代** Run 状态；Run 继续使用既有合法迁移。

## 3. adapter 操作（Protocol 声明）

`ExternalJobAdapter` 声明六个操作；adapter 只执行作业，不直接修改 Run 表，
持久化由应用层负责：

```
prepare(spec)                  # 校验并创建受控工作目录；无执行副作用
start(spec, handle)            # 唯一产生执行副作用的操作；应用层先持久化
                               # 启动意图与 launch_token 再调用
poll(handle)                   # 刷新 Job 状态
interrupt(handle)              # 中断本 Job 拥有的进程树/容器
collect(handle, cursor)        # 从 cursor 起采集归一化结果，返回 (results, cursor)
cleanup(handle)                # 清理本 Job 资源并列出残留
```

## 4. manifest 校验与 JobSpec 投影

- `validate_external_job_manifest(manifest)`（共享校验器）：要求
  `manifest.external_benchmark` 钉住 `adapter_id`、`adapter_version`、
  `runner_version`、`dataset_revision`、`environment_digest` 与
  `profile.benchmark_id/benchmark_version`。缺任一项抛
  `EXTERNAL_JOB_VERSION_REQUIRED`；缺 adapter 身份抛
  `EXTERNAL_BENCHMARK_CONFIG_INVALID`。
- `external_job_spec_from_run(run, work_root=...)`：把冻结的 Run 投影为
  `ExternalJobSpec`；`selected_case_ids` 取自 `run.case_ids`（为空即拒绝），
  `execution_config_hash` 为 external_benchmark 规范 JSON 的 sha256（确定性）。

## 5. job 结局 → Run 终态（当前映射）

`execute_external_job` 以 job 模式入口的 outcome 映射：

| job_status | 含失败 case | Run 终态 |
|---|---|---|
| settled | 否 | completed |
| settled | 是 | failed（部分结果保留） |
| failed | — | failed |
| cancelled | — | cancelled（终态不复活） |
| indeterminate | — | needs_review |

每个 selected case 都有处置行：结果缺失的记 `not_attempted`，不静默消失。
采集检查点、同键冲突与迟到结果的审计政策由 M2-T03 的 Job 存储接管。

## 6. 进程适配器（M2-T02，`packages/benchmark-runtime/`）

独立于 API 进程的适配器包（workspace 成员 `motte-benchmark-runtime`，
模块 `motte_benchmark`）：

- `process.ProcessJobAdapter`：实现 `ExternalJobAdapter` 协议的受控子进程
  适配器。进程执行走 asyncio 子进程 API（专用事件循环线程），不经 shell。
  - **prepare**：用 M1 加固的 `CaseWorkspace`（逐组件拒 symlink、dir_fd
    打开、归属校验）创建 `work_root/job-<id>`；无进程执行。
  - **start**：唯一副作用操作。子进程独立会话启动（POSIX
    `start_new_session` / Windows `CREATE_NEW_PROCESS_GROUP`）；
    `launch_token` 经 `MOTTE_LAUNCH_TOKEN` 环境变量与 `{launch_token}`
    argv 占位符传入受控 wrapper；`{work_dir}`/`{job_id}`/`{run_id}` 同理
    可用。启动身份（pid、host、started_at、token）随句柄落库。
  - **stdout/stderr 有界消费**：只保留尾部 `max_output_bytes` 字节并标记
    truncated；限制优先级 spec.limits > adapter 默认。
  - **poll**：会话内凭进程对象 + token；跨会话凭 argv 中嵌入的 token 核验
    （Linux 读 `/proc/<pid>/cmdline`，macOS 经 `ps -p <pid> -o command=`；
    Windows 不可核验）。进程不在但受控产物存在 → settled（exit 不可观测）；
    均不可证明 → indeterminate。
  - **interrupt/cleanup**：只信号**本 Job 拥有**的进程树（TERM → 宽限 →
    KILL，按进程组）。token 不可核验时绝不信号（PID 被无关进程复用不会
    误杀），残留资源列在 cleanup 报告里；不执行全系统 prune，不删除未知
    状态的工作目录。
  - **collect**：经 `CaseWorkspace` 读 `results.json`（`{"records": [...]}`，
    每条 `case_id`/`status`/`output?`/`error?`/`usage?`）；拒绝 symlink
    逃逸、超大文件与半写 JSON（`JOB_OUTPUT_INVALID`），不伪造记录。
    cursor 记 `records_consumed`，重复采集只返回新增记录。
- `registry`：显式内置 adapter 注册表；`adapter_for` 对未注册 id 抛
  `ADAPTER_UNKNOWN` 并列出已知项，不经在线安装用户插件。
- `fake_runner`：合成假 Runner（`-m motte_benchmark.fake_runner`），
  测试与离线验收用，不是官方 C-Eval 内容。

`NormalizedCaseResult` 契约在 T02 增加 `output`/`error` 透传字段；结果
冻结为受控 Artifact 后（T03）改用 `output_ref` 引用。

## 7. 应用层监督（`motte_sdk.external_jobs.ExternalJobSupervisor`）

- `launch(spec)`：prepare → 生成**新** launch_token → 持久化启动意图
  （`intent_journal`，T03 替换为 Job 存储事务）→ `adapter.start` → 持久化
  句柄。写入顺序固定 launch_intent → start → launch_started。
- `run(spec)`：launch 后轮询至终态（`max_wall_seconds` 超时 → 只中断本
  Job 进程树，outcome 记 `JOB_TIMEOUT`）并采集。
- `recover(spec, handle)`：崩溃恢复只观察/采集，绝不 start；token 不可
  核验且无受控产物 → `JOB_OUTCOME_INDETERMINATE`（映射 needs_review），
  禁止仅凭“没有 results.json”重新付费执行。
- `interrupt(spec, handle)`：操作员取消——先中断自有进程，再尽力采集部分
  工件；终态不因迟到输出复活。

## 8. Job 持久化与幂等导入（M2-T03）

存储（`motte_storage.external_jobs`，Memory/SQLite/PostgreSQL 三实现，
PG 表来自 alembic `0006_external_jobs`）：

- `external_jobs`：job_id/run_id/status/launch_token + 完整 spec/handle/
  checkpoint payload。`begin_job` 幂等（同 job_id+同 token 重放返回现有；
  不同 token 是启动身份冲突，Job 永不二次 start）。
- `external_job_records`：幂等键 `(job_id, source_record_key, parser_version)`
  + content_hash + payload。`import_record`：同键同内容 no-op；同键不同
  内容 conflict——已落库记录不变，incoming 摘要进冲突账本，两份都保留。
  记录与 job checkpoint 在**同一事务**提交（崩溃恢复无半状态）。
- `external_job_conflicts`：冲突账本（existing/incoming hash 与 payload）。

`DurableExternalJobRunner`（`motte_sdk.external_jobs`）装配 supervisor 与
上述存储，作为 job 模式 `run_job` 入口：

1. **一个 Run 只启动一个 Job**：已存在 Job 记录时绝不 start。活跃
   （launching/active/collecting）→ 只恢复观察；**终态且
   `checkpoint.import_completed`** → 从已导入记录重建 outcome（快路径）；
   **终态但导入未完成**（最终化前崩溃）→ 重新观察/采集并幂等补齐，
   绝不二次启动（review R04）。
2. 最终化顺序固定（review R04）：
   **证据冻结 → 幂等导入 → checkpoint（cursor 指标 + 证据引用 +
   `import_completed`）→ 最后写终态**。导入中途崩溃时 Job 仍非终态，
   恢复路径重新采集并补齐；原始完整证据不被截断版本覆盖。
3. 原始 outcome 冻结为**内容寻址**受控 Artifact
   （`external-jobs/<run>/<job>/outcome-<hash16>.json`），原始输出树
   另存 evidence bundle（`evidence/raw-<hash16>.json`，受预算内含内容、
   超预算仅 hash）——解析前先快照 hash，工作目录清理后仍可审计与重新
   解析（review R09）。native/diagnostic 指标随 checkpoint 持久化，
   并以 `external_job_metrics` run 事件与 scoring pass summary 暴露。
4. 导入冲突 → 结局改 failed（`EXTERNAL_IMPORT_CONFLICT`），停止最终化并
   保留两份摘要（M2-A06）。
5. 操作员取消：`RunService.cancel` 先持久化取消请求，再经注册的
   `interrupt_run` 中断本 Run 拥有的进程；**观察循环同时消费持久取消
   请求（`should_cancel`）**——API 与 Worker 分进程时，另一实例落库的
   取消也能在有限时间内中断挂起 Job（review R05）。中断后迟到的
   failed/indeterminate 观察不覆盖 Job 的 cancelled 状态，Run 终态不
   复活（M2-A09）。未声明 `max_wall_seconds` 的 Job 默认上限 3600s。
6. 迟到结果只走 `import_late_results`：写审计事件
   `external_job_late_results`（audit_only），不改 Run 状态、不新增评分。

`launching` 状态但进程不可核验（崩溃于 start 前后）→ recover 观察 →
indeterminate → Run needs_review；禁止仅凭“没有 results”重新执行（M2-A07）。

## 9. 完成标记与可信恢复（review R10）

- wrapper/Runner 承诺在结束时**原子写完成标记**
  `<work_dir>/.motte-job-complete`：`{"exit_code": <int>, "completed": true}`
  （先写 `.partial` 再 rename；`scripts/runner/opencompass-entry` 与假
  Runner 均遵循）。
- 进程不可核验时，poll 只信完成标记：标记 exit 0 → settled、非零 →
  failed；**没有标记而只有部分输出 → indeterminate**，部分采集保留为
  审计证据，不升级为成功。存在 results.json 不再等同于成功退出。

## 10. Runner 配置与受控 adapter 加载（review R01/R13）

- 创建入口（API/CLI 共用 `prepare_external_run_inputs`）把
  `build_opencompass_config` 的完整配置（模型快照、逐题 prompt、few-shot、
  凭据**引用**、config_hash）冻结进 `manifest.external_benchmark.runner_config`，
  并同步冻结平台侧 gold 来源 `manifest.case_expectations`；adapter
  `prepare` 将其写入受控目录 `runner-config.json`（真实 Runner 的输入）。
  缺 `runner_config.cases` 的外部 Run 在创建/分派层拒绝
  （`EXTERNAL_JOB_VERSION_REQUIRED`）。
- adapter 注册只经 `motte_benchmark.runner_config.ensure_builtin_adapters()`：
  读 `MOTTE_RUNNER_CONFIG` 指向的受控 JSON（`{"adapters": [{"benchmark":
  "ceval"|"cmmlu", "argv"|[...]}|"module": "..."}]}`），或固定环境 wrapper
  `/opt/motte-runner/bin/opencompass-entry` 实际存在时注册；两者皆无则
  RUNNER_NOT_CONNECTED，绝不静默落到不可执行 argv。API/Worker/CLI 启动时
  各自调用，配置同源。
- 结果采集按 `runner-config.json` 的冻结 case 顺序映射
  `(subject, 行序) → CaseID`；未知/越界/重复映射隔离为 unmapped 记录并使
  Run failed，绝不串题（review R03）。评分 gold 以 `case_expectations`
  为权威，Runner 报告 gold 分歧记入 score details。
