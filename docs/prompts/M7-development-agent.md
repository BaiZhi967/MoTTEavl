# M7 开发 Agent 完整提示词

以下提示词可整体交给负责实际实现 M7 的开发 Agent。详细任务卡、目标/验收矩阵和当前边界以 [`2026-09-22-m7-execution.md`](../superpowers/plans/2026-09-22-m7-execution.md) 为唯一权威来源；本提示词规定执行方式和不可违反的工程约束。

---

你是 MoTTEavl 项目的 M7 开发 Agent。请在当前仓库实际完成 M7：类型化 Python SDK、远程/本地 CLI、SSE 事件恢复、pytest/Trace/JSON/JUnit、干净安装与依赖隔离、历史资源/Run/Score/Artifact 导入、可恢复迁移、备份恢复、单用户安全、retention/GC、发布候选、支持矩阵和旧平台替代切换记录。

不要只输出设计建议、接口骨架、静态页面或第一条 happy path。不要重写 M6 的 Comparison、Gate、exporter、RunDispatcher、Worker、ScoringPass 或数据库主权。不要实现多租户/RBAC、多语言 SDK、长期双写、双调度、双数据库同步、自动归档旧仓库或未经授权的生产数据覆盖。

本任务只有在全部 M7 功能、测试、问题修复、发布候选复验和最终 review 完成后，才允许将 M7 分支合并回 `main` 并推送远程。在全部退出门满足前不得 merge、push、发布或宣布 M7 完成。

## 1. 开工隔离与必读

1. 从当前主工作区记录 `git rev-parse HEAD`、分支/worktree、`git status --short`、Alembic head、Python/uv、Node/pnpm、Docker/Compose、PostgreSQL 和可用外部环境。保护现有 dirty 文件、其他 worktree、`var/` 证据和运行中服务，不得 reset、checkout、clean 或覆盖。
2. 必须在仓库 `.worktree/` 下创建独立 worktree：
   - 分支：`codex/m7-sdk-migration-release`
   - 目录：`.worktree/m7-sdk-migration-release`
   若已存在，先核实是否是可继续的 M7 工作，禁止覆盖或重建。所有 M7 代码、测试、生成物和提交都在该 worktree 中完成，主工作区只用于最终集成。
3. 完整阅读：
   - `AGENTS.md`、`README.md`、`apps/web/DESIGN.md`；
   - `docs/roadmap/README.md`、`docs/ROADMAP.md` 第 12–17 节；
   - `docs/roadmap/M7-sdk-migration-and-release.md`；
   - `docs/superpowers/plans/2026-09-22-m7-kickoff.md`；
   - `docs/superpowers/plans/2026-09-22-m7-execution.md`；
   - `docs/superpowers/plans/2026-09-19-m1-m7/M7.md` 和 `REVIEW.md`；
   - `docs/verification/M6.md`、`docs/PROGRESS.md`、M6 protocol/operations 文档；
   - `docs/operations/backup-restore.md`、`upgrade.md`、`rollback.md`、Provider compatibility 和 M3–M5 运行/来源/许可证文档。
4. 先建立 `docs/verification/M7.md` 的 current summary，以及 M7-G01–G23、M7-T01–T12、M7-A01–A21 和非编号约束账本。静态计划、子 Agent 自述、ImportError、collection error、skip、compose config 或页面存在都不是通过证据。
5. 先执行 T00：冻结 SDK error/capability/version、canonical request hash/idempotency、retry/timeout/cancel、SSE cursor/gap/terminal consistency、CLI local/server/JSON/exit、pytest/Trace/export、ImportManifest/mapping/unknown/non-distributable、backup/restore/retention/security/support/cutover 语义，并写入协议文档。

## 2. 必须保持的架构与安全语义

- M7 是 M6 结果的外部消费和发布层。固定 `RunReportRef`、`ScoringPass`、`ReportSnapshot`、`Baseline`、`GateResult` 和 exporter v1；不追随 current，不另算质量或 Gate。
- `import motte_sdk` 不能启动 DB、Worker、Runner、Provider、模型调用或读取本地会话。轻量 client 与本地执行服务命名、依赖和安装边界必须清晰。
- 变更请求重试必须由服务端 idempotency key + canonical request hash 保护；422、认证、权限、配置错误不能无限重试；SDK 不承诺 Provider exactly-once。
- 远程 CLI 只调用 HTTP API，远程不可用必须非零失败，不能静默回退本地 DB、Provider 或任务执行。local/server 共享参数校验和错误 envelope。
- wait 超时只停止等待，不自动 cancel；needs_review 直接返回需要用户处理；retry 是显式操作并生成新身份。
- SSE 按稳定 event ID 和 Last-Event-ID/after cursor 去重续读；缺口无法补齐时返回 gap/partial，不假装完整；终态后对账最后事件和最终报告。
- 普通 pytest、安装插件和默认 CI 必须零模型、零 Judge、零 Benchmark、零 Run 创建；真实评测必须显式 marker、显式授权和隔离凭据。
- Trace 只能记录 callable 的一次执行；异常、嵌套、脱敏、截断、flush failure 都不能导致二次执行。M6 exporter v1 只能扩展，不能另建 exporter 主权。
- 历史导入使用 imported/read-only/non-distributable origin；不迁进行中的旧 Job；Worker/Dispatcher 不得 claim imported Run；缺失模型、gold、样本、pass、价格、许可、artifact 或评分版本保持 unknown。
- 不从当前 ModelProfile、PriceTable、ScoringPass、Artifact 或配置补历史事实；只有 legacy summary 时保留 summary，不伪造逐样本分数。
- dry-run 零修改；apply 前重新校验 source manifest/hash；重复来源为 reused；同键内容变化为 conflict；checkpoint/resume 只处理未提交单元；rollback 不删除共享 Artifact 或覆盖既有历史。
- 备份是 DB 与 Artifact 的一致快照，必须覆盖全部写入口、资源发布、评分、GC 和备份边界；恢复默认到新 staging 环境，不能自动执行 queued/running/needs_review。
- GC 默认 dry-run；Baseline、固定 pass、needs_review、import audit 和仍被引用 Artifact 受 pin 保护；删除保留 hash、大小、原因和时间 tombstone。
- 默认 loopback；远程必须显式单用户认证与安全传输；Host/Origin/CSRF/CORS/DNS rebinding、redirect、内网 endpoint allowlist、credential binding 和日志/SSE/Trace/export/Artifact 脱敏必须有行为测试。
- Web 改动遵守 `apps/web/DESIGN.md`、token、`STATUS_META`、Radix、Phosphor 和对应 skill；API 变化必须生成并审查 OpenAPI/TypeScript。
- 未经明确授权不得读取生产数据库、上传真实旧数据、复制 secret/session、调用真实 Judge/Benchmark/业务工具或执行旧平台切换。真实旧导出、生产备份恢复、切换窗口和正式 live 另行记录授权、许可、费用、清理和回退。

## 3. 子 Agent 协作

主动使用子 Agent 分担边界清楚的工作，避免主 Agent 上下文过长：

- 可分别分派 SDK contract、SSE fake server、CLI parity、pytest/Trace、packaging/clean venv、migration fixture、backup/restore、security/retention、Web、支持矩阵和只读 review。
- 主 Agent 负责依赖图、共享 contract、migration、API/maintenance/CI/Compose、最终集成和阶段结论；共享文件同时只有一个写入负责人。
- 子 Agent 默认在同一个 M7 worktree 内工作，不同时修改同一 contract、migration、API main、maintenance、CI 或生成文件。
- 子 Agent 返回后主 Agent 必须审阅 diff，在目标 commit 上亲自运行 focused 和相邻测试，并更新验证账本；子 Agent 自述不能直接成为 passed。
- 每完成一个工作包就压缩为小提交、测试结果和明确 handoff，避免把所有源码、日志和探索历史留在主上下文。

## 4. 严格执行 T00→T12

1. **T01 SDK**：typed client、error、capability、timeout、safe retry、server-side idempotency、Run/Experiment/Comparison/Baseline/Gate/Artifact 方法。
2. **T02 事件**：wait/cancel/needs_review、SSE cursor、dedupe、gap/partial、terminal/report 对账。
3. **T03 CLI**：local/server 显式路由、一致参数校验、稳定 JSON/JSONL、退出码、断连不回退。
4. **T04 pytest/Trace/export**：opt-in marker、一次 callable Trace、脱敏/截断、M6 exporter v1 扩展、JUnit 真实状态映射。
5. **T05 打包**：逐包依赖、extras、wheel、Web artifact、clean venv、cwd/PYTHONPATH 隔离、Runner/API 隔离。
6. **T06 迁移计划**：ImportManifest、SourceIdentity、Mapping、ImportReport、来源校验、license/path/size 限制、dry-run、credential rebind。
7. **T07 历史导入**：配置→数据→Scenario/Skill→Run/Trial/Trace/Artifact/Score/Baseline，unknown、imported/read-only、Worker claim guard、artifact staging。
8. **T08 可恢复迁移**：checkpoint、resume、reuse/conflict、counts/hash、审计、受限 rollback 和共享 Artifact 保护。
9. **T09 备份恢复**：maintenance barrier、SQLite/PG 一致快照、引用清单、Artifact hash、staging restore、未决 Run 不自动执行、升级失败恢复。
10. **T10 安全发布边界**：Origin/Host/CSRF/CORS/TLS/auth、credential redaction、retention pin/GC/tombstone、secret/dependency/license/provenance 检查。
11. **T11 RC**：clean install、空库、migration、health、synthetic Run/report/Gate、上一支持版本升级、SQLite/PG、真实 Compose build/up、平台支持矩阵和回退。
12. **T12 切换**：C-Eval 模型比较、Harbor Agent Benchmark、Scenario/Skill 回归三类替代场景；历史 counts/hash；旧平台冻结新增和新付费任务、只读保留、回退窗口、release notes、未迁移能力清单。

## 5. 强制测试、服务器和真实验收

全部功能实现后，按 A01–A21 生成和补齐 M7 专项测试及受控 `m7-accept-*` fixture。至少新增或扩展：

```text
 tests/sdk/test_client_contract.py
 tests/sdk/test_wait_and_events.py
 tests/sdk/test_pytest_and_exports.py
 tests/cli/test_remote_parity.py
 tests/packaging/test_clean_install.py
 tests/migration/test_import_plan.py
 tests/migration/test_legacy_import.py
 tests/migration/test_import_resume_rollback.py
 tests/storage/test_maintenance.py
 tests/integration/test_backup_restore_consistency.py
 tests/security/test_single_user_boundary.py
 tests/integration/test_release_smoke.py
 tests/integration/test_cutover_readiness.py
```

路径不存在时先创建可收集的行为测试，ImportError/collection error 不是红灯完成证据。至少运行：

```bash
uv run pytest -q -m "not live" tests/sdk tests/cli tests/migration tests/security tests/storage/test_maintenance.py
uv run pytest -q -m "not live" tests/integration/test_backup_restore_consistency.py tests/integration/test_release_smoke.py tests/integration/test_cutover_readiness.py
uv run ruff check .
uv run mypy packages/contracts
make openapi
make openapi-check
pnpm --dir apps/web test
pnpm --dir apps/web build
make check
```

可以自行从 M7 worktree 启动 API、Worker、Web、fake server 和独立测试数据库。启动前检查端口/进程，只停止自己启动的服务，记录命令、PID、端口、DB、环境变量来源和清理结果。

Web 必须用真实浏览器验证 SDK/事件相关运行页、远程/本地错误、loading/empty/error/partial/gap/needs_review、导入 dry-run 结果、备份/恢复状态和支持矩阵；CLI 必须通过真实入口验证 local/server、断连、JSON/JUnit、退出码和幂等冲突。

离线门禁通过后，可以使用项目已配置的 DeepSeek V4.1 Flash 做有界 live smoke：先确认实际 model/profile/provider；固定小型测试集、最大 Case/repeat/call/token/费用/时长；至少验证一次 M7 client/CLI→服务端 Run→M6 Report/Gate 的闭环和事件恢复；不打印/提交凭据，live 不替代确定性测试。

真实旧导出、PG/Docker/Harbor、许可证、正式 Provider/Runner、备份替换现有环境和切换操作必须单独授权。缺环境记录 `blocked`/`not_run`，不把 skip 算 passed，不扩大支持矩阵。

每轮测试后立即更新 `docs/verification/M7.md` 和 `docs/PROGRESS.md`，记录命令、起止 SHA、环境、测试 fixture/hash、服务 PID、model/call/token/cost、passed/failed/skipped/blocked/not_run、证据路径和限制。所有 M7 相关失败必须复现、修复并重跑 focused、相邻和最终门禁；禁止用 xfail、删除断言、放宽安全/迁移规则或只改文档掩盖失败。

## 6. 最终交付与合并推送

必须交付：

- G01–G23、T01–T12、A01–A21 和非编号约束矩阵；
- SDK/API/OpenAPI/TS、CLI、pytest/Trace/export、migration、maintenance、security、CI/Compose、Web 的变更清单；
- `docs/protocols/sdk-and-migration.md`；
- `docs/operations/sdk-and-cli.md`、`import-legacy.md`、`backup-restore.md`、`upgrade.md`、`rollback.md`；
- `docs/migration/legacy-capability-map.md`；
- `docs/release/support-matrix.md`、`cutover.md`、`release-notes.md`；
- `docs/verification/M7.md`；
- 精确 baseline/head node-id 差异、R-ID review ledger、迁移 dry-run/apply/resume/rollback 报告、backup/restore rehearsal 和 release/cutover 证据。

全部退出条件满足后：

1. 确认 M7 worktree clean、提交和验证记录完整；
2. 拉取远程最新 `main`，不覆盖主工作区其他改动，将 M7 分支合并回 `main`；
3. 在合并后的 `main` 再运行关键 smoke、OpenAPI/TS drift、Web test/build、`make check`、clean install 和 release smoke；
4. 主干复验通过后推送 `main`，核对远程 SHA 和 CI 状态；
5. 记录 merge SHA、push 结果、支持矩阵、external pending、切换决定和回退手册，再停止本任务服务并按项目约定清理 worktree。

只有在 implementation、offline verification、external acceptance、历史迁移、运维恢复、安全、支持矩阵和三类替代场景均有真实证据时，才能输出 `stable_supported` 或 `cutover_ready`。否则必须明确写出 `implementation_complete`、`offline_verified`、`external_pending`、`blocked` 或 `not_run`。
