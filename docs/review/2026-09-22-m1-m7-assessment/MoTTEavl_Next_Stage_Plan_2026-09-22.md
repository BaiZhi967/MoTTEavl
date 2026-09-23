# MoTTEavl 下一阶段开发规划：完成交付收口，再进入持续评测产品化

日期：2026-09-22（UTC+08:00）  
参考基线：`fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d`  
状态：建议规划，未实施、未提交 GitHub。  
配套评估：`MoTTEavl_M1-M7_Assessment_2026-09-22.md`（含原 137 项目标证据对照）。

## 1. 目标与非目标

目标不是继续增加一整套基础框架，而是让已有模型/Agent/Benchmark/Scenario/Skill/实验/发布能力成为一致、可验证、可长期使用的产品。

下一阶段分为：

- **M8：M1–M7 交付收口与 RC 验收。** 补当前确定的代码缺口、原验收缺口和发布证据。它是原路线债务的组织方式，不把原未完成项重新命名后冒充原阶段已完成。
- **M9：持续评测工作流与失败集闭环。** 以既有 SDK/Experiment/Gate 为基础，连接真实项目变更和长期回归样本管理；不新建第二套评分与调度系统。
- **M10：一个需求驱动的新评测域。** 优先选择已有实际应用的一个任务域验证扩展能力，而不是同时开 RAG、多模态、浏览器、Blender 等所有方向。

保持单用户、API-first、现有 Python/FastAPI/React/Vite、RunDispatcher、CaseAttempt、ScoringPass 与不可变资源。单 Worker 是目前的明确边界；只有测得容量瓶颈才扩到受控并行或租约模型。不开展企业权限、多租户、插件商店、任意代码热加载、整仓包重排和通用 Agent 编辑器。

本规划不授权付费调用、真实旧库写入、覆盖恢复、正式发布或切换；执行相应任务时逐项确认所用账户/模型、调用与费用上限、源/目标环境和可逆范围。已有历史授权不自动覆盖本次新任务。

## 2. 当前缺口如何进入下一阶段

| 原问题 | 所属原目标 | 下一阶段任务 |
|---|---|---|
| 当前 CI 三项失败、环境测试定位不一致 | M1/M5/M7 回归与环境支持 | M8-T01 |
| 声明的 prompt/runtime/skill 因子未进入 Run manifest | M6-G01/G04/G18 | M8-T02 |
| preview/create 实际样本预算校验不同 | M6-G01/G02 | M8-T03 |
| Direct/GSM8K 本地计算比较，固定 pass/币种/unknown 不一致 | M6-G04/G05/G07–G09/G12/G19/G21 | M8-T04 |
| Experiment 仅接受 direct-llm | M6 完整实验范围 | M8-T05 |
| 统计/stop/幂等的生产消费者需明确 | M6-G02/G10/G11/G17 等 | M8-T03/T06 |
| 真模型、正式 Benchmark、Runtime、Judge 校准证据不齐 | M1-G18、M2-G13、M3-G17/G18、M4-G16、M5-G13/G18/G20 | M8-T07/T08 |
| PG backup/restore、真实旧导出、三个替代场景未演练 | M7-G10/G14–G16/G20/G21/G23 | M8-T09/T10/T12 |
| 支持矩阵/滚动账本与实际提交不统一 | 各阶段证据及 M7-G22 | M8-T00/T11 |

## 3. M8 最终目标清单

- [ ] M8-G01：固定候选提交的所有必需 CI job 完成且没有失败；必需安全与漂移检查没有被前序失败跳过。
- [ ] M8-G02：每个接受的实验因子都能追溯到解析快照、实际请求或执行配置；不支持因子在创建前整体拒绝。
- [ ] M8-G03：preview/create/allocate 使用同一预算与资源解析政策；超预算或无法落实的严格约束不会先创建部分可执行 Run。
- [ ] M8-G04：所有比较入口只消费统一 ComparisonResult 和固定 Run/ScoringPass，不在浏览器重新裁定可比性或质量。
- [ ] M8-G05：原核心 suite 的实验编排按明确能力接入；各 suite 的试验单位与恢复语义保持不变。
- [ ] M8-G06：统计结果从固定证据进入实际 API/CLI/Web/导出；方法、样本单位、参数和不适用原因可查。
- [ ] M8-G07：原 Supported 要求的模型/数据/Runtime/Judge 验证组合有可审计收据；环境缺失不伪装 passed。
- [ ] M8-G08：真实 PG 与 Artifact 的一致备份/恢复，以及真实旧导出的隔离迁移演练通过。
- [ ] M8-G09：维护/GC/保留范围有明确实现或批准的范围修订；引用缺口不通过删除证据隐藏。
- [ ] M8-G10：137 项目标拥有按 commit/env/consumer/test/receipt 分层的唯一状态；历史记录与当前结论不混用。
- [ ] M8-G11：三个旧平台替代场景闭合后，生成可审核的 cutover-ready 清单；实际切换需单独授权。
- [ ] M8-G12：在明确的支持范围内形成 RC 产物、已知限制和回退说明；不是“所有后端都稳定”的泛化声明。

## 4. M8 实施工作包

### M8-T00：冻结验收口径与证据索引

**范围：** 复用 docs/roadmap、docs/verification、docs/release。增加可校验的机器可读证据索引，不复制一套与原 G/T/A 无关的新目标表。

每条证据包含 goal_id、implementation_commit、consumer_path、test_node、verification_layer、environment、runner/model/data/profile 版本、result、receipt_ref、artifact_hash、限制、supersedes。当前状态生成自最后有效证据；历史“未执行”不删除，但不覆盖较新已执行收据。

**明确分开两个维度：** 实现状态（缺实现/部分/实现）与证据层（offline/integration/live/支持）。不要用一个线性 tested>supported 排序混合含义，也不要将只在某开发机没 Docker 写成全项目未验 Docker。

**目标/验收：** 为 M1–M7 每项原目标留一条当前判定；必需验收缺 receipt 时不能自动发布 supported；已修复 issue 不再被旧段落重新显示 open。存在不可公开证据时保存脱敏摘要和 hash，不把真实密钥/业务原件上传公开仓库。

### M8-T01：CI、Fixture 和平台能力基线

**现有触点：** tests/scenario/test_fixture_lifecycle.py；motte_scenario/state.py；tests/storage/test_scoring_jobs.py；tests/storage/test_trials_downgrade_guard.py；相应 conftest 和 CI。

**实施范围：**

1. 复现 ControlledRoot 新建与 reopen 两条路径，约定 backend 标识的是实际安全实现而不是操作系统名称。使属性、实现和测试一致，同时保留路径/链接/所有权反例。
2. 统一 ScoringJob 测试的 subject owner、顶层 run、case、job 与 invocation ID。专门保留 owner 不匹配必须拒绝的负例。
3. 每个 PG 迁移/降级用例使用独立 disposable 数据库，或经验证覆盖迁移 search_path 的独立 schema；禁止在共享环境无差别 DELETE 来换取通过。
4. 将条件式环境测试与支持必需测试分开。缺 symlink 权限的普通开发机可以有明确 skip；声明支持的平台专用验证必须配置相应能力并真正执行安全测试。
5. Docker cleanup 的 unknown 与 residual 使用不同 fixture：无观察能力应 unknown；已证明残留才 residual。不能把 unknown 改成 cleaned/pass。

**验证：** 三条失败各自、相互组合、全量套件均执行；改变顺序仍不污染；同一候选提交完整 CI 至少一次全绿，易污染集重复执行。安全扫描可独立 job，以便不被其他 test failure 遮蔽；发布仍以全部必需 job 通过为门。

**完成门：** 当前 3 fail 归零；原负例不减少；不是 xfail 整个失败模块或关闭归属/降级校验。

### M8-T02：实验因子真实执行契约

**现有触点：** motte_contracts/experiment.py、motte_sdk/experiments.py、resolve.py；现有 Backend/Benchmark/Runtime/Skill 注册。

**实施范围：**

- 为每个 suite 声明允许因素及编译责任：model_profile、reasoning_level、prompt_version、runtime_version、skill_version。
- 首个修复不急于实现所有因子，而是对尚无消费者的因子 fail-closed，防止配置被接受后静默忽略。
- 编译结果包含 assignment→resolved resource→manifest field→consumer 的映射及 hash。manifest 固定执行参数，不只把标签写到 Cell 元数据。
- 使用当前资源发布/不可变机制；若 Prompt 尚无独立可发布资源，不虚构版本，先明确采用 Workflow/Scenario 已存在的哪一种 prompt 定义与 hash。
- 固定变量和控制变量重叠时拒绝冲突，不允许合并顺序悄悄覆盖实验变量。

**核心反例：** 两个 prompt 版本能被 fake Provider 实际请求捕获区分；两个 Skill 版本进入真实注入快照与 prompt；runtime 变量选择不同运行时及其政策。尚未接通时两个创建入口均返回明确“不支持因素”，零新 Run。

**完成门：** 不再出现不同 Cell 标签却相同预期执行配置；合法参数差异进入报告可比性；平台不会自动为用户挑选或部署“最佳模型”。

### M8-T03：预算、停止条件与请求幂等闭环

**现有触点：** ExperimentService.preview/create/allocate、BudgetPolicy/StopPolicy、现有请求幂等存储、Worker 状态/调用证据。

**实施范围：**

- 共享一个无副作用的编译/预检过程：先解析真实 Case 数、Cell/Trial 数、实际每调用输出限制、重试策略和可观察预算；preview 读同一结构，create 在持久化前对相同资源代数/hash 复核。
- 10 Case×2 Cell、calls cap=5 的例子：preview/create 都拒绝；Run/Cell/Job/Provider 调用数为零，不允许 create 独自用默认 1 Case 估计。
- 非执行型声明与可强制限制区分；无法强制的 token/cost/time/stop 字段不能作为有效硬限制接受。严格货币限制依赖价格和计量完整度；未知不填零。
- 调用预算包括声明的重试和 Judge 独立调用；不重复累加 Case/Trial/Run 的同一费用。外部 Harness 原生费用不可观察时降级或拒绝，不能承诺账户级精确硬上限。
- on_first_failure/max_failures/wall_clock 的实际消费者逐项明确，未实现的非默认值创建期拒绝；能实现时取消只作用于所属 Cell/Run，并保存停止依据。
- 请求幂等表保存 operation+key+canonical_hash+resource_ref；跨 service 重建同 key 同 body 同结果，异 body 冲突。复用已有通用幂等层，不再保存另一份仅进程内字典作为最终保证。

**完成门：** preview/submit 不矛盾；未支持预算不静默接受；中断恢复不产生无授权第二次费用。

### M8-T04：统一现有比较页面和报告读取

**现有触点：** DirectLlmCompare.tsx、Gsm8kCompare.tsx、现有 ComparisonService、RunReportRef、API schema 和 client。

**实施范围：** 删除页面 collect/comparisonGate 的领域算法，但保留布局、模型列、Case 下钻。后端一次解析并固定 run_id+scoring_pass_id+evidence_hash；服务结果包含指标资格、合法分母、coverage、未知计量和币种。

**UI：** 同题数不同题集不得静默横比；评分器版本、数据内容、Judge、干预差异展示具体原因；未知 token 用未知状态而不是 0；按 currency 逐币种展示，未声明汇率不转换；报告请求失败显示错误，不悄悄作为空成本的成功比较。

**测试：** 真实 create_app 的 API 契约样本与前端集成测试共享；不仅 mock 一个理想 client 对象。对相同 Case 数但不同 IDs、同名数据异 hash、不同 scorer、历史 pass、USD/CNY、多币种和部分缺 usage 分别断言。

**完成门：** CLI、通用 Compare、旧 suite Compare 使用同一 fixed reference 得到相同资格与数值，前端没有独立的质量/可比性裁决。

### M8-T05：将已存在的执行器接入跨 suite 实验

**范围：** 只扩展现有 ExperimentService 的组装责任，不重新实现 Backend 或调度。建议顺序：GSM8K/C-Eval → Builtin Agent/Scenario/Skill → Harbor Trial → 需要明确控制语义的外部 Runtime。

每个 assembler 负责支持因子、资源解析、选样、预算与 manifest 输出；运行仍经现有 prepare_run/RunDispatcher。不同 suite 不强制采用同一个 prompt 形状。

**关键约束：** experiment repeat 创建 Cell/Run，Harbor Trial 在 Run 内；传输重试不是实验重复。不同 runtime 对 model_control/tools 的强制程度可能不同，比较时保留这一差异。任何组装器未就绪都整体拒绝，而不是退化成 Direct。

**完成门：** 选定原核心任务分别用 API/CLI 创建两配置实验并由 Worker 消费；终态、样本、评分引用与 standalone Run 一致；重复提交和恢复不重复分配。

### M8-T06：把统计与回归结果接到实际消费者

**进入条件：** 先核对当前 statistics/regression/report/export 的调用路径。已有消费者保留，不因计划假设而重写。

**范围：** 明确实际支持的指标统计：Case 配对差值、Task cluster 的不确定区间、合格 Trial 的 pass@k、成本与耗时分布。每项输出绑定输入报告、统计单位、n/k、missing policy、权重、方法版本、seed/重采样次数。

**边界：** smoke 样本不展示误导性的强排名；缺失/非独立重复/样本不足返回不适用原因；不把基础设施 retry 算入 n。统计政策在比较前固定，不自动调参直到门禁变绿。

**完成门：** 同一个冻结报告集经 API、CLI、Web、JSON/JUnit 可取得一致结果；不是只有 statistics.py 单测。Gate 只消费政策指定指标，不隐式改变样本或评分。

### M8-T07：代表性真实链路与正式基准验收

**已有证据要继承：** M2 的真实 OpenCompass+本地 HTTP、M3 的真实 Docker oracle、M4 的真 Pi SDK/原生协议均已记录。只补缺失范围，并在当前修复影响相关路径时重验，不把这些阶段全部重做。

| 验收卡 | 范围 | 必须留下的证据 |
|---|---|---|
| L1 原生 Agent | 两个已发布模型、相同文件任务；正常/工具恢复/预算停止/取消 | 实际 request identity、工具调用、文件 hash、停止与费用、固定 pass |
| L2 C-Eval | 可追溯官方数据/许可、固定 Profile；先有界真实 smoke，再完整承诺范围 | 数据/选样 hash、prompt/提取版本、Runner 锁、原始与诊断分数、coverage、实际费用 |
| L3 Harbor | 固定 Task 集、明确真实 Agent Profile；多 Trial、失败、超时、取消 | Task/Trial/Attempt 关系、Verifier 四态、原始工件、环境所有权、清理状态 |
| L4 Pi/Claude/Codex | 每个声明支持的 runtime 一个真实小任务及取消 | 原生版本/transport/session、真实模型路径、工具/计量可见度、产物与停止 |
| L5 业务场景/Skill | 成功+确认缺失+工具错误+恢复+越权；无 Skill/v1/v2 | Fixture 状态快照、隐藏 gold 分离、三臂约束、输出与成本、case-level 差异 |

执行卡先展示预估最大调用数、各类费用风险、指定 credential reference 与停止条件。缺权限或预算记录 blocked，不使用个人已有登录态推定授权。正式全量 Profile 与少量 smoke 明确不同，不强求人工提供不需要的真实业务机密。

### M8-T08：Judge 人工校准与正式门禁资格

**范围：** 使用现有 JudgeSpec/ScoringJob/校准模块与 UI。至少达到原计划 ≥30 条人工校准资料；这只是项目原定最低验收数量，不足以自动证明统计可靠性。

校准集版本固定，注明人审来源与复核结果；覆盖明显正确/错误、边界答案、缺证据、输出内诱导改变评分规则、pairwise 顺序交换和重复评分。合成标签仅作协议测试，不能重新标成人工标注。

**评估：** 按 rubric 定义同意率、逐 criterion 分歧与误放行/误拦截，记录位置偏差与稳定性；阈值在看最终结果前固定。开发/校准与验收数据分开，防止为过门禁反复调 rubric。

**费用与历史：** Judge 付费单列授权，不混入 subject 成本；失败/不确定不生成通过；人工修订/新校准结果追加记录与 ScoringPass，旧 Baseline 固定原 pass。没有校准证据时保留 experimental，并由正式 Gate fail-closed。

**完成门：** 公共入口能完成 preflight→显式授权→排队→Worker→固定 pass→校准/审阅→Gate 资格；真实 UI 复看不是只有组件 mock 测试。

### M8-T09：生产组合的安装、迁移与一致恢复

**范围：** 先选择一个明确的正式组合（建议 Linux/WSL2 测试环境按实际支持声明分别验证），在独立临时 PG/Artifact 目标中执行。

验收空库到 head、上一版本升级、拒绝危险降级、maintenance 屏障与在途任务处置、DB+Artifact 同一快照、staging restore/hash 核对、损坏备份拒绝、失败时不覆盖工作库、GC/pin/needs_review 保护。使用真实 pg_dump/restore 路径，不能以 PG CRUD 测试替代。

Compose 必须真实 build/up、migration、health、Worker、受控 Run 及关闭；保留依赖/镜像摘要与环境版本。macOS/Windows/WSL2 不互相替代支持证据。

**完成门：** 恢复后 Run、Case、Trial、Invocation、ScoringPass、Baseline、Artifact 引用/数量/hash 对账一致；只读历史报告能还原，正在执行的未知任务保持安全状态。

### M8-T10：真实旧导出迁移演练

**范围：** 从旧平台只读导出一份脱敏或经授权的代表数据；在一次性目标上 dry-run→apply→中断→resume→重复 apply→rollback。目标不是立即覆盖用户生产库。

必须核对资源引用映射、旧 alias 的实际固定身份、样本/评分器未知、历史 Run readonly、不进队列、共享 Artifact 引用、失败明细、导入器版本和来源 hash。配置记录是仅存档还是可发布成新的 Model/Dataset/Scenario，要在报告明确区分。

**完成门：** 本次新增记录可识别、重复无额外行、回退不删共享或既有对象；导入前后数量与 hash 可核对。真实密钥、会话和生产 DSN 不写入公开仓库。

### M8-T11：公共入口与支持矩阵最终对齐

**范围：** 复看当前设计系统 v2 合并后的页面。按用户流程覆盖 Provider/模型发布、Run、Agent、C-Eval、TB、Runtime、Workflow/Skill、Judge、Experiment/Compare/Gate、导出与运维报告。

使用同一真实 API 契约构造测试；桌面和声明支持的窄屏覆盖配置保留、预检拒绝、忙碌/失败、鉴权过期、SSE 恢复、最后事件、取消确认和 retry 子 Run。没有后端支持的按钮带具体原因，不能以灰色按钮替代原目标的实现。

状态索引生成支持矩阵并检查文档链接/命令；过期账本保留历史上下文，当前总结只引用最新验证。Trace DB 行裁剪等明确未实现项必须完成或批准范围修订，不能只改标签算完成。

### M8-T12：三个替代场景、RC 与切换准备

**三个替代场景：** C-Eval 多模型比较；Harbor Agent Benchmark；多轮业务/Skill 回归及 Gate。都使用冻结版本与可复核结果。

达到候选支持范围门后产生构建产物、哈希、release notes、已知限制、迁移与回退说明。官方数据/外部 CLI 未验收的项可以通过明确批准缩小发布范围，但不能同时声称原全部路线完成。

先输出 cutover-ready 评审材料；实际生产切换单独授权。停止旧新增开发、保留旧证据只读、不长期双写、失败回退不抹除新历史。归档/删除旧仓库不是本任务默认动作。

## 5. M8 核心验收反例矩阵

| ID | 输入/故障 | 预期行为 |
|---|---|---|
| A01 | 相同模型、prompt v1/v2 | 请求体现对应差异或创建拒绝不支持；不得只有 Cell 标签不同 |
| A02 | Direct suite 声明未接通 Skill/runtime 因子 | preview/create 一致拒绝，零 Run/模型调用 |
| A03 | 10 Case×2 Cell，calls cap=5 | preview/create 均拒绝潜在 20 调用，零可执行对象 |
| A04 | 预检后资源版本/模型上限改变 | 提交使用固定合法快照或明确 stale/conflict；不静默换配置 |
| A05 | 同 request_key 跨服务重启、同/异 body | 同 body 同结果；异 body 冲突；不重复调用 |
| A06 | 两 Run 题数相同但题集不同 | 中央比较说明不可比或明确受控公共子集，页面不得直接强排名 |
| A07 | 当前 pass 已变但比较引用旧 pass | 数值及证据保持旧引用；切换必须显式 |
| A08 | USD/CNY 混合、token 缺失 | 币种分开、未知显式；不固定 ¥、不把未知当 0 |
| A09 | Trial 数不足、retry 代替 Trial | pass@k 不适用并解释；不得补次数 |
| A10 | 评分输出包含 NaN/inf/缺值 | 校验或证据不足，不进入通过门禁 |
| A11 | 首个 Cell 失败触发 stop_policy | 只停止本实验；在途处置可查；无自动直到成功 |
| A12 | Oracle reward=1 但未调用真实 Agent | 标注校准证据，不自动升级真实 Agent 支持 |
| A13 | Judge 只有合成标签 | 不标 calibrated；正式 Gate 不据此放行 |
| A14 | 真实调用 dispatch 后进程丢失 | indeterminate/needs_review；不得自动再收费 |
| A15 | PG 降级前保留 ScoringJob/Trial 证据 | 拒绝且不删除；独立 guard 用例各命中目标 |
| A16 | 备份中 Worker 正在写 Artifact | 屏障/drain 或一致策略满足；manifest 与 DB 同一时点 |
| A17 | 旧导出重复导入再 rollback | 同源不重复；只回滚本次新增，保护共享对象 |
| A18 | Web 超过 500 终态事件或鉴权过期 | 排空/恢复游标或明确失败；不静默截断 |
| A19 | 测试环境无 symlink/Docker/PG | 精确条件状态；必需支持 job 不得以 skip 通过发布 |
| A20 | 一个增强后端没 live | 支持范围如实标记；不拖入所有核心完成或全部稳定声明 |

## 6. 推荐提交顺序与并行边界

T00 先冻结证据口径，随后 T01；T02/T03/T04 是第一批产品正确性修复。T05 与 T06 在这批接口稳定后推进。T07 的环境与数据准备可以并行，但真实费用操作只能在前置门通过和明确授权后执行。T08 人工标注准备可并行。T09/T10/T11 随相关功能验证，T12 最终收口。

推荐第一批 PR：

| PR | 边界 | 测试/验收 |
|---|---|---|
| PR-A | 三个 CI 失败与专用环境 fixture | 单独、组合、全量均通过；安全负例仍拒绝 |
| PR-B | 因子支持表与实际消费映射 | fake 请求/工具/运行时能看到差异；未支持因子拒绝 |
| PR-C | 共享预算编译与持久幂等 | A03/A04/A05；创建前零副作用 |
| PR-D | 两个旧比较页面接中央服务 | A06/A07/A08；生成类型及真实 API contract |
| PR-E | 统计消费者验证及首个跨 suite assembler | 原 standalone 与 Experiment 的冻结结果语义一致 |

各 PR 的验证记录应保存 SHA、node ID、命令、环境、原失败/修复后结果；原代码已有职责优先扩展，不因计划建议路径而新建重名服务。暂不对未来全部功能规定精确类名，避免跨阶段接口假设反过来驱动无谓重构。

## 7. M9：持续评测与失败集闭环（M8 后的新产品价值）

### 用户成果

对一个实际项目的模型/Prompt/Agent/Skill 改动，能够从变更关联到实验、结果、门禁和可复用失败样本；同样的任务可以周期性复测而不手工拼资源 ID。

### 模块与范围

| 模块 | 基于已有能力扩展什么 | 不重复建设 |
|---|---|---|
| 评测配方 | 固定 Scenario/模型与允许变量、样本选择、评分/Gate 政策，一键预览/运行 | 不另建 Experiment 或 Scenario |
| 变更关联 | 记录被测项目 commit、构建、Prompt/Skill 变更、依赖版本及外部追踪 ID | 不把平台代码 commit 当成被测系统 commit |
| 失败分析 | 目标错误/环境/工具/评分/证据缺失分类与筛选；人工复核结果追加 | 不在前端重新算质量或用 LLM 自动改 gold |
| 失败转回归集 | 选择 Case 与证据、去标识化、审核 expected、发布新 DatasetVersion、保留来源 | 不自动把 test gold 泄入训练或生产 prompt |
| 基线与周期回归 | 用现有 SDK/CLI 在 CI/定时 Runner 消费配方，固定 baseline/pass 和预算 | 不增加第二套任务队列；不默认频繁付费 |
| 报告与共享 | 可审计链接/脱敏导出、变化摘要、证据与限制 | 不生成不受证据支持的万能排名 |

### 最终目标

- [ ] M9-G01：一个真实项目至少两次受控变更可关联到独立评测 Run、报告与 Gate。
- [ ] M9-G02：一条配方经 Web/CLI/CI 使用相同解析与预算结果。
- [ ] M9-G03：至少一条确认失败可经审核进入新回归集，保留来源/hash 和隐私处理记录。
- [ ] M9-G04：原始失败、修复后结果、旧/新基线均可回看，不被重新评分指针覆盖。
- [ ] M9-G05：定时/CI 触发可限频、取消与防重复，无凭据或预算时拒绝，不静默使用别的账户。
- [ ] M9-G06：使用一段真实工作流数据确认日常操作成本下降，而不是只增加几个新页面。

建议先把 MoTTEavl 自己作为 dogfood 项目，用固定的小型 Direct/Agent/Scenario 回归配方保护后续改动。通用云连接器、消息通知集群和团队审批不作为第一批要求。

## 8. M10：只选一个新评测域

扩展优先级应由具体被测应用决定，而不是按 Benchmark 热度或新增目录数量。以下是备选范围，尚未指定全部必做。

| 候选 | 适合的实际问题 | 最小有效交付 |
|---|---|---|
| RAG / Embedding / Rerank | 需要定位检索、重排和答案生成分别出了什么问题 | 文档与索引/切片快照；检索/重排/生成分段 Observation；引用与人工审核答案；固定语料上的回归 |
| 图像/文档评测 | 产品已经使用图片或文档输入 | 资产 hash/版本、明确端点模态、预处理快照、结构化提取或产物验证；先单一模态，不同时上音视频 |
| 浏览器/桌面/Blender | 有可控任务环境与可验证最终状态/产物 | 小型版本化任务包、明确执行器、动作/状态证据、环境清理、Task verifier；沿用已有 Task/Trial |
| 线上 Trace→离线回归 | 有实际应用轨迹并取得数据使用授权 | 只读导入、脱敏、样本审核与发布，保留缺失与观察范围，不声称无损重放所有副作用 |

**选择门：** 必须能回答被测对象、任务集来源、成功/失败判据、可观察证据、成本和安全边界六个问题。只有真实项目已有明确需求时才进入；否则优先 M9 日常闭环。

## 9. 范围与发布策略

原 M1–M7 未闭合内容不能因取名 M8 就从原完成度中消失。若先发布限定范围的 Preview/RC，应写清已支持组合与未支持能力；Preview 可以使用，但不能同时宣称全路线 stable_supported/cutover_ready。

M8 关闭功能正确性与支持证据门后，M9 新功能可以独立版本推进。外部 full Profile 或人工标注暂时不可用时，可并行进行不依赖它们的开发，但保留验收阻断，不把环境限制转写为通过。

**下一步最值得落地的是：CI 隔离修复 → 实验因子真正生效 → 预算创建与预览一致 → 旧比较页面统一。** 然后用正式代表性链路和真实恢复/迁移证明整个产品，而不是再进行无边界架构加固。

## 10. 依据

本规划从配套评估的 F-01–F-06、原 M1–M7 G/T/A 和当前 CI 得出。它不宣称执行了任何建议验证。

- [当前 CI](https://github.com/BaiZhi967/MoTTEavl/actions/runs/35702579654)
- [Experiment 实际支持与装配](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/packages/sdk-python/motte_sdk/experiments.py)
- [Experiment 允许因素与预算契约](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/packages/contracts/motte_contracts/experiment.py)
- [原 M6 全范围](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/roadmap/M6-experiments-comparison-and-gates.md)
- [原 M7 发布要求](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/roadmap/M7-sdk-migration-and-release.md)
- [修复后测试记录，第 12 节](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/review/2026-09-22-m1-m7-test-report.md)
- [支持矩阵](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/release/support-matrix.md)
