# 实验与比较协议（M6）

状态：**authoritative（T00 冻结版）**。本文是 M6 所有实现必须遵守的身份层级、真值表与
hash 规则。实现与其冲突时，以本文为准并修复实现；修订本文必须随代码同一提交。

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
`judged + call_failed + unknown + needs_review = attempted`（按套件映射）。
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

## 11. 退出与回退

回退顺序：停 Experiment 新建/领取与新 GatePolicy 发布 → 确认自有活动进程停止 →
保留全部 Experiment/Cell/Run/Trial/ScoreSet/ScoringPass/Baseline/GateResult/
ReportSnapshot/Artifact pin 与不确定记录。新算法/阈值/统计实现 = 新版本，不重写
原 policy 或历史结论。
