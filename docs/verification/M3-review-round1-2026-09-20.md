# M3 实现 Review — Round 1（2026-09-20）

## 结论与范围

**结论：不通过，不能把当前实现标记为 M3 代码验收完成。** 本轮记录 26 项反馈（10 项 P1、16 项 P2），包含公共执行链路、安全边界、评分、展示和存储契约问题。优先修复 P1；P2 也属于已约定的 M3 交付要求。

- 审查分支：`codex/m3-harbor-terminal-bench`。
- 审查提交：`08d8f1eab8f08f7d87310675fff15835c80d4502`；对比基线：`50e01b9`。
- 依据：[M3 需求](../roadmap/M3-harbor-and-terminal-bench.md)、[逐包计划](../superpowers/plans/2026-09-20-m3-kickoff.md)、[开发提示词](../prompts/M3-development-agent.md)。
- 主审与三个独立子代理分别检查服务/评分组合路径、Runtime/Harbor、安全与持久化、API/CLI/Web。审查前工作区干净；本轮只新增此报告，未修改业务代码。
- 以下代码定位均针对上述提交；修复后行号可能变化。

证据标记：**复现**指临时目录、临时数据库、隔离 API/CLI 或纯函数执行；**静态**指确定的代码路径/SQL 缺口，但本轮没有执行相应真实基础设施场景。存储内部接口反例不等同于已经发生公共入口故障。

## 本轮验证

`make check` 返回 0：Python **1333 passed / 33 skipped / 2 warnings**；Web **15 files / 201 tests passed**。Ruff、contracts 类型检查、编译、Web build、bridge selftest、Compose config 和 OpenAPI 一致性检查通过。完整本地日志：`/tmp/motte-m3-review-make-check.log`。

主审补充反例脚本保存在本次机器的 `/tmp/motte_m3_review_repro.py`、`/tmp/motte_m3_parser_identity.py`；Runtime 反例为 `/tmp/m3-runtime-review-repro.py`。这些临时文件不是长期仓库测试，下列反馈给出可转成正式回归测试的输入、结果和断言。

**本轮未运行真实 Provider、真实 Docker/Harbor Job、真实 PG 两连接并发、浏览器交互或真实运行中取消。** 读取固定 Harbor 0.23.0 源码、加载其原生配置模型不等于实际执行 Job。开发方已有校准记录保留其原有证据层级，不算本轮独立复验；skip 不算通过。

现有测试通过与本报告并不矛盾：多项测试分别验证 Parser、存储和私有导入 helper，未贯通正式服务层；生产校准路径使用单 repeat，也未覆盖重复 Trial 丢失；Web fixture 自行提供了真实 API 没有的字段。

## P1：优先修复

### R01 — 正式服务按 Task 覆盖 Trial，Run 完成时仍丢结果

- **定位：** `packages/sdk-python/motte_sdk/service.py:467`、`:479`、`:499`；后续 `_import_trials` 消费同一 Case 字典。
- **复现：** 使用现有 fixture adapter 得到 2 Task × 2 repeat 共 4 条结果，再通过真实 `RunService.execute_external_job` 导入。最终 `run_status=completed`，只产生 2 个 Trial 分数，repeat 0 的两条计划仍是 `pending`；覆盖为 `2/4=0.5`。
- **原因/影响：** `_external_job_case_rows` 将 Trial 的 `task_key` 作为字典键，后一个 repeat 覆盖前一个。`failed/not_attempted` 分支还丢掉 Trial payload，可能让单 repeat 的错误结果也失去身份与证据。任务级 Case 聚合无法替代 Trial 原始结果存储。
- **修复/复验：** 先按 `trial_id` 完整导入全部结果，再单独生成 Task Case 行和评分；错误/取消/未尝试也保存完整 Trial disposition。通过正式创建、执行、恢复与 rescore 路径验证至少 2 Task × 3 repeat（1/0/error），不能只调用 `_import_trials`。对应 T02/T06/T08、A03/A07。

### R02 — 任务自带 Docker Compose 绕过宿主挂载预检

- **定位：** `packages/sdk-python/motte_sdk/terminalbench.py:185`；`packages/benchmark-runtime/motte_benchmark/harbor/environment.py:180`。
- **复现：** 在合法任务中加入 `environment/docker-compose.yaml`，包含 Docker socket 与 `/:/host` 挂载，重新 prepare 后公共 preflight 仍返回 `allowed=True`、空 reason。此过程没有启动容器。
- **原因/影响：** SDK 不从任务的实际 Compose 提取 `task_mounts/task_env_vars`，预检检查的是空的声明参数。固定版 Harbor 会加载该任务 Compose，因此声明的“不向任务暴露宿主根目录/socket”没有覆盖真实执行配置。
- **修复/复验：** 对实际合成后的环境配置执行策略检查，或首版明确拒绝自定义 Compose；覆盖 volumes、privileged、相关宿主权限与替换后的配置。恶意夹具必须在零任务启动、零模型调用时拒绝。对应 T05、A13。

### R03 — 排队后的 Task 文件可变化，Runner 不校验冻结内容

- **定位：** `packages/benchmark-runtime/motte_benchmark/harbor/entry.py:94`；`packages/sdk-python/motte_sdk/terminalbench.py:223`。
- **复现：** prepare → build_run_inputs → 修改 `instruction.md`。`verify_task_manifest` 已返回 false，但 `resolved_task_paths` 和固定 Harbor 原生配置加载仍接受该目录。
- **原因/影响：** 执行时只有目录存在检查；Runner 配置保留相对路径，没有可用于重新核对的完整文件 hash，也不使用不可变内容副本。执行内容可以与 task_key、revision、预检和评分快照不一致，tests/solution/Compose 同样可能漂移。
- **修复/复验：** 准备不可变执行副本并在 Runner 边界校验清单和字节；漂移必须在启动前拒绝，不能仅重新计算身份后沿用旧 Run。覆盖排队后修改和路径替换。对应 T01/T03/T06、G01/G06。

### R04 — 容器所有权只有 metadata，取消/清理没有管理 Harbor 容器

- **定位：** `packages/benchmark-runtime/motte_benchmark/harbor/adapter.py:132`、`:146`、`:150`。
- **静态：** `motte.job=...` 只写入 handle 的 `owned_resources`，没有注入真实容器；interrupt/cleanup 仅委托 `ProcessJobAdapter`，未检查或停止本 Job 的 Docker 容器/volume。Harbor entry 没有对应的信号清理路径。
- **影响：** Docker daemon 管理的容器不能靠结束 Runner 进程组保证停止；当前 cleanup 可在没有核验容器的情况下报告 `clean`，runbook 的 label 查询也没有可靠对象。本轮未实际制造活动容器残留。
- **修复/复验：** 真实注入并核验 run/job/trial owner token，持久化资源引用，实现定向停止、残留检查与恢复；状态不能超过可观察证据。用受控 Docker 验证 in-flight cancel、wrapper 崩溃、清理失败，并保留无关容器。对应 T07、A08/A09。

### R05 — Job 定位文件写得太晚，中断时丢失已完成 Trial

- **定位：** `packages/benchmark-runtime/motte_benchmark/harbor/entry.py:206`、`:222`；`harbor/adapter.py:217`。
- **复现：** 在 work/jobs 中放置已经完成的 Trial 文件，但没有 `job-location.json`（对应 Job 未返回前 Runner 被结束）。`read_output_files` 只输出配置、计划和 `job_dir_unavailable`，不采集已有 Trial。
- **原因/影响：** 定位文件要等整个 `Job.run()` 返回或捕获普通 Exception 才写；TERM/KILL 不保证走异常分支。取消或进程崩溃会让已有结果不可恢复，当前 Parser 再将计划标为未尝试。
- **修复/复验：** 启动前持久化已知受控位置，恢复时安全定位并冻结部分结果；对硬终止不依赖 Python finally。验证首个 Trial 完成、后续 Trial 运行中中断，恢复后首个证据/分数保留且不重启任务。对应 T06/T07、A07/A09。

### R06 — Verifier 超时但留下 reward 时被判定为有效通过

- **定位：** `packages/benchmark-runtime/motte_benchmark/harbor/parser.py:350`，异常判断位于 `:367`。
- **复现：** `result.json.exception_info.exception_type=VerifierTimeoutError`，同时 structured rewards 与 `reward.txt` 均为 1。`read_verifier_observation` 返回 `status=scored/reward=1/error=None`。
- **原因/影响：** reward 分支提前 return，吞掉明确的 Verifier 错误。旧文件或部分产出可被计算成质量通过。
- **修复/复验：** Verifier timeout/crash 先决定错误语义，同时保留原始 reward 证据；保留“Agent timeout 但 Verifier 正常”的有效评分路径。分别覆盖有 reward 与无 reward 的两类 Verifier 错误。对应 T04/T08、A06。

### R07 — 公共 Job 超时接受秒数，却没有实际期限

- **定位：** `packages/benchmark-runtime/motte_benchmark/harbor/config.py:412`；`packages/sdk-python/motte_sdk/terminalbench.py:256`。
- **复现/静态：** 不同 `job_sec`、`environment_build_sec`、`agent_setup_sec` 都仅生成固定倍率 `1.0`，秒数留在旁路记录。SDK 外部 limits 只有 poll interval；Supervisor 实际读取的 `max_wall_seconds` 没有设置。
- **影响：** UI/API/CLI 接受的 `job_timeout_sec` 不能限制 Job 总运行时间，环境构建/setup 的声明期限也未按用户值执行。
- **修复/复验：** 将 Job 期限接入现有 Supervisor，并按固定 Harbor API 映射 build/setup 秒数；不能执行的参数创建时拒绝或明确标未执行。用可控阻塞 fixture 验证期限触发、中断、部分证据与清理，不能只断言 snapshot 有值。对应 T03/T07。

### R08 — Trial 详情接口直接返回含合成凭据的错误消息

- **定位：** `apps/api/app/main.py:2171`；`packages/sdk-python/motte_sdk/terminalbench.py:454`；Parser `harbor/parser.py:721`。
- **复现：** 将带合成 `OPENAI_API_KEY=sk-review-…` 哨兵的错误保存为合法 TrialResult，经 GET `/api/v1/runs/{run_id}/trials/{trial_id}` 返回原样内容。没有读取或使用真实凭据。
- **原因/影响：** Parser 保留原始 exception_message，新公共详情路由直接 `return detail`，未复用已有展示脱敏。上游错误中的环境变量/认证信息可进入 API 与 UI。
- **修复/复验：** 在公共展示边界对错误、日志等字段复用脱敏；保留身份/hash 的完整性。用合成秘密覆盖 API、CLI 与 UI 消费的字段，不能用改写 task_key/hash 的方式脱敏。对应 T03/T09。

### R09 — 改变重复数、超时与资源仍被判断为可比

- **定位：** `packages/evaluators/motte_eval/comparison.py:36`；新增消费入口 `apps/web/src/evalTypes/terminalbench/TerminalBenchPages.tsx:1483`；SDK `terminalbench.py:254`。
- **复现：** 真实 `build_run_inputs` 构造相同任务的两份 manifest：baseline 为 `n_trials=1/agent_sec=5/memory_mb=256`，candidate 为 `3/105/1280`，policy 只允许 model 变化。结果仍 `eligible=True`、`metric_eligibility.quality=True`，只提示成本未知。
- **原因/影响：** 原比较器的不变量缺少 M3 的 Agent/native 参数、n_trials、timeouts/resources；environment_digest 还从 profile 读取，而 TB 放在 external 根。不同实验条件的质量差异因此可以被当作模型差异。
- **修复/复验：** 为 TB 接入完整冻结实验指纹，明确允许变量并拒绝缺失身份，正确读取 environment digest。逐一改变重复、Agent、预算、环境、工具参数，验证产生对应不可比原因。对应 G06/G15、T09。

### R10 — 模型选择未进入 Runner，真实 Agent 路径尚未实现

- **定位：** `packages/benchmark-runtime/motte_benchmark/harbor/config.py:70`；`apps/api/app/main.py:2064` 附近的创建流程；`packages/cli/motte_cli/main.py` 的 `terminal-bench run` 分支。
- **复现/静态：** allowlist 仅 `oracle@1.0.0`，真实 Agent 一律 `HARBOR_AGENT_UNSUPPORTED`。API 校验 published model 后没有将模型配置传入 TB profile；CLI 指定 `--model NONEXISTENT-MODEL` 仍成功 queued。冻结 `external.profile.model={}`。
- **影响：** 用户选择的模型没有驱动评测，当前只有确定性校准能力。兼容文档将真实 Agent 仅记为“等待调用授权”不充分：授权之后还需要实现模型/凭据/Profile 接入。oracle 无模型合理，但 Web 的“oracle 可留空”与 API `MODEL_REQUIRED` 也不一致。
- **修复/复验：** 完成明确的首个真实 Agent 配置与模型/凭据引用映射，离线验证实际原生配置；生产 allowlist 仍须依据兼容验证，不应无验证放开所有 Agent。真实调用继续等待单独授权。修正完成度说明，区分实现缺项与 live 未验收。对应 T03/T09/T10、G17/G18。

## P2：其余必须闭环的反馈

### R11 — 二进制工件仅保存 hash，没有冻结内容

- **定位：** `packages/benchmark-runtime/motte_benchmark/harbor/adapter.py:248`。
- **复现/静态：** 非 UTF-8 文件只登记 size/hash/encoding，字节留在原工作目录；没有写入独立 Artifact，也没有可恢复内容引用，而 bundle 可被标为冻结完成。
- **影响及修复：** 工作目录清理后无法还原证据。按 T06 约定保存二进制 Artifact，bundle 放引用与校验信息；覆盖非 UTF-8 roundtrip、删除工作目录后的恢复，不可用仅有 hash 冒充完整冻结。

### R12 — Harbor 外层配置采集绕过安全读取边界

- **定位：** `packages/benchmark-runtime/motte_benchmark/harbor/adapter.py:181`、`:209`。
- **复现：** 将 work/harbor/config.json 替换为指向临时目录外合成秘密文件的 symlink，采集跟随链接并把其文本加入 bundle。
- **影响及修复：** 内层 Trial 使用 TrustedDir，外层 config/lock/result/plan/location 却用普通 is_file/read_text，缺少同等的锚定、拒绝 symlink 和大小限额。统一受控读取，覆盖外层链接、替换与超限。反例是输出文件被替换后的采集漏洞，不声称本轮执行了远程攻击。

### R13 — Parser 丢弃完整 Task 路径，同 basename 任务无法归属

- **定位：** `packages/benchmark-runtime/motte_benchmark/harbor/parser.py:569`。
- **复现：** 两任务 `a/same`、`b/same` 的原生结果各含不同完整 `task_id.path`，但 Parser 只取 basename。最终都 not_attempted，原结果进入 `HARBOR_TASK_NAME_AMBIGUOUS`。
- **修复/复验：** 首先按冻结根下的规范相对路径/显式映射匹配，唯一 basename 只能作为受控 fallback。完整路径可区分时应保留两任务全部 Trial。对应 A02。

### R14 — first-trial 实际选择了第一个有效 Trial

- **定位：** `packages/evaluators/motte_eval/harbor.py:133`–138。
- **复现：** repeat 0 为 verifier_error、repeat 1 为 reward=1，`task_pass=True` 且 reason 为 first-trial。
- **修复/复验：** 按事前 repeat_index 选择首个计划 Trial，首个无效时返回不可判断；不能先过滤错误再选第一个，否则改变预设聚合政策。覆盖缺失首个、首个错误和首个有效失败。对应 T08、G15。

### R15 — 全部成本未知时每成功成本仍返回 0

- **定位：** `packages/evaluators/motte_eval/harbor.py:285`。
- **复现：** 所有 Trial 的 cost_usd=None 且有成功 Trial，结果为 `known_cost_usd=None/unknown_cost=True/per_success_usd=0.0`。
- **修复/复验：** 无已知成本不能生成零成本比率；混合未知成本时应返回 unknown，或明确另命名为已知费用小计比率并声明覆盖，避免呈现完整单位成本。覆盖全部未知、部分未知、全部已知和零成功。对应 A12。

### R16 — Web 单 Trial 超时字段被 API 静默忽略

- **定位：** `apps/web/src/evalTypes/terminalbench/TerminalBenchPages.tsx:376`；`apps/api/app/main.py:1939`。
- **复现：** Web 发 `timeout_sec:7`；API 仅读 `agent_timeout_sec`。前者 POST 返回 202 但原生 agents 没有 timeout override；后者才产生 `override_timeout_sec=7`。
- **修复/复验：** 统一公共 DTO 字段并拒绝/校验未知参数；用 Web 实际提交 body 通过 API 后检查 Runner 配置。与 R07 的执行期限缺失是独立问题。

### R17 — CLI status 丢掉冻结计划，覆盖率与聚合规则和 API 不一致

- **定位：** `packages/cli/motte_cli/main.py:362`。
- **复现：** 同一临时库冻结 2 Task × 2 Trial、mean-success，只有一个 Trial 有结果。API 为 `mean-success/2 Task/4 Trial/coverage=.25`；CLI 为 `first-trial/1 Task/1 Trial/coverage=1.0`。
- **修复/复验：** CLI 读取 Run 并把冻结 manifest 传入 task_rows，与 API 一致。缺结果 Task 不能从计划分母消失；验证 CLI/API/report 同 Run 同分母。

### R18 — 比较页配置的 Gate 指标没有后端实现

- **定位：** `apps/web/src/evalTypes/terminalbench/TerminalBenchPages.tsx:139`；`packages/evaluators/motte_eval/gates.py:26`；`packages/sdk-python/motte_sdk/comparisons.py` 的 candidate_summary。
- **复现：** 新 policy 使用 `valid_trial_pass_rate`，registry 仅有旧 accuracy/cost.total_usd。即便 coverage=1、通过率=1、eligible=True，Gate 仍报 `unsupported metric: 'valid_trial_pass_rate'`。
- **修复/复验：** 注册并从 TB ScoreSet/Trial 计划生成对应 summary 与覆盖单位，保留共用 Gate，不另造实现。用公共 compare/gate 入口验证真正可通过和必须拒绝的样例。

### R19 — Terminal、轨迹和工件详情没有真实内容读取链路

- **定位：** `packages/sdk-python/motte_sdk/terminalbench.py:460`；`apps/web/src/evalTypes/terminalbench/TerminalBenchPages.tsx:1103`、`:995`。
- **复现/静态：** 真实 API 给 terminal_ref，不给 terminal_text；Web 只读取 terminal_text，故即使有引用仍显示终端文本不可用。ArtifactViewer 只展开 metadata，没有内容请求、下载链接、轨迹或 diff 展示；现有 Case Observation content 路径未接 Trial 引用。
- **修复/复验：** 接通经过 Run/Trial 归属、hash 与脱敏校验的 Artifact 读取及 UI；fixture 应来自真实 DTO，不能手填不存在的 terminal_text。验证切换 Trial、迟到响应、截断/二进制/缺失内容。对应 T09、G16。

### R20 — 无效创建参数返回 500，preflight 还能放行不支持的 Agent

- **定位：** `apps/api/app/main.py:1936`、`:2064`、`:2125`。
- **复现：** 数据准备、模型与 probe 均有效时，POST 分别传不支持 agent_id、`n_trials:"oops"`、不存在 task_key，均返回 500；GET preflight 对不支持 agent_id 返回 ok=true。
- **修复/复验：** 公共请求做结构化类型/选择/profile 校验，统一成可解释的 4xx；preflight 与真正创建使用同一能力校验。每个拒绝分支确认零 Run/Job 启动。

### R21 — Run 概览成本卡只汇总当前 Task

- **定位：** `apps/web/src/evalTypes/terminalbench/TerminalBenchPages.tsx:1307`、`:1358`。
- **静态：** 全 Run 质量概览旁的成本卡使用当前选择 Task 的 trials/details。切换 Task 改变“已知成本/每成功成本”，其余 Task 费用不在该卡中。
- **修复/复验：** Run 卡消费全 Run aggregate.cost；Task 成本如需展示，明确标注范围。用两 Task 不同费用验证切换不会改变 Run 总额。本轮未做浏览器交互复验。

### R22 — PG create_plans 并发首写不能满足幂等契约

- **定位：** `packages/storage/motte_storage/pg_audit_store.py:1024`–1031。
- **静态 SQL 结论：** SELECT 无记录后直接 INSERT，没有冲突处理。两个事务都读到空时，后一个 INSERT 会 UniqueViolation，而不是 identical/conflict，并回滚事务。现有“两连接”测试没有制造同时首写。
- **修复/复验：** 采用原子插入冲突处理后核对冻结内容；用真实 PG 两连接同步屏障验证相同/不同 payload 竞争。**本轮未连接 PG，未把这个场景记为实测通过或失败。** 对应 T02。

### R23 — Migration downgrade 静默删除 Trial 证据

- **定位：** `migrations/versions/0008_trials.py:38`–42。
- **静态：** 无条件 DROP trials 和 attempts.trial_id，不检查是否有数据。与 T02 明确要求“降级不能静默丢 Trial，记录拒绝条件”不符。
- **修复/复验：** 有 Trial/关联数据时拒绝普通 downgrade 并给出迁移说明；只在满足明确安全条件时允许。用一次性库验证旧库升级、空库降级和有数据拒绝。本轮没有对用户数据库执行降级。

### R24 — Attempt transition 可以改写 trial_id，绕过 Task 写入保护

- **定位：** `packages/storage/motte_storage/integrity.py:89`–92；新增保护 `audit_store.py:198`。
- **内部接口复现：** Memory/SQLite 创建 trial_id=t1 的 attempt，transition 将 trial_id 改成空字符串，随后 complete(case_run=...) 可以写入 Task Case，绕过 trial-scoped attempt 禁止写 Case 的保护。SQL payload 与索引列还可能不一致。
- **修复/复验：** trial_id 应与 run/case/attempt identity 一样不可变，校验其归属；三个存储后端验证 transition/complete 都拒绝修改。当前 SDK 未观察到主动传这种 changes，反馈针对新增持久化契约。

### R25 — Memory create_plans 返回内部引用，冻结计划可被调用者修改

- **定位：** `packages/storage/motte_storage/trials.py:155`–159；冲突响应也需检查。
- **内部接口复现：** 修改 `create_plans` 返回值的 `result[0]["plan"]["plan"]["repeat_index"]`，再次 get 得到已变化内容；重复提交原计划仍可因为缓存 plan_hash 返回 identical。
- **修复/复验：** 所有 Memory repository 出口深拷贝，检查 created/identical/conflict 分支；改变返回对象不得影响已冻结记录及其 hash。SQLite/PG 序列化不会产生同一引用问题。

### R26 — put_result 不校验 payload 身份与目标 Trial 一致

- **定位：** `packages/storage/motte_storage/trials.py:183`、`:290`；PG 对应 put_result。
- **内部接口复现：** `put_result("t1", result(trial_id="t2", ...))` 返回 stored，t1 内永久保存 t2 的结果；同类缺口涉及 run/task/repeat 归属。Memory/SQLite 已复现，PG 同类路径静态确认。
- **修复/复验：** 写入前逐项匹配目标冻结计划与 TrialResult 身份，错配明确拒绝且不改变旧记录。当前 SDK 正常传参不能代替仓库约束，尤其重启/批量导入场景。对应 T02、G05。

## 建议修复顺序与下一轮门禁

1. **执行与身份：** R01、R02、R03、R04、R05、R07、R10。先保证真正执行受控的冻结任务、保存全部 Trial、具备准确停止与恢复语义。
2. **评分与比较：** R06、R09、R13、R14、R15、R18。使用 2 Task × 3 Trial（成功/有效失败/Verifier 错误）的公共服务贯通反例，检查 ScoreSet、rescore、分母及 Gate。
3. **证据与产品：** R08、R11、R12、R16、R17、R19、R20、R21。用真实 API DTO 联调 Web/CLI，验证工件内容、脱敏、预算和全 Run 汇总。
4. **存储：** R22–R26。内部契约先离线回归，再登记实际 PG 的并发/migration 证据。

每项反馈保留“失败反例 → 修复 → 同反例通过”的记录；所有业务改动按仓库要求跑 focused 回归与 `make check`。涉及 Web 时遵守 DESIGN.md 并运行 Web test/build。真实模型调用仍单独授权；修复代码、离线配置验证和回归测试无需等待该授权。

下一轮优先 review 正式公共组合路径，而非仅新增 helper 单测数量。只有代码反馈闭环后才能给出“代码 review 通过”；真实 PG、Docker 清理/取消、固定真实 Agent 小批次的验收状态单独报告。当前不建议开始 M4。
