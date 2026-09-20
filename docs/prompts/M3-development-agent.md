# M3 开发 Agent 提示词

以下内容可完整复制给负责实际开发的 Agent。

---

你是 MoTTEavl 的开发 Agent。请在当前仓库实际完成 M3：Harbor 与 Terminal-Bench。执行到代码、测试、文档和全面 review 闭环，不要只给建议或脚手架；不提前实现 M4–M7。

先读取：
1. `AGENTS.md`、`README.md`、`docs/roadmap/README.md`。
2. `docs/roadmap/M3-harbor-and-terminal-bench.md` 全文。
3. `docs/superpowers/plans/2026-09-19-m1-m7/M3.md` 的完整需求/验收覆盖矩阵。
4. `docs/superpowers/plans/2026-09-20-m3-kickoff.md` 的当前代码差异、接口、顺序、逐包反例和验收要求。
5. `docs/verification/M2-R4-fixes-2026-09-20.md`，核对实际工作区已包含 M2 修复；不能把旧 `7774f42` 当作修复后的版本。
6. UI 工作前读取 `apps/web/DESIGN.md` 与其指定项目 skill。

先检查 git 状态、当前分支、已有实现和测试，保留用户未提交文件与 `var/`。如果有更新后的前置代码，调整落位并记录差异，不复制已经存在的模块。建立任务清单和 `docs/verification/M3.md`，逐条追踪 M3-G01–18、T01–10、A01–15，以及原需求所有未编号约束。后续每完成一个包就 review、修复并复验，不在最后才发现契约无法衔接。

执行顺序：
- T01：只读准备本地/固定来源任务，来源 revision、许可、文件 hash 和稳定 TaskIdentity；同名不同路径不合并，拒绝越界/链接逃逸，不执行任意仓库脚本。
- T02：冻结 TrialPlan，新增 Trial 存储和结果契约；扩展 CaseAttempt 的 Trial 冲突域，复用已有 ScoreSet.trial_id。InMemory/SQLite/PG 一致，升级旧库后旧无 Trial Run 仍可读写。计划重复、传输重试、操作员 retry、新评分 pass 严格分开。
- T03：确定真实可取得的固定 Harbor 版本、Terminal-Bench 2.x source revision、首个 Agent Profile 和环境 digest。核对固定版源码/CLI并尽早验证原生配置实际可加载和执行，不能凭名称/AST/字符串测试假定兼容。allowlist 参数，独立 Runner 环境，credentials 只留引用。
- T04：基于冻结 bytes 的纯 Parser，保留 task/trial/source_trial 身份、reward、Verifier错误、轨迹、终端、文件、patch 和证据完整度；多 Trial 全保留，不挑最佳/最后一个。
- T05：Docker、镜像架构/摘要、磁盘、资源、Agent依赖、许可和任务权限预检。预检不得执行任务/模型；任务容器不能访问宿主秘密、平台数据库或 Docker socket。若改变上游安全/Verifier可见性，使用新的自定义 Profile fingerprint。
- T06：复用 Dispatcher 和 DurableExternalJobRunner，一 Job 多 Task/Trial；先持久化启动身份再执行，先冻结证据再解析/导入；导入中断、删除工作目录后仍能恢复，Artifact引用/hash不漂移，不重复启动。
- T07：Agent/Verifier/Job 三层超时、取消、wrapper崩溃、PID复用和资源残留。只操作身份可核验的本 Job 资源；不确定副作用进入 needs_review，不自动重跑/补 Trial，不全局清理。清理失败不覆盖评分，迟到结果不复活终态。
- T08：Verifier→Trial/Task ScoreSet→ScoringPass→报告/M6-Lite。reward=0是有效失败；缺reward、协议错误、Verifier错误分别表示。1/0/timeout 三 Trial 的通过率为1/2、覆盖为2/3，完整覆盖 Gate不得通过。聚合规则事前固定；成本未知保留unknown，分母/单位明确，重评分不重跑任务。
- T09：同一SDK门面支撑 API/CLI/Web。提供任务筛选、版本、Agent/Profile/重复/预算/预检、Job→Task→Trial监控、结果、轨迹/终端/文件diff/Verifier/Artifact和比较入口。Trial切换不串证据，迟到响应不覆盖新选择；queued空态/缺工件/截断/未知身份可见。更新OpenAPI及生成TS类型，遵守设计治理。
- T10：完整校准、实际环境验证、操作/清理/身份/兼容文档与最终review。

每个工作包按以下流程进行：
1. 写能暴露真实缺口的行为测试，确认有效红灯；收集失败或ImportError不能算TDD证据。
2. 完成该包全部要求和错误路径，再运行focused及相邻回归。
3. 检查需求、接口、持久化/并发、取消恢复、秘密/路径边界、用户链路；登记并修复发现的问题。
4. 同步文档和验证记录。若使用子代理，固定共享契约负责人，划分不重叠文件，主代理统一review与集成；不要让子代理自报通过替代独立复验。

验证分层：
- 默认是fixture/replay/离线，不调用真实付费模型或个人订阅。
- 必须提供并尽可能运行真实固定 Harbor + 本地确定性 Agent + 真实 Docker 校准，从公共创建入口的生产配置经过实际 Runner产物、Parser、评分、报告；手写测试配置或fake runner不能代替这层。
- 真实 Agent/模型验收需用户另行明确授权。先准备可审阅的任务清单、精确版本、模型、Profile、重复数、预算、期限与命令；未获授权继续完成所有独立工作，将live明确记blocked，不能宣称整阶段真实验收完成。
- PG/Harbor/Docker依赖缺失时如实记blocked/not_run；skip不算通过。不得重启用户正在运行的Worker/Dispatcher或访问其现有数据库以凑证据。

最终运行 `make check`；UI必须 `pnpm --dir apps/web test` 与 `pnpm --dir apps/web build` 全绿。公共接口变更执行 `make openapi`并审查生成差异。额外登记真实PG两连接、真实Harbor/Docker校准的独立命令、版本、日志和结果。对运行配置dump、Artifact、API展示和日志使用合成秘密做泄漏测试。

交付 `docs/operations/terminal-bench.md`、`docs/operations/harbor-compatibility.md`、`docs/protocols/task-trial-identity.md`、`docs/operations/harbor-cleanup.md`、`docs/verification/M3.md`，同步实际Provider观测边界。兼容矩阵只能声明实际验证的dataset-version × agent-profile × environment，不把Harbor上游支持范围直接当平台支持。

完成后全面review，修复所有P0/P1/P2；每项缺陷记录复现、修改、复验与残余限制。汇报实现内容、18/10/15覆盖情况、测试证据、实际验证组合及阻塞项。若真实验收未完成，区分“代码检查通过”和“阶段验收通过”。不要把计划复选框全勾上作为交付证据，也不要自动合并、推送或开始M4。
