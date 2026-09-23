# MoTTEavl M1–M7 完成度评估与需求对照

日期：2026-09-22（UTC+08:00）  
审查仓库：BaiZhi967/MoTTEavl  
固定主干：`fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d`  
范围：原 M1–M7 需求、现有验收账本及其修订、关键源码消费路径、当前提交 CI。  
操作：只读；本次没有提交代码、改变数据库、触发 Provider/Harness/Judge 付费调用或执行切换。

## 1. 结论

**不能确认 M1–M7 已完全完成原路线的全部内容。** 更准确的状态是：主要实现与相当一部分离线/集成能力已交付；部分代表性真实模型链路有记录；仍存在当前源码可见的功能边界与一致性缺口、最新 CI 失败，以及正式支持/迁移切换验收缺失。

这不是把项目退回骨架阶段。Dispatcher、不可变快照、CaseAttempt、Trial、ScoringPass、Agent、外部 Job、场景/Skill/Judge、Experiment/比较/Gate、SDK 和运维已有实质产物。下一步应完成有停止条件的交付收口，而不是重建整个内核。

**本次证据范围：** 已逐项对照七份原始目标清单，共 137 项；检查当前关键源码和 CI 日志，并读取阶段验证记录。附录是需求证据对照，不是本次独立执行的 137 条测试。本次未在本地重跑全仓测试或真实环境：容器无法解析 GitHub 域名取得完整源码；以 GitHub 连接器读取的代码、实际 CI 日志及仓库已有验证记录为依据。历史报告的 passed 只证明其记录的版本/环境/范围，不自动证明当前所有组合。

后续路线见同目录的 `MoTTEavl_Next_Stage_Plan_2026-09-22.md`。

## 2. 判定标准

| 层级 | 意义 | 不允许的替代 |
|---|---|---|
| implementation_evidenced | 有实际实现及可定位消费者 | 目录/类名/规划文档不能替代接线 |
| offline_verified | 固定输入和合成依赖的行为测试 | 不能替代真实上游协议 |
| integration_verified | 真实 API/Worker/数据库/Runner/环境的指定组合 | 仅 compose config 不能替代 build/up |
| live_verified | 指定模型/运行时/任务的实际运行与停止证据 | --version、握手、oracle 不能替代真实模型任务 |
| stable_supported | 明确支持范围的退出门全部满足，无已知阻断 | 旧 CI 绿或大多数测试绿不能替代当前门禁 |
| cutover_ready | 旧平台替代场景、迁移、恢复与切换条件满足 | 写好 runbook 不能视为已演练/已切换 |

原 M4 的交互命令、Inspect 以及 M2 的 CMMLU 是原路线增强项；当前账本记录它们已做了实现。它们有实现不代表 live 验收完成；是否进入正式支持范围应另有明确记录，而不是静默增加或删掉发布门。

## 3. 当前版本 CI：实际失败，不是历史状态

CI run `35702579654`，HEAD 与本次审查相同。2026-09-22 16:09:21（UTC+08:00）结束。

| Job/步骤 | 本次读取结果 |
|---|---|
| python / make check | failure |
| Python non-live pytest | **3 failed / 2938 passed / 30 skipped / 1 deselected** |
| packaging | success，wheel 构建及 checkout 外 clean venv 安装通过 |
| web | success，构建/测试/audit/生成 TS 类型检查通过 |
| python job 后续 pip-audit、Trivy、OpenAPI drift | skipped；不能写成此次均已验证 |

来源：[CI-run](https://github.com/BaiZhi967/MoTTEavl/actions/runs/35702579654)、[Python 日志](https://github.com/BaiZhi967/MoTTEavl/actions/runs/35702579654/job/106663837333)。这批数据来自既有 CI，不是本次会话在本地运行的结果。

### 三个失败的初步归因

1. `tests/scenario/test_fixture_lifecycle.py::test_prepare_materializes_owned_state_and_marker`：预期 POSIX backend=sandbox，实际 portable。当前 `ControlledRoot.__init__` 默认 portable；`create=False` 走 `_validate_existing` 而不进入 `_ensure_created` 的 fd 路径。因此首先要对齐“新建/重新打开”的实际安全能力与 backend 报告语义，不应仅按操作系统改成随便通过。尚未据此证明任意文件逃逸。
2. `tests/storage/test_scoring_jobs.py::test_postgres_scoring_job_transactions`：测试覆盖了 invocation 顶层 run_id，却保留辅助 fixture 中 owner.run_id=run-1，触发 `subject invocation owner must match the run`。修复应统一测试所有关联 ID，并保留真实归属校验。
3. `tests/storage/test_trials_downgrade_guard.py::test_postgres_trials_downgrade_guard`：共享 PG 库仍有 3 行 ScoringJob，较新 revision 的降级保护先阻断；测试原本期望命中 trials。应使用独立数据库或受控独立命名空间和可靠 teardown，使每个守卫测试命中预期条件；不能删除真实证据或放宽 downgrade guard 来换绿。

上述是当前日志与源码对照形成的归因；修改前仍需独立/组合顺序复现。即便失败在 M6/M7 之前就存在，也只能影响责任归属，不能豁免整仓发布门。

最新本机验收报告另记 Windows `14 failed / 2860 passed / 97 skipped / 1 deselected`，其中 13 个在创建 symlink 时因权限不足停止，1 个与 Docker cleanup 的 unknown/residual 预期有关。它不是本次 Linux CI 的失败数，不相加、不混用。见 E9 第 12 节。

## 4. 已修复的问题：不要重开旧清单

E5 修订附录记录旧库升级、NULL 评分、fixture/skill 发布、Scenario frozen_observation、步骤端点和真实工具 wire history 的修复与复测。

E8 第 9 节及 E9 第 12 节进一步覆盖 Provider redirect、Artifact 路径、GC 引用、Origin、持久证据失败、PG 锁/恢复、maintenance、Sandbox、SSE 分页、Judge 页面契约、Windows Harness、Provider 重试/流状态、取消等。当前 Judge 页面确实使用新的 run/spec/authorisation 形状，不能继续按旧 405 报告说整页不可用（C9）。

这些历史缺陷在本报告中归入“有修复/定向验证记录”，不是当前未修复清单；也不据此给整个安全面出具完全无缺陷保证。部分数据库/Docker/真实页面复验仍由指定环境收据证明。

## 5. 仍存在的实现与产品接线差距

### F-01 高优先：实验变量接受了，但未必真正改变执行

C4 的 `EXPERIMENT_FACTORS` 允许 `model_profile/reasoning_level/prompt_version/runtime_version/skill_version`。C3 的 `_build_manifest` 只把 model_profile 和 reasoning_level 写进执行 manifest，没有消费后三者。

结果是：在已允许的 Direct LLM 实验中，改变 prompt_version 等可以形成不同 Cell ID，但执行配置仍相同。这不是“暂不支持某个后端”这么简单，而是实验自变量与真实处理条件失配。

范围：当前源码路径的静态结论，未在本次执行真实模型复现。关联 M6-G01/G04/G18，以及 M5 Skill 对照的后续统一实验接入。

修复先后：先按 suite 声明支持因子，不支持的已声明因子在 preview/create 同时拒绝；随后每接一个因子，都证明它固定进对应资源快照并被执行器消费。反例：同模型、prompt v1/v2 的两个 Cell 必须产生对应不同 prompt；不能仅不同 Cell hash 就视为实现。

### F-02 高优先：Experiment 仅支持 Direct LLM

C3 明确 `SUPPORTED_SUITES = {'direct-llm'}`，非该 suite 返回 SUITE_UNSUPPORTED。该拒绝行为安全且正确，但不等于实现了原路线的跨 Agent/Skill/Benchmark 实验系统。

关联 M6-G01/G02/G03 的完整范围。不要去掉 allowlist 后默认复用 Direct 组装；应在现有服务中逐个加入 suite assembler，复用既有 prepare_run、外部 Job 和 Dispatcher。

### F-03 高优先：预算预览与创建服务不是同一套实际样本数校验

C3 的 preview 用 `_scenario_case_count` 解析真实样本数并追加 max_potential_calls 违规；create 仅调用 `_violations(spec)`，后者读取 C4 `max_potential_calls`，空选择按 1 个样本估算。create 未复用 preview 中真实样本数的那段拒绝逻辑。

服务层反例设计：数据集 10 题，两个模型 Cell，空 selected_case_keys 表示全集，max_total_calls=5。preview 按 20 次潜在调用报超限；create 的初始检查只看到 2 次，可能创建两条各 10 题的 Run。原子整体拒绝的语义因此没有在服务层一致落实。此为源码静态风险，公共 HTTP/CLI 需补零调用复现，不在本次声称已实际消费额度。

关联 M6-G01/G02。建议共用一个零副作用 compile/resolve/preflight 结果，create 在持久化前对同一快照重校验。另审计 token/cost/stop_policy 是否具有真正消费者，避免“字段存在=预算已经强制”的推断。

### F-04 高优先：旧比较页绕过统一比较与固定评分引用

C1、C2 都从 getRun/getReport 取数据，在浏览器 collect() 内重新统计。

- Direct LLM 有数据集版本和 Case ID 集合检查，但没有中央 ComparisonPolicy 的全部评分器/内容/干预/身份资格。
- GSM8K 主要用题数差异做提示；相同题数而不同题集没有被该条件识别。
- 两页直接使用 run.scores，未把固定 ScoringPass 当作明确比较输入；不是所有比较都绑定同一不可变报告引用。
- 缺少 total_tokens 的调用用 0 加总，且成本固定加 `¥`，没有依据实际币种展示。

关联 M6-G04/G05/G07/G08/G12/G19/G21。M6 的四个新页面走中央服务，不能抵消旧入口仍绕开的事实。

最小修复：原页面保留布局和下钻，列与资格来自公共 ComparisonResult/RunReportRef；删除页面私有比较算法；unknown usage 与币种交给统一格式；新增“相同数量不同题集、不同 scorer、旧 pass、USD/unknown usage”的真实 API 契约测试。

### F-05 需专项复核：统计、停止政策和幂等作用域的生产消费者

E6 记录 statistics/regression 有测试，C5 的主要比较服务接了 regression，但本轮没有取得全部统计输出进入正式报告/导出的完整运行证据。不能把“函数存在且单测通过”当作所有统计目标完成，也不能仅据某个文件未 import 就断言全仓没有消费者。

C3 的 request_key 映射还是服务实例内字典；同一个 experiment version 的 Cell/Run 稳定身份已有持久保护，但这不等同于“同 request_key 跨进程、跨重启、不同 spec”的完整请求幂等约束。

本项列为需专项核实，不与已确认的 F-01–F-04 混成同一确定程度。

### F-06 范围与证据账本需收口

E10 对 trace DB 行裁剪明确记 not_implemented，真实旧导出、PG backup/restore、Compose build/up、cutover 未完成。需要判断哪些属于当前正式支持承诺、哪些显式调整范围。支持矩阵仍有历史“本机没做”与其他阶段 macOS/Docker/PG 已验证并存，必须按提交和环境定位，不能把某台 Windows 机器无 Docker 等同全项目从未做 Docker。

## 6. 分阶段评估

| 阶段 | 已有实质成果/验证 | 原路线仍未闭合 | 判断 |
|---|---|---|---|
| M1 | Builtin Agent、多指标、工具/文件/预算/调用日志；E5 真实 4 次模型+3 次工具闭环及预算停止；E1 有早期 PG 确定性集成 | 双模型同任务和真实取消的 Supported 收据不齐；当前版本回归与安全环境边界仍需统一验收 | 主链可用，不能把 Supported 全部打勾 |
| M2 | 外部 Job、目录/Profile、C-Eval/CMMLU Parser；E2 有真实 OpenCompass 0.4.2+本地 HTTP 12 项、真实 PG 并发记录 | 官方来源/许可、固定 full Profile、真实模型 smoke/full 仍无完整闭合收据；新旧 Parser 全差异表需可追溯 | 不再是只有 fake Runner，但正式基准验收未齐 |
| M3 | Task/Trial/Verifier/工件；E3 真 Harbor 0.23.0+Docker oracle 通过/失败/超时捕获 | oracle 不替代受测真实 Agent；固定官方任务集、小批次模型、取消清理与指定支持组合的验收未齐 | 环境/解析基础成立，真实 Agent Benchmark 支持未全验 |
| M4 | 真 Pi SDK、CLI supervisor、Codex app-server @2 consumer、Inspect v2 导入均有实现/离线记录 | Pi/Claude/Codex 各自真实任务、鉴权/停止/产物/额度证据仍 live_pending | implementation_complete 是账本范围，不能升格全部 live_verified |
| M5 | Workflow、Fixture/Skill 发布、状态断言、Judge job 与更正后的 UI/API | ≥30 人工校准及位置偏差/稳定性证据；完整三臂实验、人工修订和页面实际闭环需补验 | 主模块落地，校准与产品验收仍不足 |
| M6 | compare/coverage/baseline/gate、Direct 实验分配、统计库、CLI/新页面；有有界真实模型实验 | F-01–F-04 为当前源码差距；统计/stop/幂等消费者需复核；不是仅缺 live | 必须补实现，不能按全部完成验收 |
| M7 | 同步 SDK、双模式 CLI、wheel/clean venv、合成迁移、维护/安全；当前 packaging job 成功 | 全仓 gate 失败；PG 完整恢复、真实旧导出、支持矩阵、三个替代场景与切换未执行 | 工具已建设，稳定发布及切换未完成 |

M1 中“Docker 验证”包括以容器跑 PG；原生 Agent 的受控文件工具与任意代码的容器隔离不是同一安全能力。正式声明要按实际任务执行边界记录，不能用数据库容器通过替代被测任务沙箱验收。

## 7. 建议的处理顺序

1. 当前 CI 三项失败和专用环境测试归属收口，不削弱保护。
2. F-01 因子真实性、F-03 创建预算、F-04 统一比较先修；这是下一批最有价值的代码。
3. 按现有架构补 suite assembler、统计与停止政策的消费者验证。
4. 分层执行 M1 双模型、C-Eval full、Harbor 真实 Agent、三运行时、Skill 三臂与 Judge 人工校准。
5. PG/Compose/旧导出/恢复/切换实演，统一 137 项证据映射，再决定明确范围的 RC。

这是原 M1–M7 债务收口，不应通过新取一个 M8 名字把旧义务从完成率里删除。通过后再推进长期回归集与实际应用评测等新功能。

## 8. 137 项目标证据对照（非独立测试通过清单）

状态解释：

- **E：有阶段实现/验证记录**。本次可定位来源，但没有独立重跑该目标的全部测试，不代表 stable 通过。
- **I：当前实现/产品接线有明确差距**。正文给出源码路径和影响。
- **V：原路线要求的真实环境/支持/迁移验收未闭合**。不是断言相关代码不存在。
- **R：跨模块消费者或完整证据需要专项复核**。不将证据不足伪装成通过，也不断言全无实现。
- **O：增强项已有实现记录**。按实际选择的支持范围验收，不混入核心未实现统计。

同一目标可以由多个工作包贡献。表中采用最需要处理的状态；不对这些混合层级计算一个虚假的总完成率。

### M1（18 项）

需求来源：[R1](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/roadmap/M1-native-agent-and-evaluation.md)。主要实现/验证记录：[E1](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/verification/M1.md)；最新修订同时参考 [E8](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/review/2026-09-22-m1-m7-code-feature-review.md)、[E9](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/review/2026-09-22-m1-m7-test-report.md)。

| 目标 | 原目标摘要 | 证据评估 | 说明/下一步 |
|---|---|---|---|
| M1-G01 | 注册并实际执行 builtin-agent@1 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M1-G02 | 模式、prompt、工具、预算及模型快照固定 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M1-G03 | native-tool 不支持时预检拒绝且不降级 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M1-G04 | 每 Case 消息和 workspace 隔离 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M1-G05 | 模型/工具/拒绝/终止持久证据 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M1-G06 | 工具错误回灌、恢复与未知工具拒绝 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M1-G07 | 步数/时长/工具/可观察 token 预算 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M1-G08 | 取消阻断后续动作并记录清理 | V | 离线取消与部分历史实测存在；双模型 Supported 的实际取消/清理收据仍需固定版本补齐。 |
| M1-G09 | 不确定副作用进入 needs_review | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M1-G10 | Artifact 安全路径、类型、大小、hash、可用状态 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M1-G11 | 同 Observation 多指标及版本/分母 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M1-G12 | 缺期望/缺证据/评分错误不当通过 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M1-G13 | 追加评分、报告只读、rescore 不调 subject | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M1-G14 | gold/隐藏规则/凭据不进模型输入 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M1-G15 | Web/CLI/Worker/API 同一状态和分数 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M1-G16 | 正常/恢复/越权预算代表任务验收 | V | 三类任务已有历史正向/预算实测；原双模型范围与当前支持环境未完整复验。 |
| M1-G17 | Direct/GSM8K/Replay 评分恢复无退化 | R | 既有专项有记录；当前总 CI 未全绿，需跑受影响域并保存 node ID。 |
| M1-G18 | 支持声明包含版本、环境和分层证据 | V | E1 明确 Supported 未满足；后续 E5/E9 补了单模型，不能自动扩展为所有支持组合。 |

### M2（17 项）

需求来源：[R2](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/roadmap/M2-llm-benchmarks-and-ceval.md)。主要实现/验证记录：[E2](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/verification/M2-R4-fixes-2026-09-20.md)；最新修订同时参考 [E8](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/review/2026-09-22-m1-m7-code-feature-review.md)、[E9](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/review/2026-09-22-m1-m7-test-report.md)。

| 目标 | 原目标摘要 | 证据评估 | 说明/下一步 |
|---|---|---|---|
| M2-G01 | 一 Job 不逐题重启 Runner | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M2-G02 | 持久化 Job 句柄、所有权、配置与 cursor | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M2-G03 | 启动不确定/非零/缺结果/部分/取消处理 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M2-G04 | 重复导入幂等、异内容冲突 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M2-G05 | Catalog 四级状态与阻断原因 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M2-G06 | Dataset/Profile/Runner/scorer/选样冻结 | V | 本地受控资源与 E2 真 Runner 测试成立；正式官方数据/Profile/live 范围仍需收据。 |
| M2-G07 | 数据与代码许可/来源分开 | V | 本地受控资源与 E2 真 Runner 测试成立；正式官方数据/Profile/live 范围仍需收据。 |
| M2-G08 | C-Eval 学科、split、few-shot、提取及 token 解释 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M2-G09 | 目标 gold 与模型输入隔离 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M2-G10 | 原始 Runner 与诊断指标分离 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M2-G11 | 每 selected Case 有 disposition | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M2-G12 | 调用路径/身份/费用覆盖/重试证据 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M2-G13 | fake、真 Runner 确定性、live 分层验收 | V | 本地受控资源与 E2 真 Runner 测试成立；正式官方数据/Profile/live 范围仍需收据。 |
| M2-G14 | C-Eval 五类页面与公共组件 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M2-G15 | M6-Lite 覆盖与阈值 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M2-G16 | 新旧 Parser 对照及版本化差异报告 | R | 需取得可复核的新旧解析差异总表；不以一般 Parser 单测代替迁移语义验收。 |
| M2-G17 | 关闭 adapter 不影响旧功能及历史 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |

### M3（18 项）

需求来源：[R3](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/roadmap/M3-harbor-and-terminal-bench.md)。主要实现/验证记录：[E3](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/verification/M3.md)；最新修订同时参考 [E8](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/review/2026-09-22-m1-m7-code-feature-review.md)、[E9](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/review/2026-09-22-m1-m7-test-report.md)。

| 目标 | 原目标摘要 | 证据评估 | 说明/下一步 |
|---|---|---|---|
| M3-G01 | TB 版本/revision/task hash/runner 固定 | V | E3 有真 Harbor/Docker 的自有 oracle 任务；官方固定任务×真实 Agent 支持范围需独立验收。 |
| M3-G02 | 受控任务准备且不自动执行脚本 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M3-G03 | 任务唯一身份不依赖 basename | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M3-G04 | Trial 与 transport/operator retry 区分 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M3-G05 | Case/Trial/Attempt/Score/Artifact 关联 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M3-G06 | Agent/model/tools/env/verifier/budget 快照 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M3-G07 | 同 Dispatcher 管理多 Task/Trial Job | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M3-G08 | reward 0/缺失/畸形/Verifier 错误四态 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M3-G09 | 正常进程退出不当质量通过 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M3-G10 | 部分失败保留证据及完整 dispositions | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M3-G11 | 取消/超时/残留清理可追溯 | R | 取消/残留已有测试及修复；最新环境 cleanup unknown 语义仍需专项确认。 |
| M3-G12 | 不确定句柄或副作用保守恢复 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M3-G13 | 原始日志/轨迹/工件完整度 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M3-G14 | 隔离/gold/凭据/网络实际记录 | V | E3 有真 Harbor/Docker 的自有 oracle 任务；官方固定任务×真实 Agent 支持范围需独立验收。 |
| M3-G15 | 指标分母/单位/覆盖/重复次数 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M3-G16 | Task/Trial/终端/工件评分界面 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M3-G17 | 确定性+真 Docker+真 Agent 分层验收 | V | E3 有真 Harbor/Docker 的自有 oracle 任务；官方固定任务×真实 Agent 支持范围需独立验收。 |
| M3-G18 | 按数据/Agent/环境组合登记支持 | V | E3 有真 Harbor/Docker 的自有 oracle 任务；官方固定任务×真实 Agent 支持范围需独立验收。 |

### M4（18 项）

需求来源：[R4](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/roadmap/M4-pi-and-external-harnesses.md)。主要实现/验证记录：[E4](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/verification/M4-completion-review-2026-09-21.md)；最新修订同时参考 [E8](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/review/2026-09-22-m1-m7-code-feature-review.md)、[E9](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/review/2026-09-22-m1-m7-test-report.md)。

| 目标 | 原目标摘要 | 证据评估 | 说明/下一步 |
|---|---|---|---|
| M4-G01 | Runtime 实现/协议/配置版本固定 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M4-G02 | installed/protocol/execution readiness 分开 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M4-G03 | 真正使用固定版本 Pi SDK | V | E4 有实际 SDK/原生协议/进程证据，但正式运行时的真实模型小任务与停止验收仍 live_pending。 |
| M4-G04 | Case session/workspace 隔离 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M4-G05 | Pi 模型调用路径及工具权限 | V | E4 有实际 SDK/原生协议/进程证据，但正式运行时的真实模型小任务与停止验收仍 live_pending。 |
| M4-G06 | CLI transport/binary/native/cwd 固定 | V | E4 有实际 SDK/原生协议/进程证据，但正式运行时的真实模型小任务与停止验收仍 live_pending。 |
| M4-G07 | 自动发现配置/Skill/MCP/插件有记录 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M4-G08 | 事件 source/seq/parser/完整度 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M4-G09 | stdout/stderr 有界并行消费 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M4-G10 | 取消/超时/进程树/残留测试 | V | E4 有实际 SDK/原生协议/进程证据，但正式运行时的真实模型小任务与停止验收仍 live_pending。 |
| M4-G11 | 无终态或副作用不确定不自动重放 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M4-G12 | Artifact 与评分非退出码判通过 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M4-G13 | 未知模型/usage/cost 不伪造 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M4-G14 | Harness 控制差异进入可比性 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M4-G15 | API/CLI/Web 配置运行证据及 retry | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M4-G16 | 正式后端 offline fixture 与真实小任务 | V | E4 有实际 SDK/原生协议/进程证据，但正式运行时的真实模型小任务与停止验收仍 live_pending。 |
| M4-G17 | 交互消费者接通后才开放 ack | O | @2 consumer 和干预审计有实现记录；交互为原增强项，真实审批/取消按支持声明另验。 |
| M4-G18 | 人工干预/批准/提示进入审计比较 | O | @2 consumer 和干预审计有实现记录；交互为原增强项，真实审批/取消按支持声明另验。 |

### M5（22 项）

需求来源：[R5](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/roadmap/M5-scenarios-skills-and-judges.md)。主要实现/验证记录：[E5](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/verification/M1-M5-acceptance-report-2026-09-21.md)；最新修订同时参考 [E8](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/review/2026-09-22-m1-m7-code-feature-review.md)、[E9](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/review/2026-09-22-m1-m7-test-report.md)。

| 目标 | 原目标摘要 | 证据评估 | 说明/下一步 |
|---|---|---|---|
| M5-G01 | Scenario 引用发布 WorkflowVersion | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M5-G02 | 有界顺序/分支/循环/检查点 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M5-G03 | 受限表达式禁止 eval/import/shell | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M5-G04 | Fixture 所有权和 prepare/reset/cleanup | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M5-G05 | 多轮上下文及 Case 隔离 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M5-G06 | 步骤期限实际中断 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M5-G07 | 工具模式与有效权限固定 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M5-G08 | 工具参数/顺序/确认/状态/副作用评分 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M5-G09 | 隐藏 checker/gold 与 subject 隔离 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M5-G10 | 指令/资源/可执行 Skill 分别验证 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M5-G11 | Skill 内容/依赖/hash/顺序/权限固定 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M5-G12 | Skill fixture 输入输出/副作用/清理 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M5-G13 | 无 Skill/v1/v2 受控对照 | V | 需完整三臂/成本及业务流程收据；G18 另需至少 30 条真实人工校准（项目门，不是通用充分样本量）。 |
| M5-G14 | Skill token/tool/cost 与质量共同报告 | V | 需完整三臂/成本及业务流程收据；G18 另需至少 30 条真实人工校准（项目门，不是通用充分样本量）。 |
| M5-G15 | Judge profile/rubric/params/evidence 固定 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M5-G16 | Judge 畸形/拒绝/错误不判通过 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M5-G17 | Judge 独立授权/预算/不确定性 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M5-G18 | 人工校准/位置交换/非合成人工真值 | V | 需完整三臂/成本及业务流程收据；G18 另需至少 30 条真实人工校准（项目门，不是通用充分样本量）。 |
| M5-G19 | 人工修订/rescore 追加评分不改基线 | R | 追加评分基础存在；人工修订公共入口到冻结 pass/旧基线的一体化证据需补验。 |
| M5-G20 | 完整业务主流程及失败恢复验收 | V | 需完整三臂/成本及业务流程收据；G18 另需至少 30 条真实人工校准（项目门，不是通用充分样本量）。 |
| M5-G21 | 场景/Skill/Judge 公共配置执行下钻 | R | 旧 Judge 405 已修；最新报告仍注明最终桌面/移动复看未重验，不能按旧错误或按自动通过判断。 |
| M5-G22 | 原 suite 契约及恢复回归 | R | 原套件局部回归存在；当前整仓门禁仍有失败。 |

### M6（21 项）

需求来源：[R6](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/roadmap/M6-experiments-comparison-and-gates.md)。主要实现/验证记录：[E6](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/verification/M6.md)；最新修订同时参考 [E8](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/review/2026-09-22-m1-m7-code-feature-review.md)、[E9](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/review/2026-09-22-m1-m7-test-report.md)。

| 目标 | 原目标摘要 | 证据评估 | 说明/下一步 |
|---|---|---|---|
| M6-G01 | Experiment 任务/因子/预算/评分/停止政策固定 | I | F-01/F-02/F-03：因子声明/执行不一致、suite 仅 Direct、预算创建校验需闭合。 |
| M6-G02 | 矩阵预览校验、幂等分配与恢复 | I | F-01/F-02/F-03：因子声明/执行不一致、suite 仅 Direct、预算创建校验需闭合。 |
| M6-G03 | Experiment 复用 Dispatcher | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M6-G04 | 执行指纹与允许变化比较条件分开 | I | F-04：中央服务有实现，但旧比较入口仍绕过，固定 pass/资格/unknown/币种不统一。 |
| M6-G05 | 三级可比性及具体原因 | I | F-04：中央服务有实现，但旧比较入口仍绕过，固定 pass/资格/unknown/币种不统一。 |
| M6-G06 | 稳定样本内容匹配与增删变化 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M6-G07 | suite 合法分母保持 | I | F-04：中央服务有实现，但旧比较入口仍绕过，固定 pass/资格/unknown/币种不统一。 |
| M6-G08 | disposition 与覆盖完整展示 | I | F-04：中央服务有实现，但旧比较入口仍绕过，固定 pass/资格/unknown/币种不统一。 |
| M6-G09 | 缺失/失败不能静默提分 | I | F-04：中央服务有实现，但旧比较入口仍绕过，固定 pass/资格/unknown/币种不统一。 |
| M6-G10 | 统计单位/权重/区间/seed 可追溯 | R | 统计函数和资格测试有记录；正式报告/导出消费者的全链路仍需专项举证。 |
| M6-G11 | Trial/retry 区分及 pass@k 资格 | R | 统计函数和资格测试有记录；正式报告/导出消费者的全链路仍需专项举证。 |
| M6-G12 | 成本币种/计量/unknown 分离 | I | F-04：中央服务有实现，但旧比较入口仍绕过，固定 pass/资格/unknown/币种不统一。 |
| M6-G13 | Baseline 固定 Run/Pass/Policy | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M6-G14 | 新失败/修复/持续失败/不稳定/未覆盖 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M6-G15 | 版本化 GatePolicy 与规则政策 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M6-G16 | 六类 Gate 结论区分 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M6-G17 | 相同证据和政策确定求值且零模型 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M6-G18 | 干预/runtime/Judge 变化影响比较 | I | F-01/F-02/F-03：因子声明/执行不一致、suite 仅 Direct、预算创建校验需闭合。 |
| M6-G19 | 全部 UI/CLI 共用比较 Gate 服务 | I | F-04：中央服务有实现，但旧比较入口仍绕过，固定 pass/资格/unknown/币种不统一。 |
| M6-G20 | CI 非零原因与 completed/pass 区分 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M6-G21 | 升级/重评/导入/删除证据不改历史结论 | I | F-04：中央服务有实现，但旧比较入口仍绕过，固定 pass/资格/unknown/币种不统一。 |

### M7（23 项）

需求来源：[R7](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/roadmap/M7-sdk-migration-and-release.md)。主要实现/验证记录：[E7](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/verification/M7.md)；最新修订同时参考 [E8](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/review/2026-09-22-m1-m7-code-feature-review.md)、[E9](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/review/2026-09-22-m1-m7-test-report.md)。

| 目标 | 原目标摘要 | 证据评估 | 说明/下一步 |
|---|---|---|---|
| M7-G01 | 类型化同步客户端/错误/版本/能力 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M7-G02 | 公共 API 生命周期报告比较 Gate 可用 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M7-G03 | 付费 POST 安全重试/请求幂等 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M7-G04 | server 失败不转 local 执行 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M7-G05 | SSE 补页/游标恢复/最后事件 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M7-G06 | pytest 默认不付费/明确 opt-in | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M7-G07 | JSON/JUnit/退出语义完整 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M7-G08 | wheel/Web 干净安装无隐式 PYTHONPATH | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M7-G09 | workspace 依赖与 Runner 隔离 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M7-G10 | 旧资源 dry-run/映射/诊断/幂等 | R | 合成来源迁移有记录；实际旧导出与资源映射的可执行性/只读边界需核对。 |
| M7-G11 | 仅迁 SecretRef 不迁真实秘密 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M7-G12 | 历史 Run/Score/Artifact 只读不入队 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M7-G13 | 历史未知保持未知不反推 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M7-G14 | 导入恢复与回退不删共享资产 | V | 合成/SQLite 局部已验证；真实 PG/旧导出/正式支持和切换门尚未闭合，不能据此宣告完成。 |
| M7-G15 | DB+Artifact 一致备份/校验 | V | 合成/SQLite 局部已验证；真实 PG/旧导出/正式支持和切换门尚未闭合，不能据此宣告完成。 |
| M7-G16 | 支持环境升级/回退/旧 schema 演练 | V | 合成/SQLite 局部已验证；真实 PG/旧导出/正式支持和切换门尚未闭合，不能据此宣告完成。 |
| M7-G17 | 本地/远程认证与加密边界 | E | 阶段记录有对应实现/行为测试；当前版本完整验收仍须核对实际消费者与指定环境。 |
| M7-G18 | retention 保护 pinned/needs_review 并审计 | R | Artifact 引用保护已有修复；Trace DB 行裁剪明确未实现，需确定承诺范围与完成条件。 |
| M7-G19 | 发布秘密/私有数据/依赖/许可审查 | V | 合成/SQLite 局部已验证；真实 PG/旧导出/正式支持和切换门尚未闭合，不能据此宣告完成。 |
| M7-G20 | 正式后端各版本环境有真实证据 | V | 合成/SQLite 局部已验证；真实 PG/旧导出/正式支持和切换门尚未闭合，不能据此宣告完成。 |
| M7-G21 | 三个旧平台替代场景与迁移归类 | V | 合成/SQLite 局部已验证；真实 PG/旧导出/正式支持和切换门尚未闭合，不能据此宣告完成。 |
| M7-G22 | 文档/样例/矩阵与真实能力一致 | R | 存在滚动账本与修订附录状态差异，需按提交/环境归一并生成支持矩阵。 |
| M7-G23 | 功能切换与历史迁移分别验收 | V | 合成/SQLite 局部已验证；真实 PG/旧导出/正式支持和切换门尚未闭合，不能据此宣告完成。 |

## 9. 固定版本来源索引

- [R1](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/roadmap/M1-native-agent-and-evaluation.md) — `docs/roadmap/M1-native-agent-and-evaluation.md`
- [R2](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/roadmap/M2-llm-benchmarks-and-ceval.md) — `docs/roadmap/M2-llm-benchmarks-and-ceval.md`
- [R3](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/roadmap/M3-harbor-and-terminal-bench.md) — `docs/roadmap/M3-harbor-and-terminal-bench.md`
- [R4](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/roadmap/M4-pi-and-external-harnesses.md) — `docs/roadmap/M4-pi-and-external-harnesses.md`
- [R5](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/roadmap/M5-scenarios-skills-and-judges.md) — `docs/roadmap/M5-scenarios-skills-and-judges.md`
- [R6](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/roadmap/M6-experiments-comparison-and-gates.md) — `docs/roadmap/M6-experiments-comparison-and-gates.md`
- [R7](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/roadmap/M7-sdk-migration-and-release.md) — `docs/roadmap/M7-sdk-migration-and-release.md`
- [E1](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/verification/M1.md) — `docs/verification/M1.md`
- [E2](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/verification/M2-R4-fixes-2026-09-20.md) — `docs/verification/M2-R4-fixes-2026-09-20.md`
- [E3](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/verification/M3.md) — `docs/verification/M3.md`
- [E4](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/verification/M4-completion-review-2026-09-21.md) — `docs/verification/M4-completion-review-2026-09-21.md`
- [E5](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/verification/M1-M5-acceptance-report-2026-09-21.md) — `docs/verification/M1-M5-acceptance-report-2026-09-21.md`
- [E6](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/verification/M6.md) — `docs/verification/M6.md`
- [E7](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/verification/M7.md) — `docs/verification/M7.md`
- [E8](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/review/2026-09-22-m1-m7-code-feature-review.md) — `docs/review/2026-09-22-m1-m7-code-feature-review.md`
- [E9](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/review/2026-09-22-m1-m7-test-report.md) — `docs/review/2026-09-22-m1-m7-test-report.md`
- [E10](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/docs/release/support-matrix.md) — `docs/release/support-matrix.md`
- [C1](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/apps/web/src/evalTypes/directllm/DirectLlmCompare.tsx) — `apps/web/src/evalTypes/directllm/DirectLlmCompare.tsx`
- [C2](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/apps/web/src/evalTypes/gsm8k/Gsm8kCompare.tsx) — `apps/web/src/evalTypes/gsm8k/Gsm8kCompare.tsx`
- [C3](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/packages/sdk-python/motte_sdk/experiments.py) — `packages/sdk-python/motte_sdk/experiments.py`
- [C4](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/packages/contracts/motte_contracts/experiment.py) — `packages/contracts/motte_contracts/experiment.py`
- [C5](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/packages/sdk-python/motte_sdk/comparisons.py) — `packages/sdk-python/motte_sdk/comparisons.py`
- [C6](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/tests/storage/test_scoring_jobs.py) — `tests/storage/test_scoring_jobs.py`
- [C7](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/tests/storage/test_trials_downgrade_guard.py) — `tests/storage/test_trials_downgrade_guard.py`
- [C8](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/packages/scenario-runtime/motte_scenario/state.py) — `packages/scenario-runtime/motte_scenario/state.py`
- [C9](https://github.com/BaiZhi967/MoTTEavl/blob/fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d/apps/web/src/pages/judges/JudgesPage.tsx) — `apps/web/src/pages/judges/JudgesPage.tsx`
- [CI-run](https://github.com/BaiZhi967/MoTTEavl/actions/runs/35702579654) — pinned HEAD 的 CI。
- [CI-python](https://github.com/BaiZhi967/MoTTEavl/actions/runs/35702579654/job/106663837333) — 实际失败日志。
