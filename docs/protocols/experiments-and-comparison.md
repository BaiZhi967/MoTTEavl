# 实验与比较协议（M6）

状态：**authoritative（T00 冻结版）**。本文是 M6 所有实现必须遵守的身份层级、真值表与
hash 规则。实现与其冲突时，以本文为准并修复实现；修订本文必须随代码同一提交。

> M8 实现范围（2026-09-23）：Direct LLM 和 GSM8K Experiment 只接受
> `model_profile` / `reasoning_level`；Agent Tasks 固定 `legacy-json` 模式，
> 只接受 `model_profile`。其他因子在 preview/create 拒绝。
> controlled_conditions 当前仅接受正整数 `max_output_tokens`；GSM8K 的
> suite 预设固定为 1024，实验若显式给出其他值，preview/create 在持久化前
> 一致拒绝。Direct LLM 与受限 Agent Tasks 可使用该运行级上限。标量
> `parameters` 无法组成请求参数，必须拒绝而非静默丢弃。
> `max_total_tokens`、`max_cost_usd`、非默认停止政策及非默认 scoring 当前无
> 实验级强制消费者，同样拒绝。`max_total_calls` 根据固定题集与已解析 retry
> 上界预检；Agent Tasks 按冻结的每 Case `max_steps` 乘以题数、Cell 数和
> Provider retry 上界，不能沿用单次调用预算。Direct/GSM8K 比较页从固定 ReportSnapshot 和 ComparisonService
> 读取质量与可比性；成本按币种分列，部分未知保留 unknown 数。此范围说明
> 不改变下文目标协议；C-Eval、Scenario/Skill、Harbor 的实验装配仍未实现。
> 固定 Pass 的 Task 内 Trial pass@k 已接入比较/JSON 导出消费者；显式不可变
> 统计报告发布与读取/导出见 §10，live 独立性仍未验收。证据见
> `docs/verification/M8.md`。

M6 是**结果治理层**：只消费已持久化的 Run / Trial / Observation / ScoreSet /
ScoringPass / Artifact 引用。compare / report / gate / history / export 一律只读，
零 Provider / Judge / Runner / 业务工具调用。Experiment 只编排既有
RunDispatcher / Worker / CaseAttempt / ScoringPass，不建立第二套调度器、评分器、
比较服务、Gate 引擎或 current 指针。

## 1. 身份层级（冻结）

| 对象 | 身份 | 说明 |
|---|---|---|
| Experiment | `experiment_id@version` | 发布后不可变；新内容 = 新 version |
| Experiment repeat | `(experiment_id, version, repeat_index)` | 一次重复产生独立 Cell/Run；**不等于** Harbor Trial repeat |
| ExperimentCell | `cell_id = sha256(experiment_id, version, canonical(factor_assignment), repeat_index)` | 同实验同配置同 repeat 只分配一次；cell 拥有恰好一个 initial Run |
| Run | `run_id`（既有） | 唯一执行主权归属 RunDispatcher/Worker |
| Case | `(dataset source/revision, stable_case_key)` | 比较按内容 hash 对齐，不按名字 |
| Trial | `trial_id`（M3，Run 内） | Harbor Trial 只在 Run 内聚合；不跨 Run 计数 |
| transport retry | 传输层重试 | 不是实验样本，不计入 Trial n |
| operator retry / superseding 子 Run | `supersedes_run_id` 关联的新 Run | 原结果不消失；正式报告默认不挑最好结果 |
| ScoringPass | `scoring_pass_id`（既有，不可变） | 一切正式结果引用具体 pass，不追随 current |
| ReportSnapshot | `snapshot_id = sha256(RunReportRef + 内容)` | 固定报告的冻结视图（§5） |
| BaselineSnapshot | `baseline_id`（不可变） | 固定 RunReportRef 集合 + policy hash |
| GatePolicyVersion | `policy_id@version`（不可变） | 规则集合与语义的版本化载体 |
| GateResult | `gate_result_id = sha256(input_hash + semantics_hash)` | 重复求值产生等价结论，不改写原结果 |

计费与抽样层级：experiment repeat、Harbor Trial、Judge repeat 的费用和样本**分别**
累计显示，不重复求和，不互相替代。

## 2. Case disposition 真值表（冻结）

互斥 disposition（一个 Case 在一份固定报告里恰属一类）：

| disposition | 含义 | 进入质量分母？ | 覆盖计数 |
|---|---|---|---|
| `selected` | 被该 Run 选中（总量基线，非终分类） | — | 分母基线 |
| `attempted` | 已发起执行 | 按指标 denominator_policy | attempted |
| `judged` / `scored` | 已评分（judged=进入分母判定；scored=其中通过） | 是 | judged/scored |
| `call_failed` | 调用失败（含 provider 错误） | 否（保持可见） | attempted 内 |
| `unknown` | 结果未知（服务端无结论，不是成功） | 否 | attempted 内 |
| `not_attempted` | 选中但未尝试（停止/预算后剩余） | 否 | not_attempted |
| `needs_review` | 结果不确定待人工复核 | 否 | attempted 内 |
| `no_expectation` | 无期望答案（Direct LLM） | 不进质量分母；进覆盖分母 | judged 外 |

不变量：`attempted + not_attempted = selected`；
`judged + call_failed + unknown + needs_review + no_expectation = attempted`
（no_expectation 的 case 已执行——模型被调用、费用已发生——计入 attempted；
"judged 外"只指**质量分母资格**，不是未尝试）。
缺失/未知/失败**永不**从分母静默删除；空分母、NaN、Infinity、null 不产生 pass，
也不折算成 0。每个 metric 输出 `eligible_count` / `missing_count`。

## 3. 比较结论（冻结）

| level | 判定 | 说明 |
|---|---|---|
| `comparable` | 无结构性阻断原因 | 质量与成本指标均资格完整 |
| `partially_comparable` | 结构可比，但部分 metric 资格不足（典型：仅 cost unknown） | 列出可比 metric 与样本范围 |
| `not_comparable` | 存在结构性阻断（样本集/内容/gold/scorer/Judge/rubric/runtime/权限/预算/干预等未被政策允许的变化） | 不给任何质量结论 |

结构性原因 vs 指标原因分开记录。`eligible`（兼容字段）= 质量指标可比
（level ∈ {comparable, partially_comparable} 且 quality metric eligible）。
诊断比较可显示共同子集结果；**正式 Gate 不接受事后筛出的共同子集冒充完整选中集**。

执行指纹（复现用，全配置）与比较签名（比较用，控制条件 + 允许变量）是两个东西。
允许变量必须来自版本化 `ComparisonPolicy.allowed_factors`（见
`motte_contracts.comparison.ALLOWED_COMPARISON_FACTORS`，无"忽略全部差异"通配符）。
未知 policy 字段 fail-closed（Pydantic `extra=forbid`）。

## 4. Baseline 资格（冻结）

| 资格 | 条件 | 用途 |
|---|---|---|
| `formal` | 完整 ReportSnapshot + 固定 ScoringPass + 证据 pin + 合格 scorer/Judge（已校准）/无未决 needs_review | 可作正式 Gate baseline |
| `diagnostic` | 证据不完整但固定引用完整（如 needs_review Run、未校准 Judge、历史缺字段） | 只能诊断比较 |
| `ineligible` | 引用不完整（缺 pass / 缺 snapshot） | 拒绝创建 |

BaselineSnapshot 固定 entries（单 Run 或按 cell 条件映射）+ `comparison_policy_hash`
+ `created_by` / `reason`。写入后不可变（同 id 异内容是错误）。默认 baseline 是
**指针操作**（scope → snapshot_id，CAS `expected_current`，审计 operator/reason/
policy hash），重评分不自动移动它；current pass 漂移不改变既有 baseline 内容。

## 5. ReportSnapshot 与 evidence pin（冻结）

`ReportSnapshot` 是固定 RunReportRef 的冻结视图，包含：case dispositions、
metric values（带 registry 版本）、coverage、cost summary（多币种/未知分开）、
报告 schema 版本、evidence pins（关键 artifact 的 `artifact_id + sha256`）。
内容 hash = canonical JSON sha256（§8）。snapshot 不可变；工件被删除或历史导入
缺字段时，**新**计算的资格降低，已持久 snapshot/gate/comparison bytes 与 hash 不变。

## 6. Gate 规则 → 决策（冻结）

六类顶层 decision：`pass` / `quality_fail` / `insufficient_evidence` /
`not_comparable` / `execution_error` / `safety_block`。

规则 kind（首批）：`metric_threshold`（绝对阈值）、`baseline_delta`（相对基线最大
退化）、`critical_case`（关键样本必过）、`coverage`（最低覆盖/样本数）、
`model_identity`（实际模型身份）、`cost` / `latency`（费用/时长）、
`no_side_effect`（禁止副作用）、`safety_marker`（安全标记）。

每条规则：`rule_id`、`kind`、`metric_id@version`、`operator`、`threshold`、
`baseline_delta_policy`、`min_samples` / `min_coverage`、`required_comparability`、
`missing_policy`（`fail_closed` 默认；`diagnostic_skip` 仅诊断模式且不能把整体变
pass）、`severity`（`block` / `warn`）、`evidence_requirements`。

语义细则：

- `direction` 取 metric registry 声明（gte/lte），规则 operator 必须与其一致或拒绝。
- `baseline=0`（基线指标为 0）时相对退化规则按"绝对阈值 0"处理并显式记录。
- partial comparability：`required_comparability="comparable"` 时降为不可比；
  `"partial"` 允许但逐 metric 资格仍要满足。
- `execution_error`（Run failed / needs_review 且规则声明需要执行成功）不覆盖已有
  分数，但阻断正式放行。
- 未校准 Judge（无 ≥30 人工校准）或 M4 runtime identity unknown 的规则保持
  `experimental`，正式门禁 fail-closed。
- Run completed 只代表执行终态，不代表质量通过（A17）。
- Gate 不自动触发重评分/补跑；只返回建议动作与所需授权。

决策优先级（同时出现多种失败时）：`safety_block` > `execution_error` >
`insufficient_evidence` / `not_comparable` > `quality_fail` > `pass`。
JSON 输出**永远保留全部 rule results**；退出码只是稳定摘要。

## 7. 决策 → 机器输出（冻结）

| decision | CLI 退出码 | JUnit testcase |
|---|---|---|
| pass | 0 | 通过 |
| quality_fail | 1 | failure |
| （请求/配置不合法，求值前） | 2 | error（不产出 testcase） |
| execution_error | 3 | error |
| （用户显式取消） | 4 | — |
| insufficient_evidence / not_comparable | 5 | error |
| safety_block | 6 | error |

求值后多问题按上表优先级选择进程退出码；JSON/JUnit 保留全部问题。
`gate evaluate` 只求值固定引用；M7 的 `run-and-gate` 才负责自动启动 Experiment。
export / refresh / GET / history / compare / gate 的模型 / Provider / Judge /
Runner / task-start 计数必须为 0。

## 8. Canonical serialization 与 hash（冻结）

- Canonical JSON：`json.dumps(obj, ensure_ascii=False, sort_keys=True,
  separators=(",", ":"))`，UTF-8 编码。
- Hash：`sha256` 十六进制，前缀 `"sha256:"`。
- `cell_id` / `snapshot_id` / `policy hash` / `evaluation_input_hash` /
  `result_semantics_hash` 全部用上述规则。
- `evaluation_input_hash` = sha256(candidate RunReportRefs + baseline refs +
  policy content hash + statistical policy ref)。`evaluated_at` 不进入。
- `result_semantics_hash` = sha256(Gate 引擎版本 + 规则语义 registry 版本 +
  statistical policy 版本)。同 input hash + 同 semantics hash ⇒ 同 decision 与
  rule results（结论等价性；`evaluated_at` 只是审计字段）。
- 未知字段策略：所有 policy/spec 契约 Pydantic `extra=forbid`，默认 fail closed。

## 9. Policy 生命周期（冻结）

`draft` → `published`（不可变，CAS 发布，同 id@version 异内容冲突）→ `deprecated`
（保留历史读取，不参与新求值的默认选择，但已持久 GateResult 引用不受影响）。
ComparisonPolicy / GatePolicyVersion / StatisticalPolicy / BaselineSnapshot /
ExperimentSpec 同一规则：发布后不可变，新内容 = 新版本。

## 10. 统计政策 statistical_policy@1（冻结要点）

- 统计单位：Task（比较/配对）；Trial 是 Task 内样本；bootstrap 以 Task 为重采样
  单元，同 Task 的 Trial 组保持完整。
- 配对 bootstrap：默认 seed `20260921`、2000 次重采样、95% 百分位区间；参数进入
  policy，可覆盖但必须记录。小样本（<2 Task）或无配对资格：只给原始差值，区间标
  `not_applicable`。
- 分位数：线性插值（numpy `linear` 语义，纯 Python 实现）；均值/中位数/分位数带
  参数与输入引用。
- 二元区间：正态近似 Wald 区间仅用于独立二元样本；关联 Task 不沿用同一假设。
- Decimal 语义：成本用 Decimal 计算，比率用 float，round 到 6 位。
- pass@k = `1 - C(n-c, k) / C(n, k)`；仅当 n 个**事前计划、独立、有效完整**的
  Trial（transport/operator retry 不计）且 1≤k≤n；k>n / 计划不足 / 含未知 Trial /
  违反独立条件 → `not_applicable`（不是 0）。验收样例 n=5, c=2, k=2 → 0.7。
- 不提供未事前定义的加权总排名；综合分必须有独立版本化政策。

M8 当前只读消费者：`ComparisonService.paired_statistics`、
`GET /api/v1/comparisons/statistics`、`motte compare --statistics` 和通用 Web 比较页。
输入是两个固定 Run/ScoringPass 引用及允许变化因子；输出保留固定引用、
`statistical_policy@1` 的内容 hash、实现版本、Task/Case 单位、bootstrap
seed/迭代次数、选中/完整/缺失对数、差值与区间资格。一个 Case 只贡献一个配对
Task；缺失、失败或不确定 Case 使区间不适用，不能按完成样本重新缩小分母。
Terminal-Bench 的 Trial 由本消费者按冻结 TrialPlan 在 Task 内聚合。`k` 是显式输入
（缺省 1）；仅唯一的事前计划 Trial ID/repeat、所选 Pass 的完整有效评分行，且未
复用非空 `source_trial_id` 时才计算各 Task pass@k。未计划评分行单列排除，
transport/operator retry 不增加 n；缺失、无效、重复上游 Trial 或 k 超出计划数
返回具名不适用。完整 Task 的 pass@k 才进入 Task 配对 bootstrap；结果带固定
RunReportRef、政策 hash、单位、seed、次数、k 与每 Task 资格。CLI JSON 文件是
固定输入的可导出比较结果；显式持久发布另用下述 statistical-reports 接口，
动态比较不自动发布。离线软件路径不代表完整 T06 或 live 统计验收。
一次比较/统计读取在入口固定两个 ScoringPass ID，后续资格、引用、成本和 Trial
聚合只用这些 ID；HTTP 比较响应返回实际固定引用，Web 统计请求复用该引用。
若比较响应缺引用，Web 显示统计不可用，不再重新解析可漂移的 current 指针。


### 10.1 不可变 StatisticalReport 显式发布（2026-09-30）

`POST /api/v1/statistical-reports` 仅接受 `baseline_run_id`、`candidate_run_id`、
`allowed_factors`（默认 `["model"]`，排序去重）、可选 `baseline_pass_id` /
`candidate_pass_id`（缺省在发布时固定 current），以及严格正整数 `k`（默认 1）。
空白 ID / factor、未知因子、Boolean / 字符串 / 小数 k 与额外字段均拒绝；
客户端不能提供 body、result、policy、report_id 或 published_at，也没有任意正文导入。
服务器在维护互斥窗口内调用现有固定 Pass 计算一次，不执行模型、Judge、Runner 或 Job。
政策保持 `statistical_policy@1`、seed `20260921`、iterations `2000`、confidence
`0.95`、implementation `motte_eval.statistics@1`，此接口不提供政策覆盖。

返回 envelope 恰为 `{report_id, published_at, body}`，body 恰为
`{schema_version: 1, policy, result}`。result 完整保留 `inputs` / `input_digest`、
固定 refs、Trial 资格、subject 描述量、缺失计数、unit/k、算法版本与结果。
`report_id = "stat-report-" + canonical_hash(body).removeprefix("sha256:")`；
strict finite JSON 验证后保存 canonical TEXT，避免数据库数值归一化改变身份。
发布时间由首次成功插入产生（UTC RFC3339），与 ID 一起位于哈希正文之外。
同正文重放返回原始时间与 envelope；首次与重放均 HTTP 200，异正文同 ID 冲突。

`GET /api/v1/statistical-reports/{report_id}?format=json|junit` 只读已存正文，
先校验身份、policy 与 input digest 绑定，不解析 current、不读取实时计量、不重算。
JSON 返回完整 envelope；JUnit 复用统计导出器的适用性语义，system-out 保存
同一完整 envelope，原有 properties 加上 report_id、schema_version、published_at。
不可用区间仍 skipped，绝不产生质量 Gate 断言；安装政策变化不改写历史导出。
没有公共 list / PUT / PATCH / DELETE 接口。

错误：请求/格式/Pass 归属错误 422；Run、Pass（含缺失 current）或报告不存在 404；
不可变冲突 409 (`STATISTICAL_REPORT_CONFLICT`)；存储损坏 409
(`STATISTICAL_REPORT_CORRUPT`)；维护阻止发布 503 (`MAINTENANCE_MODE`)，读取仍可用。
报告及其 Run / Pass / Artifact 证据进入统一保护闭包与备份恢复校验；不提供删除接口。

## 11. 退出与回退

回退顺序：停 Experiment 新建/领取与新 GatePolicy 发布 → 确认自有活动进程停止 →
保留全部 Experiment/Cell/Run/Trial/ScoreSet/ScoringPass/Baseline/GateResult/
ReportSnapshot/Artifact pin 与不确定记录。新算法/阈值/统计实现 = 新版本，不重写
原 policy 或历史结论。

## M8 预检绑定与恢复补充（2026-09-30）

`preview` 返回 canonical `preview_hash`，覆盖规范化 Spec 与每个 Cell 实际解析的
manifest、case IDs、requested manifest。API 创建体的 `_preview_hash`、SDK
`experiment_create(..., preview_hash=...)`、本地/远程 CLI 的 `--preview-hash` 可绑定该预览。
首次创建时合法但不同的资源也会触发 `PREVIEW_STALE`（HTTP 409），且不创建 Spec/Cell/Run。
Web 创建沿用刚预览的 hash。未带 hash 的兼容调用仍重新预检，回执明确
`preflight_mode=create_revalidated`，不能宣称绑定了用户先前看到的预览。

新 Spec 与整批含 `prepared_run` 的 Cell 在同一存储事务内发布；之后分配仅消费该冻结输入，
不再次解析可变资源。事务中途失败不留下半个矩阵；成功发布后的进程重建可在资源仓库不可用时
恢复原矩阵。幂等重放核对完整 Cell ID 集合并返回原 hash/模式；不能拿新预览给旧 Run 重新背书。
历史未冻结 Cell 仍标 `legacy_revalidated`，历史缺失 Cell 矩阵明确报 `EXPERIMENT_INCOMPLETE`，
不静默混用新旧快照。实验外部来源和执行时资源能力仍需各自验收；hash 不是授权或实时环境保证。

Agent suite 额外接受固定控制条件 `agent_mode=legacy-json|native-tool`（默认 legacy-json）；
其他 suite 拒绝该条件。native-tool 沿用 standalone 的 prompt、工具、Agent budget 冻结链，
仍只接受 model_profile 实验因子；这不代表任意外部 Runtime 已装配。

### M8 subject 描述统计与自包含导出补充（2026-09-30）

`paired_statistics` 现在同时返回 `k`、`descriptive`、`inputs` 与 `input_digest`。
`descriptive.baseline/candidate` 只读取各自选中 Case 的 `result.cost` 与
`result.metering.latency_ms`：一个 Case 一个观测，Terminal-Bench 中一个选中
Task 一个 Case-result 观测。成本按显式币种分别给出 count/mean/median/p10/p90/
min/max/missing，不跨币种相加或换汇；缺币种、非有限值、负数与缺测仍计入
unknown/missing，不补成 USD 或零。延迟按毫秒给出 count/mean/p50/p90/missing。
这些数据仅为 subject 描述统计，不合并 ScoringPass 的 Judge 费用、Judge 延迟，
不把 transport retry 当作独立样本，也不从 Trial 分数推测缺失的 Task 计量。

RunReportRef 的 evidence hash 不包含上述 Case 计量，因此 `inputs` 额外冻结
实际采用的 subject 成本/延迟字段、两个固定引用、allowed_factors 与 k，
`input_digest` 是该对象的 canonical hash。NaN/Infinity 在冻结输入中以具名标记
保留，在描述数值中算 missing，整个结果可严格 JSON 序列化。现有结果可在不读
current、计量存储或执行模型调用的前提下导出；若底层计量事实后来改变，重新
求值会得到不同 digest，不应将相同 Run/Pass 引用误认为完全相同的统计输入。

`statistics_to_json` 保留完整 canonical 对象；`statistics_to_junit` 在 properties
记录固定引用、input_digest、政策、k、缺失数、单位、seed 与迭代次数，并在
system-out 保留完整同一 JSON。唯一 testcase 表达配对区间的适用资格，
`applicable=false` 映射 skipped，绝不伪装成质量通过；它不是质量 Gate 或排名。
动态结果的自包含导出本身不写入报告；要保存长期可复现的正文，必须显式发布。

## Cell 显式重试的冻结输入（2026-09-30）

retry_cell 从初始 Run 读取已冻结的 executable manifest、ordered case_ids 与 requested_manifest，
创建新的 superseding Run；不从当前资源重新解析 selector，也不重新选择 Cases。
这同样适用于没有 prepared_run 字段的历史 Cell。原 Run 缺失时拒绝创建，不能以空 Cases
生成替代记录。新 Run 的执行身份通过既有 refreeze helper 派生，保留原 Run 与父子审计链。
重试继续检查 suite 支持边界，不能通过手写持久 Spec/Cell 绕过未支持外部 Runtime 的拒绝。


### Subject pairwise 的显式角色与历史兼容（Scope B Task 7）

新的 pairwise 预检／提交仅接受保存的 attempt 引用。每条 `pairwise_refs` 必须携带
`case_id`、`candidate_a_attempt_id`、`candidate_b_attempt_id` 和必填的
`challenger_attempt_id`；后者必须恰好指向这两个不同、同 Run／Case 的终态 attempt
之一，另一方为 reference。每个选中的 Case 只允许一个 pair，重复、反转后的额外
副本或同 Case 的另一组 pair 都在调用／持久化前拒绝。未选 Case、跨归属、开放
attempt、缺失／损坏 FrozenObservation 也拒绝。内容只能来自保存的 attempt，不能
来自当前 CaseRun 或客户端正文。

服务器生成 `PairwiseRoleBinding`，其中 `candidate_input_sha256` 对每个完整候选输入
（候选身份、正文、证据白名单等）的规范 JSON 取摘要。角色列表按 Case／pair 固定
排序，其 `roles_sha256` 进入 subject Job 的 fingerprint，并在 Job、每次冻结 call
plan、Invocation request summary、最终 Pass 和 receipt 中保存。每条 plan 的
`role_binding` 在重复／正反展示调用中完全相同。A/B、字典序及当前设置均不决定
challenger；改变显式 challenger 会改变角色及 Job 身份，展示顺序只改变相应调用
输入／计划身份。Worker 重启后仅消费保存的角色与输入，不重新选择模型或 attempt。
新建的内部 subject Job 也必须核验完整角色与保存的终态 attempt 输入；calibration
owner 使用独立、已核验的 calibration/sample 命名空间，不借用 subject 角色。

历史缺角色 Job／Pass 的 GET 保持可读；没有补默认字段或重写历史 fingerprint。
缺角色的旧 HTTP body 在新的严格公开契约下返回 422，需要显式角色和新请求键
重新提交。内部固定输入的原 Job 精确重放只返回原 Job／receipt，零新调用、零角色
补写；修改输入或为旧 Job 补角色产生冲突。排队／prepared 的缺角色 subject Job
不会发出新调用，记录 `PAIRWISE_ROLES_REQUIRED` 并要求显式重新提交。部分已结算
的历史 dispatch 恢复保留原账本及不确定状态，另记录相同的角色恢复原因；不重发。
只有 Job／receipt、plan、call、关联 Invocation 身份元数据及 submission 引用中
完全不存在新角色标记，并且保存字段按旧规范公式计算的 fingerprint 与原值完全
一致，才适用旧格式例外；仅删可见字段不能沿用新角色 fingerprint，显式 null 也
不算缺席。缺顶层 roles 却
保留角色摘要／绑定或显式 challenger 引用属于矛盾新记录，以
`PAIRWISE_ROLES_INVALID` 拒绝，不能猜角色、补角色或改 fingerprint。
全部计划调用已结算时，可确定性解析／发布原历史结果而不构造 Provider，仍不具有
pairwise quality；已完成结果继续只读重放。Worker 在派发／发布前核对候选与 attempt 的显式映射、保存的 Run／Case 归属，
并用提交时同一规范公式复核角色／计划的 Job fingerprint；重算局部摘要不能沿用
另一角色的原 Job 身份。新记录角色摘要／plan 绑定不一致时以
`PAIRWISE_ROLES_INVALID` 停止，不能派发或发布。

本步骤只冻结测量身份。原 `pairwise_preference` 行仍保持 `passed=None`，不会映射
成 accuracy／cost-per-success 或自动通过 Gate。独立 metric 重建和显式阈值政策分别
由后续 Task 8／9 实现；此处不能声称正向质量 Gate 闭环。

### 固定 subject pairwise 质量重建（Scope B Task 8）

`reconstruct_pairwise_quality(store, scoring_pass_id)` 从所选 Judge Pass、其完成的
subject ScoringJob、完整冻结 plan/roles/spec/provider 及实际 Invocation 原始响应
只读重建。Job 与 Pass 的 owner、调用清单、角色、发布 receipt 和评分身份必须一致；
每条计划 call 及 Invocation 必须唯一、成功结算、相同输入／展示顺序／角色绑定，
Provider 回报身份及唯一响应 ID 可验证，且确定性 parser 为 `ok`、全部 criterion
为 scored。Job.result、summary 数字或编辑后的 Boolean ScoreSet 不是质量证据。
不重新读取 current、模型资源或 attempt，不执行 Provider；calibration owner 不能
作为 subject 质量测量。旧格式角色缺失保持 unavailable，不猜测 challenger。

`pairwise_challenger_score@1` 按稳定 winner identity 给予 challenger=1、reference=0，
只有显式且完整的 tie=1/2。同 pair 的计划调用先作算术平均，再等权平均所有计划 pair；
内部使用 Fraction，公开输出仅在最后转为有限 float，不作显示舍入后再求 Gate。
重复数不均不会改变 pair 权重。缺失、重复、失败、不确定、非 scored 或矛盾证据均使
全局值为 null，保留逐 pair 值／缺失原因、planned/valid pairs、planned/settled calls
及实际覆盖。正确识别的 calibration 非 scored 例外也不能在此视为 tie。

计划分母优先来自 Job 的冻结 plan，并保留所选 Pass 的冻结 `judge.calls` 中独立保存
但已从 Job 丢失的调用身份。若 Job 缺失，仍保留 Pass 可证明曾计划的身份，Job／plan／
ledger 摘要分别为 null 并说明缺失；若两份来源矛盾，则全局值与有效覆盖不可获得。
这些保留项只表达已知缺失，不会把损坏的额外身份提升为可信测量。真正空计划为 null，
不返回零分或完整覆盖。旧 Pass 连 pair 身份也无法恢复时，显式记录
`planned_pair_identities_unavailable`，不能按重复调用数捏造 pair 身份或分母。

Pairwise 来源使用 `PairwiseReportSnapshot`、`report-pairwise-v1` 和
`metric-registry@2`；report ref 绑定 roles、plan、ledger、quality 的内容摘要。
`candidate_summary` 增加同一 `pairwise_quality` JSON 区段、显式版本 metric 值与
所选 `scoring_pass_id`／report schema／registry version。原 Boolean counts／coverage
不改口径，accuracy、judged_accuracy、cost-per-success 仍不可用，原 ScoreSet 行继续
`passed=None`。人工后代的 quality source ID 绑定所选人工 Pass，值不可用且原因为
`manual_pairwise_quality_unsupported`；资格可沿来源验证，但不忽略人工修订以复制数值。

结构比较及旧指标资格保持原义；任一方为 pairwise 时，新增 metric 的比较资格为 false，
原因为 `pairwise_baseline_comparison_unsupported`，双方绝对值仍可读取。当前版本不定义
跨 Run baseline delta、pairwise critical-case、配对统计推断或 pairwise pass@k；统计入口
返回 `pairwise_statistical_inference_unsupported`，case Boolean 结果保持 unknown。
此限制不排除 Task 9 单独实现的显式绝对阈值质量 Gate，也不授予 Judge 校准资格。

### 实际 Judge 模型身份与限定 HTTP 发送边界

校准与 subject pairwise 调用的 Invocation result_summary 和 call.raw_response
同时保存 requested_model、reported_model、resolved_model_identity、identity_evidence、
identity_policy、identity_policy_result、policy_passed。只读重建重新按冻结 Judge model
及明确版本化 alias map 核验这些字段；legacy `model` 只表示请求的模型，不能证明
实际模型身份。缺失/不匹配实际身份、alias 内容或版本不一致均不可取得校准资格或
pairwise quality，两个公开 Gate 都 fail closed。普通 `report_only` 仍允许执行；
执行成功本身不授予资格。重启及当前资源变化不替换已冻结身份，缺证据的旧账本不补写。

所有新建、具有原生硬调用上界的实验（Scenario、Skill、Agent、Direct/GSM）冻结
`provider_transport_policy="bounded-http@1"`。该配置进入 requested/prepared manifest
和 preview binding，实际执行禁止 HTTP redirect，包括保留 POST 的 307/308；每次
HTTPTransport attempt 最多一个客户端发送，显式重试仍计入 `1 + max_retries`。
普通未声明此限定策略的历史/独立运行保留既有同源跳转兼容行为，不能因此获得更强的
历史预算保证。已有 C-Eval 限定 profile 继续使用其固定 no-redirect 传输实现。

### 类型化实验请求与传输控制

preview/create 的 OpenAPI request schema 公开同一判别式 suite_config。未知字段、
错 suite、非法预算等请求返回安全的 422 / `EXPERIMENT_INVALID`，零 Spec/Cell/Run。
`request_key`、旧别名 `_request_key` 与 `_preview_hash` 是传输字段；两种 request key
同时出现时非空 `_request_key` 优先，所有控制字段都不进入 Spec fingerprint 或 preview
身份。local/server CLI 保留对应错误与零写入边界，过期 preview 仍为 409。

Judge 的已授权单次调用也遵守同一发送边界：新 ProviderSnapshot 在 `transport` 中
冻结 `follow_redirects=false` 与整数 `max_retries=0`，两者进入快照摘要。实际
FrozenProviderFactory 对已排队的旧快照也禁用跳转和自动重试，且不修改保存的快照。
通用 standalone Provider 的默认跳转/幂等重试行为保持不变。权威校准与 subject 质量
重建必须看到严格的 false 和整数 0；缺失、字符串、数值 0 代替 false、布尔 false
代替整数 0 都不构成发送上界证明。历史报告及原摘要仍可读取，不自动升级；旧记录
缺少证明时，其实时资格验证与 Gate 使用保持不可用，需要新的明确冻结测量。
