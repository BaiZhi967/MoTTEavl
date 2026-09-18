# M6：实验、可比性、统计与回归门禁详细规划

> 状态：待实施。M6-Lite 随 M2 提前交付，其余工作在 M3/M4/M5 的证据类型可用后收口；不是等到所有执行器完成才开始比较。实施使用测试先行、逐任务评审和小提交，可使用 superpowers:subagent-driven-development 或 superpowers:executing-plans。

**Goal：** 将多个 Run 组织成可复核实验，说明结果是否可比、差异来自哪些样本，并用固定基线、覆盖与安全政策给出可供 CI 消费的质量结论。  
**Architecture：** 只消费已持久化的 Run/ScoringPass/Observation，不重新执行模型或在报告读取时改分。Experiment 只产生现有 Run，Baseline 固定评分版本，Comparison 与 Gate 共享同一指标和证据政策。  
**Tech Stack：** 现有 Python、Pydantic、存储、FastAPI、React/Vite 与 CLI；统计实现选用经核验的库或可独立测试的有限公式，不建立新调度基础设施。  
**Spec：** [总路线](../ROADMAP.md)第 11 节、[共通约束](README.md)、[M1](M1-native-agent-and-evaluation.md)、[M2](M2-llm-benchmarks-and-ceval.md)、[M3 Trial](M3-harbor-and-terminal-bench.md)、[M5](M5-scenarios-skills-and-judges.md)。  
**基线：** MoTTEavl `a668d13ee5ea0c3613648f8992ecaa4a148d3855`；旧 Gate 参考 `b661bcdf83e1c3dfb8d6062ee78817d249e86a4c`。

## 1. 分层交付与现有基础

现有 ScoringPass、ScoreSet 和版本快照作为唯一分数来源。当前通用 RegressionGate 仍是简单阈值判断；旧平台的基线、失败集对比、可比性和证据不足政策可以提取语义，但不复制 ORM 和组织权限层。[B1][B2][B3]

### M6-Lite：T01–T03，随 M2 实施

输入固定的两个 RunReportRef；核对样本与 Profile 条件；正确计算覆盖；绑定一个基线评分版本；按绝对阈值、最低样本数/覆盖和证据条件输出结构化 Gate。M2-T09 直接使用这份实现，不创建独立 C-Eval Gate。

### M6-Full：T04–T11

轻量实验矩阵、多维配置比较、Trial 统计、丰富回归规则、分组基线、统一 UI 与 CI。M1 的 Agent 任务和 M5 的首批 Skill A/B 不必等待 Full；它们先保存正确关联与版本，之后纳入同一实验视图。

非目标：自动为所有 Benchmark 合成万能总分、无限参数搜索、自动调优直到门禁通过、跨组织审批、多租户项目、自动选择“最优模型”并改生产配置，以及在证据不足时制造明确排名。

## 2. 最终具体目标清单

- [ ] M6-G01：ExperimentSpec 固定任务、变量、重复、预算、评分和停止政策。
- [ ] M6-G02：矩阵展开可预览、可校验、可幂等恢复，不重复创建同一实验单元的 Run。
- [ ] M6-G03：Experiment 复用现有 Dispatcher，不建立另一套执行队列。
- [ ] M6-G04：执行指纹与比较条件分开，允许变化维度明确而非任意忽略字段。
- [ ] M6-G05：比较返回 comparable/partially_comparable/not_comparable 与逐项原因。
- [ ] M6-G06：样本按稳定身份和内容对齐，新增/移除/内容变化单独列出。
- [ ] M6-G07：GSM8K、Direct LLM、C-Eval、Harbor 各自合法分母不被统一改写。
- [ ] M6-G08：selected/attempted/judged/unknown/not_attempted 与指标覆盖完整显示。
- [ ] M6-G09：失败和未知样本不会通过静默过滤提高正式门禁结果。
- [ ] M6-G10：统计单位、权重、区间方法、随机种子及参数可追溯。
- [ ] M6-G11：Trial 与传输/恢复重试分开，pass@k 只在声明的实验条件成立时计算。
- [ ] M6-G12：成本、货币、计量来源和未知费用保持区分，不把 null 当 0。
- [ ] M6-G13：Baseline 固定 Run + ScoringPass + ComparisonPolicy，不追随最新评分指针。
- [ ] M6-G14：新失败、修复、持续失败、不稳定和未覆盖样本可逐项查看。
- [ ] M6-G15：GatePolicy 版本化，规则对应的指标、方向、阈值与缺失政策明确。
- [ ] M6-G16：质量失败、证据不足、不可比、执行错误、安全阻断具有不同结果语义。
- [ ] M6-G17：同一报告、基线和政策重复求值产生相同 GateResult，不触发模型调用。
- [ ] M6-G18：人工干预、运行时配置、Judge/rubric 变化影响比较资格。
- [ ] M6-G19：UI 与 CLI 使用同一比较/Gate 服务，不能各自计算不同分数。
- [ ] M6-G20：CI 非零退出原因可机器读取，Run completed 不等于 Gate pass。
- [ ] M6-G21：版本升级、重评分、历史导入和已删除证据不会悄悄改变既有结论。

## 3. 模块与责任划分

| 模块 | 现有触点 / 拟新增路径 | 输入 → 输出 | 实现范围 |
|---|---|---|---|
| 实验契约 | 新增 `packages/contracts/motte_contracts/experiment.py` | 显式任务/变量 → ExperimentSpec/Cell | 类型化矩阵、版本、预算、资源引用 |
| 比较契约 | 新增 `motte_contracts/comparison.py` | RunReportRef/政策 → ComparabilityResult | 固定证据引用、匹配范围、阻断原因 |
| 指标/统计 | `packages/evaluators/motte_eval/aggregate.py`；新增 `statistics.py`、`coverage.py` | 版本化 ScoreSet/Trial → MetricSummary | 按政策聚合、覆盖、配对差异、区间；不重新调用 scorer |
| 比较领域服务 | 新增 `motte_eval/comparison.py` | 固定报告与政策 → ComparisonResult | 条件核对、Case 对齐、metric 资格、差异分类 |
| 门禁领域服务 | 扩展 `motte_eval/gates.py` | 基线/候选报告/政策 → GateResult | 纯规则求值、所有规则明细、退出语义 |
| 实验应用服务 | 新增 `packages/sdk-python/motte_sdk/experiments.py` | 发布 ExperimentSpec →现有 Run 集合 | 幂等分配、预算预检、停止/取消和进度，不重新实现执行 |
| 基线与结果存储 | `packages/storage/`及 migration；新增比较/基线 repository | 不可变实体 →可查询历史 | BaselineSnapshot、GatePolicyVersion、ComparisonResult、GateResult |
| API/CLI | 现有公共入口 | preview/submit/compare/gate/export | 类型化响应、零隐式执行、复用领域算法 |
| Web | 新增 `apps/web/src/pages/experiments/`、`compare/`或复用当前同类目录 | 统一报告 →矩阵、差异、门禁详情 | 引用固定 pass、显示条件与未知，避免私有前端算法 |

路径是拟议落位，已有同类页面/模块优先扩展。统计与 Gate 独立于具体 Benchmark Parser，插件只声明指标定义和合法聚合规则。

## 4. ExperimentSpec 与运行编排

### 4.1 契约

| 对象 | 关键字段 | 不变量 |
|---|---|---|
| ExperimentSpec | id/version、task_ref、selected_case_keys、factors、controlled_conditions、repeats、budget_policy、evaluation_ref、stop_policy | 发布后不变，所有变化维度有名称与版本 |
| ExperimentCell | cell_id、factor_assignment、repeat_index、resolved_spec_hash、run_id、allocation_status | 同实验同配置同 repeat 只分配一次 |
| RunReportRef | run_id、scoring_pass_id、report_schema_version、evidence_hash | 不能只有 run_id 而隐式读取 current pass |
| ComparisonPolicy | id/version、allowed_factors、required_equal_fields、case_policy、identity_policy、evidence_policy、statistical_policy | 不提供通配符“忽略所有差异” |
| BaselineSnapshot | id/version、entries、comparison_policy_hash、created_at、source_note | entries 固定单个或按 cell 映射的 RunReportRef |
| GateResult | policy_ref、baseline_ref、candidate_refs、decision、rule_results、evidence_refs、evaluated_at | 输入引用固定；重复求值不改写原结果 |

### 4.2 矩阵展开

首批因素：ModelProfile、reasoning level、prompt version、runtime version、SkillVersion。变化必须通过相应资源的合法配置范围校验。不能把数据集本身或评分器变更悄悄塞成普通模型比较的允许因素。

创建前 preview 展示 cell 数、Case/Trial 数、最大潜在调用数、已知/未知成本、有效参数与不支持组合。数量超过配置上限或缺必需资源时拒绝，不先排队一部分再才发现错误。

cell_id 建议由 experiment version、规范化 factor assignment、repeat_index 的 hash 生成。分配事务同时写 cell→run 关联和 Run 创建；大矩阵可以分批事务提交，但进度持久化且每个 cell 唯一，恢复不会重复创建已分配 Run。尚未分配与已失败的 cell 分开显示。

实验取消只请求取消自己所属的 Run，不影响其他任务；失败重试产生显式子 Run 与 superseding 关系，原 cell 的初始结果不消失。不得自动反复运行直到取得更好的结果；额外 Trial 是新实验版本或显式补充计划。

单执行器限制保持不变。实验层只编排已有 Run，不绕开锁、CaseAttempt 或 needs_review。

## 5. 可比性与样本匹配

### 5.1 两种指纹

execution_fingerprint 包含全部解析配置，用于复现。comparison_signature 由明确控制条件与允许变化维度产生，用于当前实验问题。模型不同可以是允许变量；sample set、任务内容、隐藏期望、工具权限、评分器、运行预算等是否可变必须由政策说明。

比较任务身份使用 dataset source/revision、stable_case_key、任务输入和期望的内容 hash。prompt 是允许变量时，比较对象仍是同一任务，而不是把渲染后的不同消息 hash 当成不同题目；任务语义变化不能仅靠将 prompt 列为变量来绕过检测。

### 5.2 结果与理由

| 情况 | 默认结论 | 解释要求 |
|---|---|---|
| 同一任务/评分政策，仅声明模型不同 | comparable | 列出变化维度和固定条件 |
| 同集部分样本未执行 | 对可观察子集可作诊断；正式覆盖门禁不足 | 显示选中集合、共同子集、缺失与偏差风险 |
| 样本内容或 gold 改变 | not_comparable，除非明确版本映射支持某指标 | 不能只按名称合并 |
| scorer/Judge/rubric 不同 | 默认不直接比较质量分数 | 可从同份证据生成新的共同评分 pass，再比较 |
| 人工干预不同 | partial/not_comparable，按政策 | intervention 类型与时点可查 |
| 实际模型未报告且政策 require_match | 证据不足或不比较该维度 | 请求模型不替代真实回报 |
| 工具/权限/timeout/预算不同 | 若不是事前因素则不直接比较 | 明确不同的实验条件 |
| 仅费用证据缺失 | 质量可能可比，成本指标不可比 | 逐指标资格，而非一刀切抹掉全部结果 |

所有部分可比结论列出允许比较的 metric 和样本范围。正式 Gate 默认不接受事后筛出的共同子集代替原始选中任务集；诊断比较可以显示，但不能伪装成完整集合成绩。

## 6. 指标、覆盖、费用与统计

### 6.1 保持合法分母

每个 metric 声明 metric_id、version、unit、direction、aggregation、denominator_policy、eligibility_policy、required_evidence。已有 GSM8K selected-case 和 Direct LLM judged-case 分母保留，不强改成相同口径。[B4]

报告同时保存 selected、attempted、judged、scored、call_failed、not_attempted、unknown 和各 metric 的 eligible/missing count。不能把这些不同维度的计数简单相加；每个字段有明确定义，并提供互斥的 Case disposition 分布以核对总数。

无期望可合法不进入 Direct LLM 质量分母，但会影响可用于该指标的覆盖。正式 Gate 必须同时校验最低覆盖/样本量；缺失字段、NaN、无穷值和空分母均不转成通过或 0。

### 6.2 成本与时长

cost_summary 包括 known_cost、unknown_usage_count、currency、price_table_versions、source 和 coverage。货币不同不能直接相加，换算只在有版本化汇率政策的独立功能中执行，本阶段不自动换算。

成本/正确任务的定义必须说明分子包含哪些失败尝试费用，默认包含被比较任务的所有已知 subject 调用费用；Judge 和环境费用单列。unknown 存在时结果标为部分观测，不能据此通过严格成本上限。正确任务数为零时 cost_per_success 为不适用。

latency 区分模型调用、Case/Trial、环境准备和整个 Job；基线与候选使用同一口径。不要把多个 tool 事件当成多个独立质量样本。

### 6.3 统计实现范围

首批：均值/中位数/分位数、固定样本配对差异、二元结果区间与可解释分组汇总。分位数插值方法、权重、缺失政策都要版本化。区间只能表达所声明采样或随机性假设，不把固定小样本 smoke 泛化为模型总体能力。

配对 bootstrap 以 Task 为重采样单元，在一个 Task 内保留对应 Trial 组；不能把同题多个 Trial 当独立题目扩大样本量。拟议默认配置为固定 seed 与 2000 次重采样，可覆盖但进入 statistical_policy。小样本或没有配对资格时保留原始差值并说明区间不可用。

对独立二元样本可提供明确方法的区间；加权聚合或关联 Task 不沿用同一简单二项假设。上游官方原始区间与平台计算的诊断区间分开保存。

pass@k 仅在固定任务上存在 n 个有效、事先计划的可用独立 Trial，c 个成功且 1≤k≤n 时计算：

```text
pass@k = 1 - C(n-c, k) / C(n, k)
```

组合数边界 n-c<k 时失败组合为 0。k>n、未决 Trial、基础设施中断导致计划样本不足、或违反独立实验条件时返回不适用/证据不足；不能删掉失败传输后自称相同实验，也不能把 Provider retry 当新 Trial。n=5、c=2、k=2 的合成验收值为 0.7。

不提供未经事前定义的任意加权总排名。多指标可以列 Pareto/权衡视图，但任何综合分必须有单独版本化政策；本阶段不把这种综合分作为默认目标。

## 7. Baseline 与回归分类

BaselineSnapshot 引用固定 ScoringPass。设置默认 baseline 是指针操作，需要保留之前的 baseline 记录与变更原因；重评分不自动移动它。实验矩阵的 baseline entries 以匹配的 cell 条件关联，不取“最近的一条 Run”代替所有条件。

默认只允许符合完整性和证据政策的候选设为正式 baseline；需要保留历史不完整数据时可设 diagnostic baseline，但不能绕过正式门禁。needs_review、评分待确认和缺关键工件必须显示资格不足。

回归分类以共同任务集中的明确定义结果为基础：new_failure、fixed、persistent_failure、persistent_pass、changed_unknown、added_case、removed_case。重复 Trial 出现通过/失败混合时标记 instability 并由统计政策处理，不只保留最好的一次。

基线更新不要求企业审批流，但须有操作者、原因、policy hash、数据与报告引用。历史 Compare/Gate 在读取时仍对应原快照；新政策重新计算产生新结果，而不是修改旧结论。

## 8. GatePolicy 与机器语义

规则字段：rule_id、metric_id/version、scope、operator、threshold、baseline_delta_policy、min_samples、min_coverage、required_comparability、missing_policy、severity、evidence_requirements。阈值由具体任务需求配置；本规划不预设通用“模型合格准确率”。

首批规则：绝对质量阈值、相对 baseline 的最大允许退化、关键样本必过、最低覆盖/样本数、实际模型身份、费用/时长、禁止副作用与安全标记。missing_policy 正式门禁默认 fail-closed；跳过仅允许诊断模式且不能将整体显示为通过。

GateResult 必须保留所有规则结果和原因。建议顶层 decision 为 `pass/quality_fail/insufficient_evidence/not_comparable/execution_error/safety_block`，内部每条规则可以有更细原因。执行错误不覆盖已有有效分数，但会影响是否可正式放行。

### CLI 退出码建议与兼容要求

拟议沿用旧平台可辨认的语义：0 通过、1 质量失败、2 请求/配置不合法、3 平台或执行错误、4 用户取消、5 证据不足或不可比、6 安全阻断。实施时核对现有 CLI 已公开的退出约定；不兼容变化必须版本化，不直接静默替换。

配置验证失败在求值前退出 2；用户显式取消退出 4。求值后的多个问题按安全阻断 6 →执行错误 3 →证据不足/不可比 5 →质量失败 1 →通过 0 的优先级选择进程退出码，同时 JSON 保留全部失败规则，避免一个优先级掩盖其他事实。

读取/求值本身不运行模型、Judge 或 Benchmark。需要补证据或共同重评分时，只返回建议动作与所需授权，不能在 Gate 内自动开始付费调用。

## 9. 详细实施工作包

| 任务 | 消费 → 产出 | 具体范围 | 测试（拟新增） |
|---|---|---|---|
| M6-T01（Lite） | 固定 RunReportRef →比较条件 | ComparisonPolicy/Result、Case identity、允许变量、逐指标原因 | `tests/evaluators/test_comparability.py`：换模型可比、换 gold/scorer 默认不可比 |
| M6-T02（Lite） | ScoreSet →可信 summary | 指标定义、合法分母、coverage、null/error/NaN 处理 | `tests/evaluators/test_metric_coverage.py`：缺失样本不能改善放行结论 |
| M6-T03（Lite） | 固定 baseline/候选/政策 → Gate | 单 baseline 引用、阈值/覆盖/身份规则、API/CLI 结构化输出 | `tests/evaluators/test_gate_lite.py`：不足覆盖和 unknown cost 不通过 |
| M6-T04 | 资源因素 → ExperimentSpec/预览 | 矩阵合法性、规模上限、预算与调用数、组合排除原因 | `tests/contract/test_experiment_spec.py`：非法组合创建前拒绝 |
| M6-T05 | 发布 Spec → Run 集合 | cell 唯一分配、事务/断点恢复、取消和子 Run 关系 | `tests/storage/test_experiment_allocation.py`：重试请求和进程恢复不重复建 Run |
| M6-T06 | Task/Trial →统计结果 | 配对 bootstrap、分位数、pass@k、资格检查 | `tests/evaluators/test_statistics.py`：固定 seed 重现、0.7 公式样例、缺 Trial 拒绝 |
| M6-T07 | Lite baseline →历史与分组 baseline | cell 映射、默认指针审计、资格、历史读取 | `tests/storage/test_baseline_history.py`：current pass 改变不移动旧 baseline |
| M6-T08 | Lite Gate →完整回归规则 | 退化、关键样本、成本/时长、安全、所有规则明细 | `tests/evaluators/test_regression_gate.py`：多错误优先级与 JSON 明细一致 |
| M6-T09 | Comparison/Experiment →统一 UI | 矩阵进度、配置差异、样本变化、区间与未知解释、baseline 设置 | `apps/web/src/pages/experiments/Experiments.test.tsx`；比较页回归 |
| M6-T10 | GateResult →CI/导出 | 退出码、JSON/JUnit 最小导出、固定引用与零隐式执行 | `tests/cli/test_gate_exit_codes.py`：completed Run 仍可质量失败 |
| M6-T11 | 跨类型 fixture →交付证据 | Direct/GSM8K/C-Eval/Trial/Skill/Judge 的政策矩阵与文档 | `tests/integration/test_experiment_gate_slice.py`：端到端与历史不变 |

T01–T03 在 M2-T09 中交付，但仍归属于本阶段的同一服务。T04/T05 可在 M3 前实现；T06 的 Trial 资格依赖 M3，T08 的 Judge/Skill 条件依赖 M5；T09/T10 随可用功能增量交付，不等最后一次性建页面。

每项先写反例/预期输出并确认失败，再实现、执行 focused 与相邻回归，更新生成契约和文档后独立提交。不要因为统计图可显示就把缺失政策和 Gate 当成已完成。

### 合成 Gate 输入示例（拟议契约）

```json
{
  "policy": {"metric": "diagnostic.accuracy", "gte": 0.8, "min_coverage": 1.0},
  "candidate": {"selected": 10, "judged": 8, "correct": 8, "unknown": 2},
  "comparability": "comparable"
}
```

即便 judged accuracy=1.0，也因为 coverage=0.8 而得到 insufficient_evidence，不得通过。若目标 Benchmark 使用 selected 分母，质量值为 0.8，覆盖不足仍独立阻断；不得为了让门禁通过临时切换分母。

## 10. 强制验收矩阵

| ID | 场景 | 预期 |
|---|---|---|
| M6-A01 | 仅声明模型因素不同 | 可比，准确列出固定条件 |
| M6-A02 | 同 ID 样本内容/答案改变 | 不按名字误匹配 |
| M6-A03 | 候选缺失难样本 | 显示缺失，完整覆盖 Gate 不通过 |
| M6-A04 | 无 expected 样本 | 保留 no_expectation，合法质量分母与覆盖分开 |
| M6-A05 | NaN/Infinity/null/空分母 | 拒绝或 unknown，不出现 pass |
| M6-A06 | 评分器/Judge/rubric 版本不同 | 不直接比较，不能用最新 scorer 修改历史 |
| M6-A07 | 并发创建同一个实验/重复 API | 每个 cell 只有一个初始 Run |
| M6-A08 | 分配一半时崩溃 | 恢复剩余 cell，无重复执行 |
| M6-A09 | baseline Run 的 current pass 改变 | 已存 baseline/Gate 内容不变 |
| M6-A10 | 共同集之外新增/移除 Case | added/removed 独立，不混成修复或退化 |
| M6-A11 | Trial 少于计划/含未知调用 | pass@k 不适用，不自动补跑 |
| M6-A12 | n=5,c=2,k=2 / k>n | 分别为 0.7 /不适用 |
| M6-A13 | 相同 Task 多 Trial | bootstrap 以 Task 聚类，不夸大样本数 |
| M6-A14 | 费用缺失/货币不同/成功数为零 | partial/不可相加/不适用 |
| M6-A15 | 有人工干预或工具权限改变 | 明确条件变化和比较资格 |
| M6-A16 | 质量失败同时证据不足 | JSON 列全问题，退出码按已定义优先级 |
| M6-A17 | Run completed 但目标失败 | Gate quality_fail，不能放行 |
| M6-A18 | Gate 查询多次或导出 | 不调 Provider/Judge，无状态修改 |
| M6-A19 | 工件被删除或历史导入缺字段 | 证据资格降低，不从当前配置补历史事实 |
| M6-A20 | 取消一个实验 | 只作用于归属 Run，保留其他实验 |

## 11. 用户入口、发布与回退

拟议公共能力：experiment preview/create/get/cancel；report compare；baseline create/list/select；gate policy publish/evaluate；comparison/gate export。API 路由和 CLI 名称与已有 run/report 命令协调，核心响应从 Pydantic 生成，不让 Web 自行解析 Runner 工件。

UI 先显示比较条件和覆盖，再显示分数；明确模型、数据、scorer、预算与人工干预变化。提供共同失败、新失败、修复、未知、新增/移除筛选。少量 smoke 不展示强统计排名；区间说明对应其真实假设。基线切换需要确认具体 Run 和评分版本，不能只有“设为基线”而不知道绑定哪次评分。

```bash
uv run pytest -q -m "not live" tests/contract tests/evaluators tests/storage tests/cli
uv run pytest -q -m "not live" tests/integration/test_experiment_gate_slice.py
uv run ruff check .
pnpm --dir apps/web test
pnpm --dir apps/web build
make check
```

命令针对本阶段拟新增测试，不是当前执行记录。统计 fixtures 完全合成、确定性；真实质量结果只作为显式实验，不用于让单元测试依赖模型随机性。

回退停用新 Experiment 创建和 Gate policy 版本，保留已存 Run/ScoringPass/Baseline/Comparison/Gate。新的计算方法或阈值只发布新版本，不重写原 policy。存储 schema 沿实际 migration head 追加；历史计算失败可标为无效并另建修正结果，不能静默覆盖。

交付：`docs/protocols/experiments-and-comparison.md`、`docs/operations/gates-and-ci.md`、`docs/operations/statistical-policies.md`、`docs/verification/M6.md`。向 M7 交付稳定报告引用、退出码/导出契约和所有需要受保留政策保护的 baseline/score/artifact 引用。

## 12. 固定来源

- [B1] [当前基础 Gate](https://github.com/BaiZhi967/MoTTEavl/blob/a668d13ee5ea0c3613648f8992ecaa4a148d3855/packages/evaluators/motte_eval/gates.py)
- [B2] [旧 Gate 服务](https://github.com/BaiZhi967/llm_agent__evaluation_platform/blob/b661bcdf83e1c3dfb8d6062ee78817d249e86a4c/apps/api/app/services/gate_service.py)
- [B3] [ScoringPass 与快照边界](https://github.com/BaiZhi967/MoTTEavl/blob/a668d13ee5ea0c3613648f8992ecaa4a148d3855/docs/operations/platform-integrity.md)
- [B4] [现有插件与分母](https://github.com/BaiZhi967/MoTTEavl/blob/a668d13ee5ea0c3613648f8992ecaa4a148d3855/packages/sdk-python/motte_sdk/benchmark_plugins.py)

本文统计参数与门禁优先级是项目设计选择，不是宣称所有 Benchmark 官方采用相同口径；正式支持时以固定 Profile 与统计资格测试为准。
