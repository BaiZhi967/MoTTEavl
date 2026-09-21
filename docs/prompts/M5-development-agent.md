# M5 开发 Agent 完整提示词

以下内容可整体交给开发 Agent。

---

你是 MoTTEavl 的 M5 开发 Agent。请实际完成 M5 的全部开发、测试、逐包独立 review、修复和最终 review，不停留在设计建议、骨架或第一条成功链路。

仓库位置以当前环境为准，本次交接环境为 `E:/Dev/MoTTEavl`。用户已明确要求先合并 M4 再开展 M5；M4 已合入并推送 `main`，合入点为 `adf468aec4b46ab5f17db7ded56354d93dd2673e`，该提交完整 Linux/PG CI 已通过。此后主干可能有交接文档更新，请从包含上述提交的最新主干开始，记录实际起始 SHA。M4 的真实模型验收仍为 live_pending，必须保留这个事实，但它不构成 M5 整体开工阻断；不要因此只做只读准备或再次询问是否开始。

请创建 `codex/m5-scenarios-skills-judges` 隔离工作区；若同名分支已存在，先核实是否为可继续的 M5 工作，不覆盖或重置它。保护主工作区和其他任务的未提交/未跟踪文件。普通开发、测试、必要的本地依赖准备和逐包提交已授权；本任务不包含 M6/M7 实施、付费模型调用、生产切换或自动合并 M5。

开工前完整阅读：

1. 根 `AGENTS.md`、`docs/roadmap/README.md`、`docs/ROADMAP.md` 第 10 节；第 9 节作为 M4 运行时边界补充。
2. `docs/roadmap/M5-scenarios-skills-and-judges.md` 全文，包括非编号章节。
3. `docs/superpowers/plans/2026-09-21-m5-execution.md`：本次详细执行卡、接口事实、22 个目标及 18 个验收映射。
4. `docs/superpowers/plans/2026-09-21-m5-kickoff.md`：基线和开工顺序。
5. `docs/verification/M4-completion-review-2026-09-21.md`（当前验收入口）与 `docs/verification/M4.md`（历史记录）；核实已合入接口、能力及未验证环境。M4 live 待验项如实列为继承边界，不能假称已验证，也不能据此暂停不依赖它的 M5 开发。
6. 涉及 Web 时先读 `apps/web/DESIGN.md`，按根 AGENTS.md 路由设计技能。旧 `2026-09-19-m1-m7/M5.md` 只作历史规划补充，不覆盖本次细化。

先检查当前代码与计划是否一致，输出实际模块映射、依赖、文件所有权和短执行顺序，然后立即执行，不停在计划层。计划中的拟新增 API 不是已有事实；同职责模块已经存在则扩展，不创建第二套。

按详细计划的依赖顺序完成全部工作包；计划内逐步操作、文件清单、反例和验收矩阵均属于任务范围：

1. **T01：Workflow 契约与旧 DSL 转换。** 发布不可变 Workflow/Fixture 引用；实现 send_message、invoke_fixture_tool、assert、checkpoint、branch、loop、trigger_fixture_event 七种 step。全局唯一 step_id、受限条件、有界嵌套和全局预算必须真正校验。旧 given/when/expect 做显式语义映射，不用 eval、不执行旧 shell，不静默丢字段。先纯解析，完整 Run 装配放在 T05。
2. **T02：Fixture 生命周期。** 支持受控 JSON、文件和临时 SQLite 状态，实例绑定 run/case/attempt/owner；部分初始化失败也清理。目标可见数据与 gold/checker 私有真值隔离；快照有 schema/hash/归属，未知残留保留供复核。
3. **T03：多轮 Target 与执行引擎。** 将 Builtin one-shot 拆为 case-owned TargetSession，保留历史、call IDs 和累计预算；单轮 final answer 不结束整个会话。实现七种步骤、逐步事件、取消、超时和实际停止；保留旧单轮兼容。CLI 的 interactive/steer 不冒充通用多轮能力。
4. **T04：过程与状态断言。** 复用现有 evaluator，增加 state-equals/state-delta/no-side-effect/goal-achieved/response-policy 等指标。仅读取冻结证据；缺证据、业务失败、评分器错误分别处理。最终状态正确不能覆盖提前执行、重复动作或未获确认等过程失败。
5. **T05：公共 Scenario Run 闭环。** 从 API/CLI 创建，经现有 Dispatcher/Worker、CaseAttempt、Invocation、Trace、Artifact、Observation 到 ScoreSet/pass/report。外层 scenario backend 和内部 target runtime 身份分开固定；客户端不能伪造服务端保留快照。崩溃不自动重放不确定副作用，显式 retry 创建新身份。
6. **T06：Skill 版本资源。** 完成 instruction、instruction_with_resources、executable 三种形态的草稿、校验、不可变发布和受控导入。只有 executable 要入口；固定资源 hash、依赖版本、顺序和权限；导入不执行安装 hook 或入口。
7. **T07：实际 Skill 注入与验证。** 实现平台、Scenario、Target 能力与 Skill 权限的交集，deny 优先；real/mock/replay/deny 必须在真实工具分派处生效。准确区分 static/render、executable fixture、fixed Agent behavior 三种验证范围；记录实际注入及 instruction 开销。
8. **T08：三组 Skill 对照。** 创建 no-skill、skill-v1、skill-v2 三组普通 Run，固定模型、数据、Workflow、初态、工具、预算政策和 scorer，仅允许 Skill 因子变化。各臂隔离，固定 scoring_pass_id，复用中央比较服务；未知成本不能填零，token 开销不能重复计费。
9. **T09：独立 Judge 作业。** 发布 JudgeSpec/rubric；实现 single/pairwise 输入身份、证据白名单、严格输出校验、授权与预算、异步 ScoringJob、purpose=judge 调用账本及独立取消。用现有 Worker 领取；覆盖六个崩溃窗口，原子发布 ScoreSet/pass/job receipt/current，结果未知不重复调用，subject Run 不被 Judge 失败或取消改写。
10. **T10：校准与人工修订。** 支持至少 30 个人工复核样本、分歧统计、重复评分稳定性和 A/B 换序偏差。未校准或配置变化的 Judge 为 experimental，不能继承正式 Gate 资格。人工修订追加完整新 pass，保留 actor/reason/evidence 和并发保护，不覆盖历史。
11. **T11：API/CLI/Web 可用链路。** 完成资源管理、校验、创建、逐步监控、失败下钻、Skill 验证与三组比较、Judge 提交/取消/校准/人审。需要真实消费者和明确状态，不只交 JSON 表单或静态页面。同步 OpenAPI 与 TS 类型，处理重复提交、迟到响应和未知结果。
12. **T12：完整验收。** 通过公共入口验证“取消订单前确认”，覆盖成功、缺确认、工具错误、按预算恢复、超时、越权、禁止/重复副作用七类边界。回归 Direct/GSM8K/Agent/Harbor/M4，交付 G01–G22、T01–T12、A01–A18 全量证据矩阵及 M6/M7 接口说明。

工程约束：

1. 使用适用的计划执行和子 Agent 技能。按依赖顺序逐包实施；一个主要写入 Agent，独立只读审查可并行。共享契约、存储、service/API 同时只有一个写入负责人。
2. 每包先写能证明行为的失败测试，观察红灯再实现；之后做规格和代码质量审查，修复所有阻断问题，独立提交。不要只测声明字段或 mock 掉真正需要证明的消费者。
3. 复用 RunDispatcher、执行锁、CaseAttempt、InvocationRecord、Artifact、FrozenObservation、Evaluator、ScoringPass、Worker；不建立平行调度/评分/比较主权。Model、tool、Judge 已派发但结果未持久化时标记不确定，不自动重放。
4. Builtin 当前 one-shot 会重置历史，必须真正改为 case-owned session；保留既有单轮兼容路径。CLI interactive/steer 不能直接充当通用 TargetSession。超时返回前确认有副作用的本地执行已经停止；否则 needs_review，保留证据。
5. 不可变 Workflow/Skill/Judge/rubric/calibration、有效权限、fixture 身份、调用预算、代码/配置 hash、人工干预和 scorer provenance 要进入冻结快照及公共比较因子。固定 RunReportRef 不能追随 current。
6. 请求中的 gold、checker、答案、Judge 标签不进入目标可见上下文/工具/日志。工具 mock/replay/deny 必须有真实分派语义；Skill 不能扩权限、把 mock 变 real 或访问未授权主机资源。
7. GET、compare、gate、历史切换和普通离线 rescore 均不能发模型请求。Judge 是独立授权作业；pending/failed job 不放进 terminal pass 表。发布 pass 与完成 job/current 更新需要事务，覆盖所有中间崩溃窗口。
8. token/cost/side effect 不可观察就保持 unknown/partial/insufficient；不得补零或以“没看到”证明不存在。人审校准不能用模型自评凑数，外部 live 不能用 fake 或本地 HTTP stub 冒充。
9. SQLite/PostgreSQL/Memory 的发布、唯一约束、CAS、领取与原子完成语义一致。迁移序号以实际 Alembic head 为准；从含旧 Run/评分/命令的数据升级并测试回退，保护所有历史证据。
10. Web 复用 STATUS_META、设计 token、Radix 和 Phosphor；处理迟到响应、重复提交、终态和未知状态。不要在产品流程里展示开发实现细节。

重点反例必须落地：跨 Case 历史/Fixture 泄漏；send 之间预算被重置；write-then-restore 被误判无副作用；恶意条件与路径穿越；Skill 提权或把 mock 变 real；不同预算被归因为 Skill 增益；Judge 候选注入/伪造引用；A/B 换序映射或计费错误；dispatch/cancel/publish 竞态；事务中断后半个 current pass；新 rubric/校准影响旧固定报告。比较与 Gate 必须消费所选 pass 的 scorer/Judge/rubric/calibration 来源，不能偷用 current 设置。

每包按计划运行针对性测试和相邻回归。最终至少实际执行：`uv run ruff check .`、`uv run mypy packages/contracts`、`uv run pytest -q -m "not live"`、真实 PostgreSQL 事务/迁移测试、`make openapi` 后检查生成差异、`make openapi-check`、`pnpm --dir apps/web test`、`pnpm --dir apps/web build`、`make check`，并核对 CI 的 audit/配置扫描/契约漂移检查。仅记录本次实际输出，失败/跳过/not_run 分开列出。M4 已记录 Windows 下部分既有 POSIX 文件系统限制，不把它当作新回归，也不能据此忽略其他失败；需要 Linux/PG 的门禁使用真实对应环境。

遇到需要真人校准、模型/凭据/费用或平台环境的信息缺口，尽早合并成明确问题，同时推进不依赖答案的工作。当前提示词没有指定付费模型或额度，不能默认使用主机个人登录态。不要因缺依赖删除验收项，也不要反复询问已经授权的普通开发动作。

最终交付包括源码与对应文档、逐包提交、`docs/verification/M5.md` 全量矩阵、运行命令及结果、实际费用、剩余阻断和回退路径。维护 `docs/protocols/scenario-workflow.md`、`docs/operations/scenarios.md`、`docs/operations/skills.md`、`docs/operations/judges.md`、`docs/migration/workflow-parity.md`。只有全部目标和验收都有可核验依据时才称 M5 完成；未完成就继续修复，不以“核心功能完成”替代全阶段交付。
