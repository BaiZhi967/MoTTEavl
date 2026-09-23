# MoTTEavl M8 交付收尾：开发 Agent 完整提示词

编写日期：2026-09-23（UTC+08:00）。本文件是可交给开发 Agent 的任务指令，不是实现或验收完成记录。

---

你正在维护 `BaiZhi967/MoTTEavl`。请依据下面的评估与规划，实际完成 **M8：M1–M7 交付收尾与 RC 验收准备**。任务包括现存问题复核、代码修复、产品接线、回归验证和证据更新，不是再写一遍泛化审查报告。

本轮不启动 M9/M10 新功能，不重写现有评测内核。先交付 M8-T00–T04，再按依赖推进 T05/T06 及 T07–T12 中具备条件的部分；遇到外部条件阻断，只暂停受阻任务，继续不依赖它的工作。所有未闭合目标保留原 M1–M7 归属，不通过改名 M8 把它们标成已完成。

## 1. 必读文件与基线核对

先读当前 checkout 的 `AGENTS.md`、相关目录约定、`README.md` 和 `Makefile`，再读：

1. `docs/review/2026-09-22-m1-m7-assessment/README.md`
2. `docs/review/2026-09-22-m1-m7-assessment/MoTTEavl_M1-M7_Assessment_2026-09-22.md`
3. `docs/review/2026-09-22-m1-m7-assessment/MoTTEavl_Next_Stage_Plan_2026-09-22.md`
4. `docs/roadmap/README.md` 及 M1–M7 原阶段目标；按工作包读取相关协议、执行计划和 `docs/verification/`。
5. `docs/review/2026-09-22-m1-m7-code-feature-review.md` 第 9 节及 `docs/review/2026-09-22-m1-m7-test-report.md` 第 12 节。必须读取修订附录，不能只读旧 finding 后重复修复。
6. `docs/release/support-matrix.md`、`docs/release/cutover.md`；修改前端前完整读取最新 `apps/web/DESIGN.md` 及相关产品规范。

两份原文的审查基线是 `fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d`；文档提交时主干已经是 `807f84c7d7e42f880f24d2e4ffe5916509d59d12`。这不是同一个快照。先执行 `git fetch origin`，记录实际 `origin/main`、工作区状态、分支和环境，检查原基线到当前代码的增量。

报告中“当前 CI 三项失败”等描述只绑定原 SHA；读取最新候选 SHA 的 CI 和实际日志，不照搬历史测试数。对 F-01–F-04 重新确认消费者与反例；对 F-05/F-06 先核查，不把需专项复核事项擅自写成确定缺陷。已修复项补有效证据并保留原成果，不重复改写。

## 2. 分支与修改范围

在隔离 worktree 或干净的专用工作区中，从核对后的最新主干创建实现分支，例如 `fix/m8-delivery-closure`；名称冲突时使用唯一后缀。不要覆盖或清理用户已有的 dirty 文件、其他 Agent 的 worktree、数据库或构建产物。

文档分支是 `docs/m1-m7-assessment-m8-handoff-20260923`。文档 PR 未合并时，从这个远端 ref 读取上述文件；需要放入实现分支时，只引入相应文档，不把整个旧实现基线覆盖到最新主干。不要自动合并文档 PR，也不要把功能代码追加到只用于文档交付的分支。

按工作包独立提交；可使用一个实现 PR 中的清晰提交序列，也可使用依赖明确的 stacked PR。依赖未合并时写明真实 base。无论采用哪种方式，都不直接推送 main、不 force-push、不自动 merge。最终推送实现分支并创建说明清楚的 PR；门禁未满足时使用 draft 并列明阻断。

## 3. 第一批必须处理的工作：T00–T04

### T00：增量复核与证据索引

把原 137 项目标映射到当前实现、消费者、测试与外部收据。区分实现状态与验证层级，保留 supersedes 关系。

先建立能持续更新的轻量索引和本轮验证记录，再开始修复；不要花整轮时间造证据管理框架。每个 finding 记录“仍存在 / 已修复有证据 / 需复核 / 外部阻断”以及对应原目标。

### T01：当前 CI 失败与专用测试隔离

重新定位最新 CI 的失败。原报告中的重点是：

- ControlledRoot 新建与 reopen 的实际后端/安全语义不一致；属性、实现与测试必须一致，不能仅按操作系统填标签。
- PG ScoringJob 测试的 invocation 顶层 run_id 与 owner.run_id 不一致；修复 fixture，保留归属不匹配拒绝的负例。
- PG Trial 降级测试受共享 ScoringJob 状态污染；使用本次创建的独立临时数据库，或已证明迁移 search_path 正确的独立 schema，并可靠清理。

分别运行单项、组合、改变顺序和全套。不要放宽 downgrade/owner/path 保护，不要删除安全负例或把整类失败改成 xfail。symlink 权限不足与实际路径漏洞分开；cleanup 的 unknown、residual、cleaned 不互相替代。

### T02：实验因子必须真正生效

核查 model_profile、reasoning_level、prompt_version、runtime_version、skill_version 从声明到请求/执行的全部消费者。

先按 suite 明确支持因子：尚未接通的非默认因子在 preview/create 一致拒绝，零新增可执行对象。随后逐项接通已有发布资源，固定 assignment → resource/version/hash → manifest → runtime/Provider request 的映射。不存在的 Prompt 资源不要凭空造版本；复用确实存在的 Scenario/Workflow 定义。

变量与控制条件冲突时明确拒绝。测试必须捕获实际 fake Provider 请求、Skill 注入或 runtime 选择的差异；只有 Cell ID 不同不能证明变量生效。

### T03：预算、停止政策与幂等一致

让 preview/create/allocate 使用同一个无副作用的解析与预算政策，并在持久化可执行对象前校验当前固定资源。

必须包含反例：数据集 10 Case、2 Cell、空选择代表全集、max_total_calls=5。预览与创建均识别潜在 20 次调用并拒绝，Run/Cell/Job/Provider 新增计数全部为零。

确认 token/cost/stop_policy 每个接受字段的实际消费者。不能落实的严格约束或非默认停止政策应拒绝，不静默接受；未知费用不补 0，不承诺账户级精确硬上限。停止只作用于本实验，保留在途与不确定结果，不自动“重试直到成功”。

复用现有持久幂等机制，绑定 operation/key/canonical request hash/resource。跨服务重建、跨进程、重启后，同 key 同请求得到同一结果，异请求明确冲突；不得仅依赖内存字典。保留现有稳定 Cell/Run ID，不重建调度器。

### T04：所有比较入口消费统一事实

对 DirectLlmCompare、Gsm8kCompare 和同类入口做增量修改。保留当前布局与下钻，移除前端私有质量/可比性算法，统一消费 ComparisonService、固定 RunReportRef 与指定 ScoringPass。

覆盖：同题数不同题集、同名数据不同内容、不同 scorer、显式旧 pass、缺失评分、USD/CNY/多币种、部分 usage 未报告、报告请求失败、异步结果晚到。

未知计量不能按 0 展示；成本按实际 currency 分开；current pass 改变不影响已固定比较。测试必须包含真实 create_app/HTTP 契约输入，而不只是 mock 一个理想 client。

## 4. 后续工作：按已有依赖推进，不重复建设

| 任务 | 具体处理要求 | 完成证据 |
|---|---|---|
| T05 跨 suite 实验 | 在现有 ExperimentService 增量接入 GSM8K/C-Eval、Builtin Agent、Scenario/Skill、Harbor 等原核心能力；每项独立组装，不移除 allowlist 后统一退化 Direct | 相同配置的 standalone 与 Experiment Run 语义一致；因子/选择/预算生效；重复和恢复不重复分配 |
| T06 统计消费者 | 先搜索生产调用链与现有统计输出，保留已实现部分；补 API/CLI/Web/导出缺口 | 固定输入、方法版本、单位、seed、missing policy 可追溯；Trial不足/把retry当Trial明确不适用 |
| T07 真实链路 | 准备原生 Agent 双模型、C-Eval、Harbor 真 Agent、Pi/Claude/Codex、业务/Skill 的有界验收卡 | 按明确授权执行；任务、版本、停止、工件、费用和 pass 的真实收据；无授权标 blocked |
| T08 Judge | 完成人工校准入口与协议验证；人工样本不足时保留 experimental | ≥30 条真实人工资料是项目最低门，不是可靠性保证；合成标签不可冒充人审；Judge费用单列 |
| T09 恢复 | 在本次独立临时 PG/Artifact 目标验证 Compose build/up、升级、pg_dump/restore、一致屏障和恢复后引用核对 | 不以 CRUD 或 compose config 替代；损坏备份、在途任务、pin/GC/needs_review 反例有效 |
| T10 旧导出 | 合成迁移测试先行；真实导出仅在取得来源授权后导入一次性目标 | dry-run/apply/中断/resume/重复/rollback 对账；不删共享、不反推未知、不入执行队列 |
| T11 产品/矩阵 | 真实入口、生成契约、支持环境、最新证据和未实现 retention 范围对齐 | 不因某台机器缺环境否定其他收据，也不拿旧收据证明新版本全验；范围缩减须用户确认 |
| T12 RC/切换准备 | 验证三个替代场景，生成构建哈希、支持清单、限制与回退材料 | 只有必需门均满足才能提出 RC/cutover-ready；不自动打 tag、发 Release、归档或切换生产 |

详尽验收条件采用配套规划的 M8-G01–G12、T00–T12、A01–A20。T05/T06 不以“首个切片完成”冒充全部范围；暂未接通的组合保留具体缺口。T07–T12 阻断不允许被 mock 通过覆盖。

## 5. 架构、数据与前端不可退化的约束

复用 RunDispatcher、执行锁、CaseAttempt/InvocationRecord、ExternalJob、Trial、ScoringPass/ScoreSet、不可变资源和现有 SDK；不新建独立 Run 生命周期或前端评分事实源。平台执行完成、任务通过、证据充分、结果可比、Gate放行是不同判断。

任何请求或副作用处于不确定状态时保留 indeterminate/needs_review，不自动再次付费或重放不可逆动作。已发布配置、原始工件、评分历史和 Baseline 绑定不可覆盖。Fixture/mock/oracle 证据不升级为真实模型支持，gold与隐藏评分规则不进入subject。

前端以开工时最新 AGENTS.md、DESIGN.md 为准，不照搬旧路线的技术栈禁令。文档提交时主干已经采用 Tailwind CSS 4 + Semi Design；index.css 已删除，样式由 tailwind.css/ui.css/theme.css 与板面组件分层管理。不要恢复旧 token、旧 .operate-* 或临时兼容桥，不另做视觉重设计。

截图证明 UI 修复时必须满足仓库现行规则：截图晚于 build、服务吐出的资产与 dist/index.html 一致、画面中能读到 __BUILD_ID__。无法取得真实浏览器或该证据时明确 not_reverified，不拿组件测试冒充截图验证。

## 6. 允许动作与必须暂停的动作

允许在隔离工作区修改代码/测试/文档，使用合成数据、假 Provider/Runner，以及本次明确创建、无生产数据的临时数据库、目录或容器做非付费验证；只清理可证明由本任务拥有的资源。

以下动作不包含在本提示词的默认授权内：真实 Provider/Harness/Judge 调用或消耗个人订阅额度、读取未授权私有旧导出、生产数据库迁移/恢复/GC、覆盖真实 Artifact、正式发布、合并主干、生产切换、归档/删除仓库。即使环境已有密钥、登录态或 DSN，也不能据此推定授权。

需要上述动作时，给出精确执行卡：账户/凭据引用、模型/runtime/数据版本、最大调用/token/费用、来源与目标、停止条件、清理和回退边界。没有授权就记录阻断，并继续安全且独立的工作。不要为权限问题要求用户提供真实密钥正文。

不允许为了任务完成降低原验收标准、删除失败历史、把 unknown 变 pass，或把缺环境等同于通过。

## 7. 实施与验证要求

遵循项目已有开发流程；对每个待修问题先定位根因和失败反例，再做最小完整修复、专项回归、相邻回归和独立 review。有子 Agent 时按文件/职责隔离，主 Agent 复核合并结果；没有子 Agent 时做同样的逐包检查，不虚称独立评审。

现有接口能满足时不新造；计划中的建议路径与当前实现不同，应更新映射而不是机械创建同义模块。必要的重大设计差异单列说明，不擅自扩大范围。

在隔离环境执行当前 Makefile/CI 的真实门禁，常规入口包括：

```bash
uv run pytest -q -m "not live"
uv run ruff check .
uv run mypy packages/contracts
uv run python -m compileall -q packages apps
make openapi-check
pnpm --dir apps/web test
pnpm --dir apps/web build
make check
git diff --check
```

接口变化时按仓库流程重新生成 OpenAPI/TypeScript 产物，不手改生成文件冒充同步。打包、安全扫描、平台专用 job 和真实恢复测试按受影响范围执行；核对所提交 SHA 的远程结果，不能把基线 CI success 当作新提交通过。

保存完整命令、退出码、SHA、环境、通过/失败/跳过数、精确 test node ID、日志或收据摘要。失败要分类为新增回归、证实的继承问题、外部阻断；分类不能豁免必需发布门。当前机器没有所需工具时，不伪造结果，不修改权限或使用陌生 DSN 规避约束。

## 8. 必须交付的成果与停止条件

在仓库现有结构内复用或新增以下本轮文件；若已有同职责记录则增量更新，不复制多个当前真值：

- `docs/superpowers/plans/2026-09-23-m8-delivery-closure-execution.md`：基于实际 HEAD 的工作包、依赖、文件、反例和交付顺序。
- `docs/verification/M8.md`：每包实际验证、失败、阻断和授权记录。
- `docs/verification/m8-evidence-index.json`：原137项目标与 M8工作包的可校验证据映射；缺证据明确记录，不生成假测试名或假哈希。
- 必要的协议/操作/支持矩阵更新，以及代码、测试和生成契约的对应提交。

原两份评估/规划附件作为历史快照保留，新的复核或修正使用上述记录和 supersedes，不重写它们来隐藏原结论。

最终报告必须列出：起始/最终 SHA 与 PR；F-01–F-06 的最新状态；M8每包完成范围与剩余项；实际测试结果及未执行原因；是否仍有阻断；下一项最小外部授权需求（仅在确有需要时）。明确分开 implementation、offline/integration/live、stable_supported、cutover_ready。

先从基线复核和 T00/T01 开始，然后完成 T02/T03/T04。完成可独立验证的批次就提交，不把所有工作堆成一个巨大修改。继续推进依赖就绪且授权允许的 M8内容；遇到阻断记录后转向独立任务，不停留在泛化计划或要求重做 M1–M7。M8收尾前不启动M9/M10；缺少真实验收时输出实际已完成范围和剩余执行卡，不宣称全部完成。
