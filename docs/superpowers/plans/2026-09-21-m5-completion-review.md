# M5 收口修复与验收执行计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 `02ea8ed` 审查发现，完成 M5 公共执行、Skill、Judge 和验收闭环，使阶段状态与可复核证据一致。

**Architecture:** 继续使用既有 RunDispatcher、Worker 执行锁、CaseAttempt、Invocation、FrozenObservation、ScoreSet 和 ScoringPass。先修边界正确性，再启用入口；Judge 在提交期冻结非秘密 Provider/Profile/价格快照，Worker 只执行已冻结且授权的调用计划。Skill 的计划、实际执行与比较消费同一冻结身份。

**Tech Stack:** Python 3.12、uv、FastAPI/Pydantic、SQLite/PostgreSQL、Node 24/pnpm、React/Vite。

**Spec:** `docs/roadmap/M5-scenarios-skills-and-judges.md`、`docs/superpowers/plans/2026-09-21-m5-execution.md`、`docs/verification/M5-review-2026-09-21.md`。本计划是收口增量，不取代原 M5 需求；F01–F20 指审查报告的问题编号。

## Global Constraints

- 一个 Case 一个 session；跨 Case、retry 子 Run、Skill 对照臂必须独立。
- 步骤和全局限制使用同一个 monotonic deadline；未知停止为 needs_review，保留现场、不自动重放。
- 权限为平台 ∩ Scenario ∩ Target ∩ Skill；deny 优先，Skill 不得把 mock/replay 改成 real。
- 发布版本、冻结证据、历史评分不可覆盖；固定 pass／Baseline 不追随 current。
- GET、compare、gate、历史切换与普通离线 rescore 零模型调用。
- Judge 独立授权、独立计量；未知费用不得声称可证明货币硬上限。
- 至少 30 条真实人工复核校准样本，覆盖明确通过、明确失败、边界、证据缺失和提示注入；合成协议 fixture 不能替代人工验收。
- 不改变 M4 live_pending 的真实边界，不扩大本轮为 M6 统计/Gate 重写。
- 涉及 `apps/web` 时先读 DESIGN.md，遵守 tokens、STATUS_META、Radix、Phosphor 约定。
- 一包一轮行为验证和独立提交；提交前执行 AGENTS.md 规定的 `make check`。如环境无法执行，记录缺项并在受支持 CI 补齐，不写“已通过”。
- 本计划不授权调用付费模型、操作真实业务、合并或推送；执行者按届时用户授权行动。

## Review Focus

1. 超时响应带工具调用：超过期限后不能继续受控写入或追加模型请求；未知停止保留现场（R1）。
2. 客户端填 0 估算／0 授权、两组 pair、三次 repeats：实际计划和硬限制不能绕过授权（R2）。
3. 证据 owner 错配、两个修订竞争同一 current：零错误发布，CAS 只能有一个胜者（R3）。
4. 同一 Run 选不同 Judge pass、预算增加、旧 plan 换模型：比较必须识别真实条件差异（R4）。
5. 发布缺字节、嵌套缓存被修改、相同业务 ID 跨臂：发布不可伪造，执行与状态不可串扰（R5/R7）。

## 调度与文件所有权

初始可并行三条线：R1 Scenario、R2→R3 Judge、R5 Skill。R4 在 R3 固定 pass 身份后完成；R6 依赖 R1；R7 依赖 R4/R5/R6；R8 依赖 R2/R3；R9 统一收口。每条线可有只读 reviewer，避免多个实现者同时改公共契约、存储、API 或 Worker。

```text
R1 Scenario 正确性 ──────────→ R6 公共 Run ────────┐
R5 Skill 发布正确性 ────────────────────────────→ R7 Skill 执行与三臂 ─┐
R2 Judge 计划／预算 → R3 证据／修订／校准 → R4 比较身份 ──────────────┤
                                      └──────→ R8 Judge 接线 ──────┤
                                                                  R9 用户入口与验收
```

先记录实际 HEAD、工作区状态与迁移 head；不要假定根工作区仍停在审查提交。若有后续提交，先判断哪些发现已修复，保留对应回归，避免重复实现。不要将本地 `.superpowers/` 的复现脚本当作已经提交的测试。

## R1：修复 Scenario 实际执行边界

**对应：** F05/F06/F07/F08/F14/F15/F16；G02/G04/G05/G06/G07/G09；A03/A05/A06/A07/A08/A09。

**修改文件：**

- `packages/sdk-python/motte_sdk/scenario_target.py`：deadline 与 Builtin observe 适配。
- `packages/agent-runtime/motte_agent/builtin_react.py`：与既有预算／取消钩子对齐，保留 one-shot 行为。
- `packages/scenario-runtime/motte_scenario/engine.py`：所有动作的有效期限、全局 failure policy。
- `packages/scenario-runtime/motte_scenario/executor.py`：模式分派、逐实例清理所有权、未知停止保留。
- `packages/scenario-runtime/motte_scenario/state.py`、`fixtures.py`：嵌套可见投影、私有真值和生命周期证据。
- 测试扩展 `tests/scenario/test_workflow_engine.py`、`test_target_stop_confirmation.py`、`test_fixture_lifecycle.py`、`test_scenario_run_vertical.py`；Builtin 适配反例可新增 `tests/scenario/test_builtin_target_boundaries.py`。

**接口：** 保留 TargetSession 的 begin/send/observe/interrupt/close；`send(..., deadline=...)` 表示实际执行期限，`interrupt.confirmed` 只能依据已观测停止。fixture cleanup 消费执行结果中的停止确定性；工具桥消费有效 mode，不再硬编码 real。

- [ ] 把 scenario-repro.py 的七项反例转成正式行为测试；用可控时钟／同步屏障或受控阻塞进程降低短毫秒计时的抖动，禁止把等待时长当唯一成功证据。
- [ ] 先复现失败，再逐个修复 deadline 传播与执行、observe 结构、模式分派、投影、清理和 policy 继承；每个改变运行相邻测试。
- [ ] 测试必须直接观察副作用、调用数、残留文件及进程状态：

```text
过期响应包含写工具 → 写 handler 未被调用，后续模型调用数不增加
最后一个工具超时 → Workflow 不得 completed
mock/replay → real handler 调用数为 0，分别消费明确的模拟／回放来源
state/tool-result 的 dict/list 内含 gold → Target 可见输出不含隐藏值
interrupt.confirmed=false → needs_review + retained，资源仍在、owner 可核验
第二个 fixture prepare 失败 → 第一个已成功实例有清理记录，无静默残留
全局 continue_for_evidence → 失败后只读 checkpoint 执行，写工具不执行
runtime terminated → adapter observe 能读取状态，停止确认与事实一致
```

- [ ] 验证 begin／send／close 任一阶段失败都保留原错误与清理结论，Case 更换后 history/call IDs/工具状态隔离。
- [ ] 更新 scenarios 运维文档，执行下述检查后独立提交。

```powershell
uv run pytest -q -m "not live" tests/scenario tests/runtime/test_agent_session.py tests/runtime/test_builtin_react.py tests/runtime/test_builtin_agent_backend.py tests/evaluators/test_workflow_assertions.py
```

## R2：统一 Judge 调用计划和预算执行

**对应：** F01/F02/F17；G15/G16/G17；A13/A15/A16。

**修改文件：** `packages/sdk-python/motte_sdk/scoring_jobs.py`、`packages/evaluators/motte_eval/judge.py`，按需要增量扩展 `packages/provider-runtime/motte_provider/` 的现有限额／错误接口；测试 `tests/evaluators/test_judge_execution.py`、`tests/storage/test_scoring_jobs.py`。

**接口决定：** 提交生成且冻结唯一 plans 列表，每项有 call_id、owner/case、mode、repeat_index、presentation_order、input_sha256。预检的 max_calls 等于实际列表长度，不能分别用两个公式计算。Provider 错误类别复用现有定义；价格快照与预算预留记录进入作业，秘密值不入库。

- [ ] 固化零授权、pairwise 笛卡尔扩展、repeats 丢失、server 分类四组失败测试。
- [ ] 先编译完整请求计划，再计算预算与幂等 fingerprint；默认顺序按每个 pair 自身生成，repeat/order 各有独立 call ID。明确多次评分的结果保存及聚合政策，禁止后一次静默覆盖前一次。
- [ ] 输入估算由真实渲染请求决定；只有能证明上下界的 tokenizer／Provider 能力才用于硬承诺。把可选输出参数收紧为剩余预算允许值，校验本地预算、用户授权和 Provider 能力三者交集。
- [ ] 每次 dispatch 前原子保留额度；已 dispatch 但未结算的调用占用额度且不重发。累计实际 usage/cost，未知计费保持未知。
- [ ] 以下断言全部通过后再允许 Worker 使用：

```text
authorized_tokens=0 或 authorized_usd=0 且请求需要正额度 → 拒绝，provider.calls=0
spec cap=0、估算费用>0 → budget_executable=false，不声明可执行硬上限
2 pairs × 1 order × 1 repeat → exactly 2 calls，不能 4 calls
1 sample × 1 order × 3 repeats → exactly 3 独立调用／证据
变更任一 order/repeat/input/预算身份 → fingerprint 与冻结计划正确对应
dispatch 后 ProviderHTTPError(500) → 不确定结果／计费，不能 billable=false
GET/preflight/cancel/history → provider.calls 不增加
```

- [ ] 校验 SQLite 重启后计划与剩余额度一致，运行 Judge 定向组并独立提交。

```powershell
uv run pytest -q -m "not live" tests/evaluators/test_judge_execution.py tests/storage/test_scoring_jobs.py
```

## R3：修复证据归属、人工修订 CAS 与校准资格

**对应：** F03/F04/F10；G15/G16/G18/G19；A15/A17。

**修改文件：** `packages/sdk-python/motte_sdk/scoring_jobs.py`、`packages/evaluators/motte_eval/calibration.py`、`rubrics.py`、既有 ScoringPass 仓库及 `packages/storage/motte_storage/scoring_jobs.py`；测试 `tests/evaluators/test_judge_calibration.py`、`test_judge_execution.py`、`tests/storage/test_scoring_jobs.py`。实际仓库文件先用 rg 定位，不新建第二套评分存储。

**接口决定：** subject owner 必须绑定保存的 Run＋source pass＋case/attempt＋Observation/artifact hashes；calibration owner 使用独立命名空间，不能冒充 subject。人工修订的 expected_current_pass_id 必须由仓库事务核验。qualification 消费覆盖、质量、重复、换序及 spec 身份，而不只消费 measured 标志。

- [ ] 固化 FOREIGN Observation、错误 case key、外部 source pass、错误 artifact owner 的拒绝测试；失败前 provider.calls=0，current 与原 subject 证据不变。
- [ ] 用同步屏障构造两次人工修订争用同一 expected_current；把 current/revision 比较放入追加事务。验证失败请求没有部分 ScoreSet、孤立 pass 或伪成功事件。
- [ ] 在 Memory、SQLite、真实 PostgreSQL 验证同一语义；数据库迁移只在实际需要新增结构时追加，不修改既有迁移历史。
- [ ] 将 CalibrationSet.covers 的逐类缺项写入报告和资格原因；在 CalibrationPolicy 中明确重复稳定性等阈值及未测行为，新增 policy 字段纳入版本/hash。
- [ ] 按如下验收向量验证，不把合成标签误称为人工验收：

```text
request run-1/case-1 + observation FOREIGN/foreign-case → reject, zero calls/passes
两次 expected_current=pass-source → exactly one successful current update
30 clear_pass，其他类别 0 → experimental=true, gate_eligible=false
重复稳定率低于所选 policy → 不合格，并记录具体依据
新 model/rubric/spec hash → 不继承旧 qualification
人工修订 → 新 pass 有 actor/reason/evidence/source，旧 pass 内容与 baseline 不变
```

- [ ] 统一 pass.judge 的 rubric 引用与资格读取字段，覆盖“真实发布 pass → qualification → 资格读取”路径，不能只构造测试专用字典。
- [ ] 更新 judges 运维文档与校准说明，运行 Judge＋存储定向组后提交。

## R4：让比较使用真实 pass 和完整冻结条件

**对应：** F09/F11；G11/G13/G14/G19；A12/A17。

**修改文件：** `packages/sdk-python/motte_sdk/comparisons.py`、`skill_ablation.py`、`packages/evaluators/motte_eval/comparison.py`；测试 `tests/evaluators/test_m5_comparison_factors.py`、`tests/sdk/test_skill_ablation.py`，新增 `tests/sdk/test_m5_selected_pass_comparison.py`。

**接口：** `ComparisonService._manifest_view(run_id, scoring_pass_id)` 投影指定 pass 的真实评分身份。计划生成／展开／比较共用一份规范化冻结条件，包含数据集与样本、Workflow/Fixture、Agent/runtime/model/provider、工具有效权限、实际预算、scorer/Judge/rubric/calibration 与干预身份。

- [ ] 把两个真实保存的 Judge pass 放入 ComparisonService 比较，allowed_factors=() 时发现其评分口径变化；不能只测直接传 manifest 的底层比较函数。
- [ ] 把 100→100000 tokens、max_steps 2→200、旧 plan 换 model 作为失败测试。
- [ ] 将预算值纳入 plan_hash，展开时校验输入基底与冻结条件一致；no-skill/v1/v2 的允许变化明确限定为已发布 Skill 及其政策允许的预算分配。
- [ ] 对 same-total-budget 与 same-execution-budget 分别记录总额度、指令开销和执行额度；未知数据导致对应维度不可比，不能补 0 或默认一致。
- [ ] 验证历史字段缺失、人工修订、新 rubric、显式允许的合法变化均有可解释原因；比较全过程零模型调用，固定 pass 不随 current 改变。
- [ ] 运行比较和 ablation 定向测试后提交。

```powershell
uv run pytest -q -m "not live" tests/evaluators/test_m5_comparison_factors.py tests/sdk/test_skill_ablation.py tests/sdk/test_m5_selected_pass_comparison.py
```

## R5：收紧 Skill 发布与缓存边界，补持久字节存储

**对应：** F12/F13/F19/F20；G10/G11；A10/A11。

**修改文件：** `packages/skill-runtime/motte_skill/versions.py`、`registry.py`、`importer.py`，复用／扩展现有内容存储接口；`packages/storage/motte_storage/resource_store.py` 及其调用的资源仓库实现；测试 `tests/skill/test_skill_versions.py`、`tests/storage/test_scenario_skill_resources.py`。

**接口：** `publish_skill(repository, record, *, resource_store=..., published_at=...)` 是版本发布服务边界；非空资源清单必须核验内容存储。若保留底层资源发布接口，入口同样不能绕过字节与依赖保证。内容存储按 hash 寻址、写入后核验并可跨 API/Worker 进程重启读取。

- [ ] 固化以下输入并确认原实现失败：

```text
资源清单非空、resource_store=None → 发布拒绝
资源缺失／size或hash不一致／依赖版本不存在 → 发布拒绝
python argv=['-cprint(42)']、对应node组合eval参数 → 禁止绕过已哈希入口
调用方修改 resolve 返回的嵌套 schema → 后续读取仍为原内容
published→deprecated 同时改 published_at → 冲突
```

- [ ] 对允许的解释器建立明确 argv 语法规则，入口文件必须在发布资源中；纯指令不强制入口。保留导入不执行、不下载、不运行钩子的约束。
- [ ] 实现持久内容存储及 API/Worker 共享读取配置；测试重启后读取、内容损坏、并发相同 hash 写入及部分写入中断，不能仅提供 Memory store。
- [ ] 发布固定依赖版本与字节；registry 返回隔离对象；弃用仅允许生命周期相关字段变化。
- [ ] 更新 skills 运维文档，运行 Skill＋资源存储测试，独立提交。

## R6：完成 Scenario 公共 Run 和标准评分闭环

**依赖：** R1；**对应：** T05、G01/G08/G20，A01–A08。

**修改文件：** `packages/sdk-python/motte_sdk/scenario_backend.py`、`scenario_target.py`、`resolve.py`、`service.py`、`packages/scenario-runtime/motte_scenario/executor.py`、既有 observation／scoring 装配位置；新增 `tests/integration/test_scenario_run_backend.py`、`tests/integration/test_business_regression_slice.py`。

**接口：** 继续 `prepare_run → RunService → RunDispatcher → ExecutionHandle.invoke(case_id)`；同一个 CaseAttempt 保存 steps、Invocation、Artifact、FrozenObservation，workflow evaluator 经既有评分入口产生标准 ScoreSet/pass。步骤不创建子 Run。

- [ ] 先写公共 API 创建→持久 queued Run→普通 Worker 领取→报告读取的集成测试，不直接调用 executor、不在测试中把 backend 强行标为 available。
- [ ] 固定 Workflow／Fixture／Target／Skill 快照；接标准 subject 调用账本、取消、状态错误映射、工具／checkpoint 证据和 hash。
- [ ] 让 workflow evaluator 消费冻结且归属有效的证据；普通离线 rescore 重用证据，不能再次操作业务工具或模型。
- [ ] 覆盖成功、缺确认但终态正确、暂态错误恢复、超时、越权、重复副作用、进程崩溃。缺监测的 no-side-effect 只能 insufficient。
- [ ] 验证需求允许的无 fixture 纯消息流程；当前创建验证与 executor 对 fixture 的要求应一致，不能在创建后才发现无 primary binding。
- [ ] 所有公共路径通过后启用真实后端能力，并把 availability 测试改为验证实际可用与不满足能力时的拒绝，不能仅改 False 为 True。
- [ ] 运行公共集成、既有 Direct/Agent/外部后端相邻回归并提交。

## R7：接通 Skill 实际应用、fixture 和三组对照

**依赖：** R4/R5/R6；**对应：** T07/T08，G10–G14，A09–A12。

**修改文件：** `packages/skill-runtime/motte_skill/injection.py`、SDK Run 冻结／Agent 装配／`skill_ablation.py`，既有 sandbox 工具权限入口；新增受控 executable fixture 消费者及 `tests/skill/test_skill_injection_fixture.py`、`tests/integration/test_skill_ablation.py`。

**接口：** compile_injection/select_for_execution 的实际输出进入 Agent 请求或可核验的 native loader 清单；effective permissions 进入唯一执行网关。ablation 计划展开三条既有普通 Run，保存每臂 run_id 和固定 report/pass 引用，不新增调度器。

- [ ] 先写 Agent 请求捕获测试：渲染内容、顺序、hash 与冻结声明相同；目标拿不到私有 checker；声明 Skill 不能提升 mock/replay/deny、路径和网络权限。
- [ ] 对 instruction、instruction_with_resources、executable 分别证明静态／渲染、资源落位、受控入口输入输出与副作用；明确各层验证范围。
- [ ] executable fixture 使用现有 sandbox 的 cwd/env/argv/期限/清理机制，输出不合 schema 或越权返回失败；关闭时不给“已执行通过”结果。
- [ ] 用公开冻结资源创建并执行 no-skill/v1/v2 三臂；相同业务 ID、文件名和会话输入下，证明每臂初始状态一致且互不影响。
- [ ] 生成逐 Case 配对结果、失败分类、指令 token overhead、工具次数及总成本变化；缺 usage/cost 保持未知，指令 token 不重复计费。
- [ ] 对一臂失败／取消／无结果给出覆盖解释，禁止只挑成功 Case 声称收益。验证 R4 不同预算／Agent 条件会阻断纯 Skill 归因。
- [ ] 提交实现、行为测试与 skills 运维文档。

## R8：冻结 Judge Provider 快照并接入同一 Worker

**依赖：** R2/R3；**对应：** T09/T10 公共接线，G15–G19，A13–A18。

**修改文件：** `packages/sdk-python/motte_sdk/scoring_jobs.py`、`resolve.py` 中已有资源解析逻辑、`apps/worker/motte_worker/runtime.py`、`apps/api/app/main.py`／`schemas.py`；相邻资源仓库；新增 `tests/api/test_judge_flow.py`、`tests/integration/test_judge_worker_flow.py`。

**选择 A：** 提交期解析已发布 ModelProfile、ProviderConnection 和价格版本，固定引用、hash、adapter、endpoint、模型、非秘密 credential reference。Worker 从快照构造 Provider，执行期只解析秘密引用，不按可变名称重新选模型。现有 factory 仅接收模型字符串，需要升级参数契约，不能靠把资源仓库随意注入来绕过冻结。

- [ ] 先写资源在提交后改名／升级，但旧作业仍使用原快照的集成测试；secret 明文不进入 job/manifest/event/log。
- [ ] 完成预检、持久 submit、read/history、幂等 cancel 的服务/API 契约；服务端自行解析保存的证据和计数，不信任客户端 sample_count 和 observation 内容。
- [ ] 保留 factory 不可用时拒绝提交的行为。将恢复、领取和执行放入既有 Worker 执行锁，不让 GET 或 API submit 直接调用模型。
- [ ] 为普通 Run 与 Judge 作业定义不会长期饿死任一类的领取顺序，并测试连续 Run 入队时 Judge 最终得到调度；不能只在系统永远空闲时才有机会执行。
- [ ] 覆盖 prepared、dispatching、响应已持久化、发布事务中断、提交后通知失败各崩溃窗口：不重复收费、终态结果原子可见、pending/failed 不推进 current。
- [ ] 验证 purpose/owner/job/pass 字段在实际 Invocation 契约、存储、API 和报告中一致；校准结果使用 R3 的统一 pass 身份，不被序列化丢弃。
- [ ] 使用 ScriptedProvider 完成公开 API→Worker→新 pass→历史切换／取消测试，真实付费验证留到有明确授权时。
- [ ] 更新 judges 运维说明，运行 Worker/评分作业/报告相邻回归后提交。

## R9：公共入口、生成物与最终验收

**依赖：** R1–R8；**对应：** F18、T11/T12、G18/G20/G21/G22、全部 A01–A18。

**修改文件：** API/CLI 实际入口、`apps/web/src/api/client.ts`、`schema.d.ts` 与 Scenario/Skill/Judge 页面及测试、`api/openapi.json`、`docs/verification/M5.md`、对应 operations/protocols 文档。现有 UI 增量修改先按 AGENTS.md 读取设计规范、使用适用设计技能，不重写无关页面。

- [ ] 发布 Skill 版本、三层测试、步骤下钻、三臂对照、Judge 预检／提交／查询／取消／校准报告都有真实 API 消费者。补齐 Skill/Judge CLI，保留旧命令兼容与离线 rescore 默认行为。
- [ ] Web 费用确认使用服务端真实计划与预检，显示模型、用途、次数、费用已知性及预算能力；历史 pass 切换只读。用真实 API 集成契约测试补充 mocked fetch 测试。
- [ ] 更新 API JSON 与生成的 TS，运行生成物无漂移验证；不要只让 Vite build 成功就认为类型同步。

```powershell
uv run python scripts/check_openapi.py
pnpm --dir apps/web gen:api
git diff --exit-code -- apps/web/src/api/schema.d.ts
```

这里 gen:api 第一次预期会产生需要提交的变更；生成物提交后再次运行，预期无 diff。公共路由变化时先按 Makefile 的 openapi 导出命令更新 JSON。

- [ ] 分别在干净基线和待验收提交、同 OS/依赖/环境配置下执行非 live 全量，保存日志及 JUnit。提取失败 node ID 集合：新增=`head−base`，消失=`base−head`，共同=`head∩base`。不能用失败总数相减代替差集；每个新增失败必须修复或有可复核环境原因。
- [ ] 在 Linux＋真实 PostgreSQL 运行完整 make check，覆盖 0010→0013 升级、实际新增 head、SQLite/PG 一致性、取消/CAS/并发和恢复。迁移检查必须确认不是仅扫描迁移文件的静态测试。

```bash
uv run pytest -q -m "not live" --junitxml=artifacts/m5-head-nonlive.xml
uv run ruff check .
uv run mypy packages/contracts
pnpm --dir apps/web test
pnpm --dir apps/web build
make check
```

上述完整门禁按验证批次执行一次即可，后续只因代码变化、失败或未解决风险重复。CI 产物目录按 CI 配置创建并上传日志，不能写进业务 var。

- [ ] 填满 A01–A18 矩阵：每行记录公共入口、命令、commit、环境、结果和证据链接。至少一个业务主流程覆盖成功／缺确认／工具错误与恢复／超时／越权／副作用失败，不用多个互不相干的单元测试替代主流程。
- [ ] 整理真实至少 30 条人工复核校准资料：来源、类别、标注者／复核者、时间、期望准则、争议说明、固定 rubric/model/spec；在授权环境执行并按所选 CalibrationPolicy 出报告。没有资料或付费授权时记录未验收，Judge 保持 experimental，其他软件修复继续完成。
- [ ] 更新文档中的陈旧状态，把“实现存在、公开可用、实际通过”三层证据分开；保留 M4 live_pending，不删除旧失败记录掩盖问题。
- [ ] 由未参与对应实现的 reviewer 按本次 F01–F20 和全部目标复审，交付验证记录、剩余边界、提交列表和回退步骤。用户授权合并／推送后再操作主干。

## 目标覆盖与最终退出条件

| 目标 | 负责包 |
|---|---|
| G01/G08 | R6 |
| G02/G03 | R1，保留契约／无 eval／恶意条件回归 |
| G04/G05/G06/G07/G09 | R1＋R6；Skill 权限由 R7 验证 |
| G10/G11/G12 | R5＋R7 |
| G13/G14 | R4＋R7 |
| G15/G16/G17 | R2＋R3＋R8 |
| G18/G19 | R3＋R4＋R8＋R9 |
| G20/G21/G22 | R6＋R7＋R8＋R9 |

- [ ] F01–F20 每条有修复提交、真实消费者回归及复审结论；不能只改文案或关闭测试。
- [ ] Scenario、Skill 三臂、Judge 三条公共链路都可按文档重跑，并保留历史和失败证据。
- [ ] 指定提交的完整门禁、生成物无漂移、真实 PG 和精确失败差集有可复核产物。
- [ ] 人工校准与 live 的事实边界明确；未完成时不得宣布全部 M5 验收完成。
- [ ] 能关闭新增能力／停止新作业但保留已有资源、证据和评分；回退不删除历史。

推荐后续执行方式：按上述三条文件互不冲突的主线派实现 Agent，公共存储/API/Worker 由集成负责人串行收口，每包附独立只读 review。当前任务仅做审查与规划；后续执行时使用本计划和审查报告作为共同输入。
