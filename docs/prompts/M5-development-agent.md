# M5 开发 Agent 完整提示词

以下内容可整体交给开发 Agent。

---

你是 MoTTEavl 的开发 Agent。请实际完成 M5 的全部开发、测试、逐包 review 和最终 review。仓库位置以当前工作环境为准（本次交接环境为 `E:/Dev/MoTTEavl`）。从已交付 M4 的最新主干建立 `codex/m5-scenarios-skills-judges` 隔离工作区，记录起始 SHA；不要覆盖用户文件、重置其他分支或进入 M6/M7 功能开发。本提示词不授权付费模型调用、生产切换或自动合并 M5。

开工前完整阅读：

1. 根 `AGENTS.md`、`docs/roadmap/README.md`、`docs/ROADMAP.md` 第 10 节；第 9 节作为 M4 运行时边界补充。
2. `docs/roadmap/M5-scenarios-skills-and-judges.md` 全文，包括非编号章节。
3. `docs/superpowers/plans/2026-09-21-m5-execution.md`：本次详细执行卡、接口事实、22 个目标及 18 个验收映射。
4. `docs/superpowers/plans/2026-09-21-m5-kickoff.md`：基线和开工顺序。
5. `docs/verification/M4-completion-review-2026-09-21.md`（本轮实际验收入口）与 `docs/verification/M4.md`（历史记录）；核实已合入接口、能力及尚未验证的环境。若 M4 尚未满足验收和主干合入条件，先完成只读准备并说明阻断，不把开发分支冒称已交付主干。
6. 涉及 Web 时先读 `apps/web/DESIGN.md`，按根 AGENTS.md 路由设计技能。旧 `2026-09-19-m1-m7/M5.md` 只作历史规划补充，不覆盖本次细化。

先检查当前代码与计划是否一致，输出实际模块映射、依赖、文件所有权和短执行顺序，然后立即执行，不停在计划层。计划中的拟新增 API 不是已有事实；同职责模块已经存在则扩展，不创建第二套。

必须完成以下全部工作包：

- **T01/T02**：发布 Workflow/Fixture 版本；严格解析七种 step、受限条件与全局预算；旧 DSL 做显式诊断与语义对照；目标可见夹具和私有 gold/checker 隔离，清理有所有权证据。
- **T03/T04/T05**：真实持续上下文 TargetSession、累计预算、限时与实际停止；过程/工具/最终状态断言；经公共 API 创建的 Scenario Run 进入既有 Dispatcher、CaseAttempt、Invocation、Trace、Artifact、Observation 和评分链路。外层 scenario backend 与内部 target runtime 身份分别固定。
- **T06/T07/T08**：instruction / instruction_with_resources / executable 三种 Skill 的草稿、校验、不可变发布与实际注入；static/render、executable fixture、fixed Agent behavior 三种验证范围准确标记；平台/Scenario/Target/Skill 权限取交集且 deny 优先；no-skill / skill-v1 / skill-v2 三组普通 Run 使用明确已发布版本引用、同一计划和预算，固定评分 pass 比较，保留开销与能力未知。
- **T09/T10**：独立 JudgeSpec、异步 ScoringJob、用途为 judge 的调用账本、预算和取消；Worker 执行并原子发布 ScoreSet/pass/current，崩溃不自动重发不确定调用；至少 30 个人工复核样本校准，位置偏差与重复评分如实计量，人工修订只追加 pass 并保留依据。
- **T11/T12**：API/CLI/Web 的完整创建、监控、证据、验证、三组比较、Judge 作业、校准、人审链路；七类业务边界的纵向验收；G01–G22、T01–T12、A01–A18 全量矩阵及 M6/M7 接口说明。

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

每包按计划运行针对性测试。最终至少实际运行 ruff、contracts mypy、Python 非 live 测试、真实 PG 事务/迁移、OpenAPI/客户端生成一致性、Web test/build 和 `make check`。仅记录本次实际输出；失败/跳过/环境 not_run 分开列出。没有环境时先继续独立工作，再清楚说明缺少什么，不能宣称全绿。

遇到需要真人校准、模型/凭据/费用或平台环境的信息缺口，尽早合并成明确问题，同时推进不依赖答案的工作。当前提示词没有指定付费模型或额度，不能默认使用主机个人登录态。不要因缺依赖删除验收项，也不要反复询问已经授权的普通开发动作。

最终交付包括源码与对应文档、逐包提交、`docs/verification/M5.md` 全量矩阵、运行命令及结果、实际费用、剩余阻断和回退路径。维护 `docs/protocols/scenario-workflow.md`、`docs/operations/scenarios.md`、`docs/operations/skills.md`、`docs/operations/judges.md`、`docs/migration/workflow-parity.md`。只有全部目标和验收都有可核验依据时才称 M5 完成；未完成就继续修复，不以“核心功能完成”替代全阶段交付。
