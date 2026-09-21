# M6 权威执行计划：Experiment、Comparison、Statistics、Baseline 与 Gate

> 状态：planned。本文是 M6 实施的权威细化，不是实现报告或测试通过证明。用户已授权在全部功能、Web/CLI/真实模型测试、失败修复和最终 review 完成后合并主干并推送；在此之前不得操作主干。原始需求仍以 [`M6-experiments-comparison-and-gates.md`](../../roadmap/M6-experiments-comparison-and-gates.md) 为准；本文件补充当前代码基线、接口冻结、真实消费者和执行顺序。每个工作包完成后，必须更新 `docs/verification/M6.md`，不能仅勾选本文件。

## 0. 完成定义与状态模型

M6 的核心不是执行模型，而是把已冻结的运行证据变成可比较、可统计、可门禁的结果。所有 M6 领域服务必须消费已持久化的 Run/Trial/Observation/ScoreSet/ScoringPass/Artifact 引用；compare、report、gate、history、export 不得执行 Provider、Judge、Benchmark、业务工具或隐式 rescore。

阶段状态采用三层，不混用：

- `implementation`: 代码和公共入口是否已实现；
- `offline_verification`: 合成/fixture、Memory/SQLite/可用 PG、API/CLI/Web 的行为证据；
- `live_acceptance`: 真实 C-Eval/Harbor/Harness/模型/人工校准的证据。

工作包状态只允许 `complete`、`partial`、`blocked`、`not_run`。`skip` 不是通过；子 Agent 自述不是独立证据；测试数量不能替代需求覆盖。阶段只有在 21 个目标、11 个包、20 个验收场景和非编号约束均有实现、消费者、行为测试和证据时，才可标记 M6 完成。缺少真实外部资料时允许 `implementation_complete / offline_verified / live_pending`，但不得宣称正式 Gate 支持。

## 1. 开工冻结：T00（不计入 11 个正式工作包）

### 1.1 输入与当前事实

开发 Agent 必须从当前允许基线开始，记录：

- `git rev-parse HEAD`、分支/worktree、`git status --short`；
- Alembic 实际 head；
- M5 合入点及当前 M5 产品验收状态；
- 当前 M6-Lite 文件、路由、CLI 命令、OpenAPI/TS 生成物和测试收集结果；
- Memory/SQLite/PostgreSQL repository 的实际能力；
- M4 runtime、M5 Skill/Judge/calibration 的实际可消费字段；
- 当前未提交文件和 `var/` 证据目录，不得清理、覆盖或提交。
- M6 实现、测试、服务启动和提交必须在 `.worktree/m6-experiments-comparison-gates` 独立 worktree 完成；主工作区只在最终退出门全部满足后用于合并。
- 主 Agent 应按工作包将纯函数、storage、CLI、Web、fixture 和独立 review 分派给子 Agent，保持共享 contract/migration/API/Worker 单一写负责人，并亲自复验子 Agent 结果。

M5 运维说明与 M1-M5 产品验收报告可能来自不同时间点。Agent 必须以当前 HEAD 的公共零费用复验更新 current summary；旧报告只作为历史，不得直接升级或降级当前状态。

### 1.2 先冻结的术语与真值表

在 T01 写实现前新增或更新 M6 协议文档，至少冻结以下六张表：

1. **身份层级**：Experiment、experiment repeat、Cell、Run、Case、Trial、transport retry、operator retry、ScoringPass、ReportSnapshot、Baseline、GateResult；明确 Experiment repeat 与 Harbor Trial repeat 不相等，不重复计费或重复抽样。
2. **Case disposition**：`selected`、`attempted`、`judged`、`scored`、`call_failed`、`unknown`、`not_attempted`、`needs_review` 的互斥关系、状态转换和每个 metric 的 eligible/missing 规则。
3. **比较结论**：`comparable`、`partially_comparable`、`not_comparable`，以及允许变化因子、共同样本范围和逐指标资格。
4. **Baseline 资格**：正式/诊断/不合格；固定 pass、report snapshot、证据 pin、policy hash、人工干预和缺失证据条件。
5. **Gate 规则到决策**：`pass`、`quality_fail`、`insufficient_evidence`、`not_comparable`、`execution_error`、`safety_block` 的组合与优先级。
6. **决策到机器输出**：JSON/JUnit/CLI 退出码、JUnit testcase 状态、CI 阻断条件；JSON 永远保留全部 rule result，退出码只提供稳定摘要。

同时冻结 canonical serialization、规范化 hash、未知字段策略（默认 fail closed）、policy 生命周期（draft/published/deprecated）、`evaluation_input_hash`、`result_semantics_hash` 和 ReportSnapshot/evidence pin 规则。M6-Lite 契约必须能向 M6-Full 向前兼容扩展，不以第二次不兼容 migration 修补前一包的缺口。

### 1.3 T00 产出与门

- `docs/protocols/experiments-and-comparison.md` 初版；
- `docs/verification/M6.md` current summary；
- G/T/A/非编号要求账本；
- 当前代码→计划映射和文件 ownership；
- 外部条件清单和可独立推进的工作包；
- 基线 focused 测试与精确 node-id 失败差分。

T00 只有在测试可以正常收集、共享文件写入负责人明确、M5 继承阻断已分开登记后，才能进入 T01。

## 2. 工作包顺序与文件所有权

```text
T00 基线/术语/真值表
 ├─ T01 比较身份与政策 ─┐
 ├─ T02 分母/覆盖/费用 ──┼─ T03 快照/Baseline/Lite Gate
 │                        │
 └────────────────────────┴─ T04 ExperimentSpec/preview
                              └─ T05 Cell 分配/Dispatcher/恢复
                                  ├─ T06 Trial 统计/pass@k
                                  └─ T07 分组 Baseline/历史分类
                                      └─ T08 完整 Gate 规则
                                          ├─ T09 API/CLI/Web
                                          ├─ T10 CI/export/退出码
                                          └─ T11 跨 suite、最终 review、M7 交接
```

共享 contract、migration、storage、API main、Worker 和生成文件在同一时间只允许一个实现负责人。T01-T03 是 M6-Lite 的唯一实现；M2-T09、M3/M4/M5 只消费它们。T04/T05 不得创建新的调度器；T09 不得在 Web 计算分数；T10 不得和 M7 产生第二份 exporter 主权。

## 3. 工作包实施卡

每包统一执行：行为反例红灯（可收集且断言业务缺陷）→ 最小完整实现 → focused 回归 → 相邻回归 → 规格/安全/并发 review → 修复复验 → 文档和验证记录 → 小提交。导入错误、缺模块、collection error、skip、静态类型通过不能算红灯或完成。

### M6-T01：RunReportRef、比较身份与 ComparisonPolicy

**依赖：** T00、M1 固定 Observation/ScoringPass。
**目标：** G04-G06、G18。
**验收：** A01、A02、A06、A10、A15。

**实现范围：**

- 扩展 `RunReportRef`，固定 run/pass/report schema/evidence hash，以及可验证的 ReportSnapshot、metric-definition、case disposition、artifact/evidence pin 引用；不能只有 run_id 后读 current。
- 将 execution fingerprint 与 comparison signature 分开；因素使用版本化 registry，不接受任意 JSON path 或“忽略全部差异”通配符。
- 按 source/revision/stable case key/input/expected 内容 hash 对齐样本；渲染 prompt 改变不改变任务身份，gold/expected 内容改变必须单独报告。
- 支持 `comparable`、`partially_comparable`、`not_comparable`，列出固定条件、允许变化、added/removed/changed、共同样本和逐 metric 资格。
- 把 model、agent、runtime、tool/permission、budget、timeout、retry、Workflow/Fixture/Skill、Judge/rubric/calibration、人工 intervention 作为真实冻结因素；只允许 policy 明示变化。
- 只缺 cost 时允许 quality metric 可比，cost metric 独立不可比；真实模型身份缺失不能用请求模型补齐。

**必须落地的反例：** 同模型但 gold/scorer/rubric 改变；同 case 名不同内容；一侧有 M4 runtime 或人工干预；Skill 三臂预算不同；缺费用但质量证据完整；未知 policy 字段。

**建议触点：** `packages/contracts/motte_contracts/comparison.py`、`packages/evaluators/motte_eval/comparison.py`、`tests/evaluators/test_comparability.py` 及 contract tests；已有模块优先扩展。

**门禁：** 比较纯函数/只读；服务调用计数保持 0；旧 M6-Lite 返回兼容投影；OpenAPI/TS 若变更同步。

### M6-T02：Metric registry、Disposition、Coverage 与 Cost

**依赖：** T00、M1/M2/M3 评分契约。
**目标：** G07-G09、G12。
**验收：** A03-A05、A14。

**实现范围：**

- 建立版本化 metric registry：metric id/version、unit、direction、aggregation、denominator、eligibility、required evidence、missing policy。
- 发布统一 disposition truth table；selected、attempted、judged、scored、call_failed、unknown、not_attempted 不互相混算，保留互斥 Case 分布用于核对。
- GSM8K selected-case、Direct judged-case、C-Eval Profile、Harbor Trial/Task、Scenario/Skill/Judge 各自使用合法分母；不为了统一 API 改写 suite 公式。
- `coverage`、metric value、eligible/missing count、min samples 和 min coverage 分开；空分母、NaN、Infinity、null 不产生 pass。
- cost summary 保留 known/unknown、currency、price table version、source、usage coverage；subject、Judge、环境成本分开，跨币种不自动相加，零成功的 cost_per_success 为不适用。
- 将 `selected/attempted/judged/unknown/not_attempted` 全量呈现给 API/CLI/Web，不因共同子集过滤而提高正式 Gate。

**必须落地的反例：** 10 选 8 全对 coverage 仍为 .8；无 expected 的 Direct 样本；全缺失、空分母、NaN/Inf；unknown cost、不同 currency、成功数为 0；Harbor 计划 Trial 未尝试；一个 Case 多指标不得扩大 attempted。

**建议触点：** `packages/evaluators/motte_eval/coverage.py`、`aggregate.py`、`packages/contracts/motte_contracts/report.py`、各 suite adapter、`tests/evaluators/test_metric_coverage.py`。

**门禁：** 所有聚合输出带 denominator/eligibility/provenance；旧 report roundtrip 不漂移。

### M6-T03：不可变 Report/Baseline/GatePolicy 与 M6-Lite Gate

**依赖：** T01、T02。
**目标：** G09、G13、G15、G17。
**验收：** A05、A09、A17、A18。

**实现范围：**

- 定义 `BaselineSnapshot`、`GatePolicyVersion`、`GateResult` 的 contract、生命周期、规范 hash、发布 CAS 和不可变存储；补齐 Memory/SQLite/PostgreSQL contract suite 与 migration。
- Baseline 固定 RunReportRef、ComparisonPolicy、metric/score provenance、evidence pin、created_by/reason；current pass 改变不能移动既有 baseline。
- GatePolicy 规则包含 metric/version、operator、threshold、direction、baseline delta、min samples/coverage、comparability、missing policy、severity、evidence requirements。
- `evaluate_gate` 纯求值，保存所有 rule results、evidence refs、input hash 和 semantics hash；`evaluated_at` 只作为审计字段，不改变结论等价性。
- 统一输出六类 decision；Run completed 只代表执行终态，不代表质量通过。
- API/CLI 只读评估零模型/零 Judge/零 Runner 调用；公共输入必须引用服务端固定 pass，不接受客户端伪造 score summary。

**必须落地的反例：** baseline current pass 漂移；同 baseline id 异内容写入；缺 ReportSnapshot/artifact pin；quality fail 与 insufficient/safety/execution 同时出现；重复 evaluate；GET/history/pass switch 前后调用数变化；未知 cost 的正式 cost gate。

**建议触点：** `packages/evaluators/motte_eval/gates.py`、`packages/storage/motte_storage/baselines.py`、新增/扩展 contracts、`motte_sdk.comparisons`、API schemas/routes、三 store、`tests/evaluators/test_gate_lite.py`、`tests/storage/test_baseline_history.py`。

**门禁：** M2-T09 使用本服务；不得出现 C-Eval/Skill/Harbor 私有 Gate 实现。

### M6-T04：ExperimentSpec、FactorRegistry 与 Preview

**依赖：** T03。
**目标：** G01-G02。
**验收：** A07、A08、A20 的预检部分。

**实现范围：**

- 定义 ExperimentSpec、FactorAssignment、ExperimentCell、StopPolicy、BudgetPolicy、EvaluationRef；发布后不可变。
- 首批因素为 ModelProfile、reasoning level、prompt/workflow version、runtime version、SkillVersion；每个因素必须有资源 owner、合法值和版本 hash。
- 明确 `experiment_repeat` 与 `trial_repeat` 层级；transport retry/operator retry 不是实验样本。Experiment repeat 产生独立 Cell/Run，Harbor Trial 只在 Run 内聚合。
- preview 展示 cell/Case/Trial/最大调用/预算和 known/unknown cost，并列出不支持组合；超矩阵、超 Case/Trial/call/export 限额或缺资源必须整体拒绝，不能先排队部分单元。
- cell_id 使用 canonical spec version + factor assignment + experiment_repeat 的稳定 hash；stop policy 不能变成“直到成功”。

**必须落地的反例：** 非法组合；超上限；Preview 创建 Run/调用模型；数据集或 scorer 改变却只声称 model factor；factor value 版本漂移；Experiment repeat 与 Trial repeat 误乘；缺费用被当 0。

**建议触点：** 新增 `packages/contracts/motte_contracts/experiment.py`、`packages/sdk-python/motte_sdk/experiments.py`、`tests/contract/test_experiment_spec.py`。

### M6-T05：Cell 原子分配、现有 Dispatcher 接线与恢复

**依赖：** T04。
**目标：** G02-G03。
**验收：** A07、A08、A20。

**实现范围：**

- 同一事务写 Experiment、Cell、initial Run 关联和 allocation checkpoint；Memory/SQLite/PostgreSQL 语义一致，唯一约束保护 cell。
- 复用现有 RunDispatcher/Worker/CaseAttempt/ScoringPass；Experiment 只编排，不直接调用 Provider/业务工具，不持有第二个 current。
- 预览后到提交之间重新校验资源/version/budget，拒绝 TOCTOU 漂移；大矩阵分批分配但 checkpoint 可恢复。
- 重复 API request key + 相同内容幂等；同 key 不同内容冲突；半批崩溃恢复只补未分配 cell，不重建已分配 Run。
- Experiment 取消只请求自己拥有的 Run；dispatch 后未知不得自动重放；显式 retry 产生 superseding 子 Run，保留 initial_run_ref 和原结果；正式报告默认不挑最好结果。

**必须落地的反例：** 两个并发创建同 cell；半分配后进程崩溃；一个 Experiment 取消误伤其他 Run；Worker 重启重复启动；失败后自动重试直到分数上升；跨 Experiment/cell owner 错配。

**建议触点：** 新增/扩展 `packages/storage/motte_storage/experiments.py`、`run_store.py`、`postgres.py`、API schemas/routes、`tests/storage/test_experiment_allocation.py`、`tests/integration/test_experiment_dispatch.py`。

### M6-T06：Trial/Task 统计与资格

**依赖：** T02、M3 TrialPlan/TrialResult。
**目标：** G10-G11。
**验收：** A11-A13。

**实现范围：**

- 先发布 `statistical_policy@1`：统计单位、权重、置信水平、区间方法、分位数插值、Decimal/float 精度、缺失政策、bootstrap seed/iterations（默认 2000）和 implementation version。
- 均值/中位数/分位数、固定配对差、二元区间和分组统计必须保留参数、版本和输入引用；小样本/无配对只给原始差并标区间不适用。
- 配对 bootstrap 以 Task 为重采样单元，同 Task Trial 组保持完整；不能把 Trial 当独立题目扩大样本。
- pass@k 只接受事前计划、独立、有效、完整 Trial；传输 retry、恢复 retry、operator retry 不计入 n；`n=5,c=2,k=2` 为 0.7，k>n/缺计划/未知 Trial 返回不适用或不足证据。
- Experiment repeat、Harbor Trial、Judge repeat 的费用和样本层级必须独立显示，不能重复累计。

**必须落地的反例：** n=0/1、k>n、未知 Trial、计划不足、同 Task 多 Trial、零方差、极端值、固定 seed 两次结果相同、不同 seed 明确不同。

**建议触点：** 新增 `packages/evaluators/motte_eval/statistics.py`、`tests/evaluators/test_statistics.py`、`docs/operations/statistical-policies.md`。

### M6-T07：分组 Baseline、历史与回归分类

**依赖：** T03、T05。
**目标：** G13-G14、G21。
**验收：** A09、A10、A19。

**实现范围：**

- 支持单 Run 和按 Cell 条件映射的 Baseline entries；默认 baseline 变更是有操作者、理由、policy hash 和 CAS 的指针操作，不覆盖旧指针。
- 正式 baseline 必须有完整 ReportSnapshot、固定 ScoringPass、证据 pin、合格 scorer/Judge/calibration；needs_review、缺关键 artifact、未校准 Judge 只能 diagnostic/experimental。
- 在共同稳定样本上分类 `new_failure`、`fixed`、`persistent_failure`、`persistent_pass`、`changed_unknown`、`added_case`、`removed_case`、`instability`；不只保留最好 Trial。
- 工件删除、历史导入缺字段、重评分、资源升级只降低新计算资格或产生新版本，不修改已持久 Gate/Comparison bytes/hash。
- 为 M7 提供 pin/retention 所需引用，但不实现 M7 的真实导入、GC 或备份。

**必须落地的反例：** current pass 改变；默认 baseline 最近 Run 猜测；同 Cell 条件错配；新旧样本集变化；一 Task 多 Trial 混合；artifact 删除后旧 Gate 改变；import-like incomplete report 从当前配置补事实。

**建议触点：** baseline repository/contract、`motte_sdk.comparisons`、API schemas/routes、`tests/storage/test_baseline_history.py`、`tests/integration/test_history_immutability.py`。

### M6-T08：完整 Regression Gate 与安全/证据语义

**依赖：** T06、T07、M4 runtime contract、M5 Judge calibration contract。
**目标：** G12、G15-G18。
**验收：** A06、A14-A17。

**实现范围：**

- 增加绝对质量阈值、相对 baseline 退化、关键样本、最低覆盖/样本数、实际模型身份、费用/时长、禁止副作用和安全标记规则。
- 冻结 direction、baseline=0 的行为、partial comparability、关键样本身份、severity、missing policy 和规则 registry；正式门禁默认 fail closed，diagnostic skip 不能将整体变 pass。
- M4 的 runtime/tool/permission/intervention provenance 和 M5 Skill/Judge/rubric/calibration provenance 必须来自所选 pass/report；没有真实 runtime 或 ≥30 人工校准时保持 experimental。
- 一次输入可同时触发质量失败、证据不足、不可比、执行错误和安全阻断；输出完整规则，按已冻结优先级生成 decision/exit code。
- Gate 不自动触发重评分、Judge 或补跑；只返回建议动作和所需授权。

**必须落地的反例：** 质量失败+缺证据；有人工干预/工具权限改变；未校准 Judge；baseline=0；partial compare；安全标记；completed 但目标失败；M4 identity unknown。

**建议触点：** `packages/evaluators/motte_eval/gates.py`、comparison service、`tests/evaluators/test_regression_gate.py`、M4/M5 fixture、`docs/operations/gates-and-ci.md`。

### M6-T09：统一 API/CLI/Web 用户入口

**依赖：** T05、T07、T08。
**目标：** G19。
**验收：** A03、A09、A10、A18、A20 的公共入口部分。

**实现范围：**

- API 提供 experiment preview/create/get/cancel、report compare、baseline create/list/select、policy publish/evaluate、comparison/gate detail/export；响应由公共 contract/Pydantic 生成。
- CLI 使用同一 SDK/domain service，不在命令内重新聚合或访问 DB；JSON 输出固定引用、规则、覆盖、unknown 和原因。
- Web 先显示条件、版本、样本集、coverage 和资格，再显示分数、区间、Gate；支持矩阵进度、取消、失败集/Case/Trial 下钻、固定 Run/pass/policy 的 baseline 选择。
- Web 不展示 smoke 的强排名，不把 partial/unknown/区间不可用渲染成 0；迟到响应不得覆盖新的 Experiment/Cell/Run/pass 选择。
- 不可用能力显示具名原因；页面不通过 404/405/静态存在假装已交付。

**建议触点：** API schemas/routes、CLI main/client、`apps/web/src/api`、实验/比较页面和 `apps/web/tests/`。UI 开工前执行 `design-taste-frontend` 或 `redesign-existing-projects` 对应流程，遵守 DESIGN.md。

**门禁：** API/CLI/Web 对同一输入的 JSON decision、report refs、coverage 和 rule ids 一致；OpenAPI/TS 生成物无漂移。

### M6-T10：CI、退出码与最小 Exporter v1

**依赖：** T08、T09 的稳定 JSON 契约。
**目标：** G16、G19-G20。
**验收：** A16-A18。

**实现范围：**

- 定义 `gate evaluate` 与未来 M7 的 `run-and-gate` 边界：M6 只求值固定引用，不负责自动启动 Experiment。
- 兼容性核对现有 CLI 退出约定；若无冲突，使用 0 pass、1 quality_fail、2 invalid/config、3 execution_error、4 cancelled、5 insufficient/not_comparable、6 safety_block；JSON 保留全部问题。
- exporter v1 提供固定 ReportRef/Baseline/GatePolicy/GateResult 的 JSON 和最小 JUnit；M7 只能扩展同一版本 extension point，不重写主权。
- export、refresh、GET、history、compare、gate 的 model/provider/Judge/Runner/task-start 计数必须为 0。
- CI 示例只使用合成固定证据，不能把 workflow 成功或 Run completed 转成 pass。

**建议触点：** `packages/sdk-python/motte_sdk/export.py`（若已有同职责模块则扩展）、CLI、`tests/cli/test_gate_exit_codes.py`、`tests/sdk/test_export.py`、CI fixtures、`docs/operations/gates-and-ci.md`。

### M6-T11：跨 suite 集成、阶段 review 与 M7 交接

**依赖：** T09、T10、M2-T09、M5 公共链路。
**目标：** G21，并汇总 G01-G20。
**验收：** A01-A20 的跨层复验。

**固定集成矩阵：**

- Direct LLM、GSM8K、C-Eval 固定 Profile；
- Harbor Task/Trial，含计划不足和 retry；
- Builtin Agent/Scenario/Skill no-v1-v2；
- M4 runtime 的脱敏 report fixture，覆盖 model/tool control、native config、identity unknown/intervention；
- M5 Judge 已校准与未校准两种状态；
- import-like incomplete report 和删除 artifact 场景；
- 同一报告多次 compare/gate/export，验证零副作用和 hash 不变。

M6 对 imported-origin 只验证策略对缺失字段的行为；真实历史导入、retention/backup、SDK/pytest 和 RC 属于 M7，不在 M6 偷渡实现。

**集成要求：** API create/preview → 真实 Worker/Dispatcher → 持久 Run/ScoreSet/ScoringPass → ReportSnapshot → compare → baseline → gate → CLI/export/Web 读同一结果。直接调用 helper 只能作为单元证据，不能替代公共链路。

**最终 review：** 逐 G/T/A 与非编号要求核对实现引用、真实消费者、测试 node、commit、环境、证据级别、限制；做基线/head 精确 node-id 差分；P0/P1 必须关闭，P2 必须关闭或证明不影响退出门；更新 M6 verification、协议、运维和 M7 handoff。

## 4. 目标覆盖矩阵

| 目标 | 主责工作包 |
|---|---|
| G01 | T04 |
| G02 | T04/T05 |
| G03 | T05 |
| G04-G06 | T01 |
| G07-G09 | T02/T03 |
| G10-G11 | T06 |
| G12 | T02/T08 |
| G13-G14 | T03/T07 |
| G15-G18 | T03/T08 |
| G19-G20 | T09/T10 |
| G21 | T07/T11 |

## 5. 强制验收矩阵

| ID | 条件 | 必须观察 | 主责 |
|---|---|---|---|
| A01 | 仅模型因素改变 | comparable，固定条件完整 | T01 |
| A02 | 同 ID 内容/gold 改变 | changed/not_comparable，不按名字合并 | T01 |
| A03 | 候选缺失样本 | 缺失留在分母，完整 coverage Gate 不通过 | T02 |
| A04 | 无 expected 样本 | no_expectation 保留，质量分母独立 | T02 |
| A05 | NaN/Inf/null/空分母 | unknown/拒绝，无 pass | T02/T03 |
| A06 | scorer/Judge/rubric/runtime 变化 | 逐项不可比/资格不足 | T01/T08 |
| A07 | 并发创建同 Cell | 一个初始 Run | T05 |
| A08 | 半分配崩溃恢复 | 只补未分配 Cell | T05 |
| A09 | Baseline current pass 变化 | 历史 bytes/hash/ref 不变 | T03/T07 |
| A10 | added/removed Case | 独立分类，不变成 fixed/regression | T01/T07 |
| A11 | Trial 少于计划/含未知调用 | pass@k 不适用，不自动补跑 | T06 |
| A12 | n=5,c=2,k=2 与 k>n | 0.7 与不适用 | T06 |
| A13 | 同 Task 多 Trial | Task 聚类重采样 | T06 |
| A14 | 缺费用/异币种/零成功 | partial/不可相加/不适用 | T02/T08 |
| A15 | 人工干预/工具权限改变 | 资格受影响且原因可查 | T01/T08 |
| A16 | 多种 Gate 失败同时出现 | JSON 全量规则，稳定优先级 | T08/T10 |
| A17 | Run completed 但质量失败 | quality_fail，不放行 | T03/T08/T10 |
| A18 | 重复查询/导出 | 0 模型/Judge/任务启动，结果 hash 稳定 | T03/T10 |
| A19 | artifact 删除/import-like 缺字段 | 新资格降低，旧结论不漂移 | T07/T11 |
| A20 | 取消一个 Experiment | 只影响归属 Run，其他不变 | T05/T09 |

## 6. 验证命令与证据要求

按包记录实际命令、commit、OS/Python/Node/pnpm/DB、exit code、passed/failed/skipped、日志/工件 hash。建议命令如下，按当前实际测试路径调整，不得把不存在的测试文件当作执行过：

```bash
uv run pytest -q -m "not live" tests/contract tests/evaluators tests/storage tests/api tests/cli
uv run ruff check .
uv run mypy packages/contracts
make openapi
make openapi-check
pnpm --dir apps/web test
pnpm --dir apps/web build
make check
```

阶段集成还要求：

```bash
uv run pytest -q -m "not live" tests/integration/test_experiment_gate_slice.py
```

全部功能完成后必须根据 A01-A20 生成 M6 专项测试用例和 `m6-accept-*` 受控数据，覆盖 API、真实 Worker/Dispatcher、CLI、Web、三种 storage、并发/恢复和跨 suite。Web 除 `pnpm test/build` 外，必须启动独立 API/Worker/Web 服务，用浏览器实际走 preview/create/status/compare/baseline/gate/export；CLI 必须实际走 preview/create/status/cancel/compare/baseline/gate/export 和 JSON/JUnit/退出码。服务从 M6 worktree 启动，使用独立数据库，只停止本任务拥有的进程。

离线门禁通过后，使用项目已配置的 DeepSeek V4.1 Flash 做有界真实验收：从模型目录核对实际 model/profile/provider，固定小型测试集、Experiment、最大调用数、token、费用和总时长，至少跑通 Run→ScoringPass→ReportSnapshot→Comparison→Baseline→Gate，并证明重复 compare/gate/export 不增加模型调用。凭据不进入日志或提交。每轮测试后更新 `docs/verification/M6.md` 和 `docs/PROGRESS.md`，所有 M6 相关失败必须修复并重跑 focused、相邻和最终门禁。

具备支持环境时执行真实 PostgreSQL 两连接并发、迁移/回退、Worker/Dispatcher 恢复、目标 OS 和 Docker/Harbor fixture。缺环境记录 `blocked` 或 `not_run`，不把 skip 算 passed。真实模型、Judge、人工校准、外部 Harness 另表记录授权、版本、样本、预算、费用和清理范围。

## 7. 交付、回退与 M7 接口

交付文档：

- `docs/protocols/experiments-and-comparison.md`；
- `docs/operations/gates-and-ci.md`；
- `docs/operations/statistical-policies.md`；
- `docs/verification/M6.md`；
- API/OpenAPI/TS、migration upgrade/downgrade 说明和必要的 M7 handoff。

回退先停止 Experiment 创建/领取和新 GatePolicy 发布，确认活动自有资源停止，再关闭能力开关。保留 Experiment、Cell、Run、Trial、ScoreSet、ScoringPass、Baseline、GateResult、ReportSnapshot、Artifact pin 和不确定记录；不删除历史来掩盖失败。新的算法、阈值或统计实现必须创建新 policy/version/result，不覆盖旧结论。

M7 只消费稳定的 RunReportRef、ReportSnapshot、Baseline/GateResult、退出码和 exporter v1；M7 负责类型化 SDK、pytest/CI 扩展、真实历史导入、retention/backup、发布和三类替代场景，不复制 M6 比较/Gate 算法。

最终集成顺序：确认 M6 worktree clean 和所有验证记录完整，重新运行最终门禁；拉取远程最新 `main`，在不覆盖主工作区其他改动的前提下合并 M6 分支；在合并后的 `main` 再运行关键 smoke、OpenAPI drift、Web test/build 和 `make check`；确认通过后推送 `main`，核对远程 SHA，并将 merge commit、push 结果和 CI 状态写入 `docs/verification/M6.md` 与 `docs/PROGRESS.md`。
