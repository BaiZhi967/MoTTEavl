# M7 权威执行计划：SDK、迁移、备份恢复、安全与稳定发布

> 状态：planned。本文是 M7 实施的当前权威细化，建立在主干 M6 merge `aee31a9` 之上；它不是实现报告、真实迁移授权、生产备份授权或发布批准。原始目标以 [`M7-sdk-migration-and-release.md`](../../roadmap/M7-sdk-migration-and-release.md) 为准。每个工作包结束后必须更新 `docs/verification/M7.md`，不以静态复选框或计划覆盖证明完成。

## 0. 完成定义、证据层级与总原则

M7 交付的是 M6 固定结果的外部消费、安装发布、来源迁移和运维保护。M7 所有客户端、导出、迁移、备份、恢复和切换服务必须保留 Run/Case/Trial/Attempt/ScoringPass/ReportSnapshot/Baseline/GateResult 的既有身份边界。

证据分三层：

- `implementation`: 代码、契约、CLI/API/Web 消费者和文档是否落地；
- `offline_verification`: 合成数据、fake HTTP、SQLite、可用 PG、clean venv、Compose 和安全测试证据；
- `external_acceptance`: 真实旧导出、Docker/PG、正式 Provider/Runner、许可、人工操作和切换窗口证据。

工作包状态只能为 `complete`、`partial`、`blocked`、`not_run`。`skip`、ImportError、collection error、页面静态存在、compose config 成功、子 Agent 自述都不能算通过。M7 的阶段状态允许是 `implementation_complete / offline_verified / external_pending`，但只有在支持矩阵和三类替代场景满足后才可标记 `stable_supported`，只有真实切换审批和回退窗口满足后才可标记 `cutover_ready`。

不可违反的原则：

- 不复制 M6 Comparison、Gate、exporter v1、RunDispatcher、Worker 或数据库访问层；
- 不用 SDK/CLI 判断结果是否“看起来重复”来替代服务端幂等；
- 不在历史导入中从当前 ModelProfile、PriceTable、ScoringPass 或 Artifact 补写旧事实；
- 不将 imported Run 放入 Worker claim 队列；不迁进行中的旧 Job；
- 不在备份/恢复/GC 中忽略 Gate、Baseline、needs_review、Artifact pin 或并发写入；
- 不把 local/server 断连静默切换到另一执行地点；
- 不把真实模型/旧平台/生产数据调用塞入普通测试；
- 不因一个后端通过就扩大整个支持矩阵；
- 不在未完成全部退出门前 merge、push、发布或声明旧平台 ready。

## 1. T00 开工审计与契约冻结

### 输入核对

开发 Agent 首先记录：

- 当前 `git rev-parse HEAD`、分支、worktree、`git status --short`；
- Alembic head、Python/uv、Node/pnpm、Docker/Compose、PG DSN 可用性；
- M6 verification 的目标/场景状态和遗留限制；
- SDK、CLI、API schemas/routes/events、Web event hook、maintenance/artifact、Dockerfile/Compose、CI 和现有测试；
- 当前 package build/backend/extras、OpenAPI/TS 生成状态；
- 旧导出/旧仓库/许可证/凭据引用是否存在，不能猜造来源；
- 其他 worktree、dirty 文件、`var/` 证据目录和现有服务 PID，不能清理或带入 M7。

### 冻结产出

新增或更新 `docs/protocols/sdk-and-migration.md`，冻结：SDK error envelope、capability handshake、request canonical hash、idempotency key、retry matrix、timeout/cancel；SSE event cursor/gap/terminal reconciliation；CLI mode/config/stdout/stderr/exit; pytest marker/Trace/export mapping；ImportManifest/SourceIdentity/MappingRecord/ImportReport；dry-run/apply/checkpoint/reuse/conflict/rollback；backup barrier/manifest/hash/staging; retention pin/tombstone/GC; security Origin/Host/CSRF/CORS/TLS/auth/redaction; package dependency/extras/provenance; release/cutover state machine。

### T00 门

至少一组 contract tests 能收集并对上述决策建立失败断言；当前事实、M6 继承限制、环境缺口和文件 ownership 写入 `docs/verification/M7.md`；主 Agent 把共享 contract、migration、API、maintenance、CI/Compose 的写入权分配清楚后才能进入 T01。

## 2. 工作包顺序与所有权

```text
T00 审计/协议冻结
 ├─ T01 SDK 类型/错误/幂等 ─┐
 │    └─ T02 wait/SSE/事件 ──┤
 ├─ T03 CLI local/server ─────┤→ T04 pytest/Trace/export
 └─ T05 packaging/clean install┘
        ↓
 T06 ImportManifest/dry-run → T07 historical apply → T08 resume/conflict/rollback
                                      ↓
 T09 consistent backup/restore → T10 security/retention/supply chain
                                      ↓
 T11 release candidate/support matrix → T12 cutover/readiness/final review
```

共享文件同一时间只有一个写入负责人。可并行的只读工作包括 package audit、fixture 审查、Web/CLI 测试、migration threat review 和支持矩阵核对，但必须在主 Agent 串行集成后复验。

## 3. 工作包实施卡

每包执行固定闭环：可收集的行为反例红灯 → 最小完整实现 → focused 测试 → 相邻回归 → 安全/兼容/故障 review → 修复复验 → 文档与验证账本 → 小提交。任何测试失败都要复现、修复并重跑，不能用 xfail、删除断言、放宽约束或只改文档隐藏问题。

### M7-T01：公共 API → Python SDK 类型、错误、能力协商与幂等

**依赖：** T00、M6-T03/T07/T10。
**目标：** G01-G03。
**验收：** A02、A03。

**范围：**

- 在 `packages/sdk-python/motte_sdk/` 建立轻量同步 HTTP client、公共 typed response、error class、capability handshake 和配置对象；`import motte_sdk` 不启动 DB/Worker/Provider。
- 首批覆盖 provider/model 必要读取、Run create/get/list/cancel/retry、指定 ScoringPass report/artifact metadata、rescore、Experiment preview/create/get/cancel、Comparison、Baseline、Gate 和 M6 exporter。
- 类型来自公共 Pydantic/OpenAPI 形状，保留 server error code、request ID、retryable、安全 detail；未知扩展字段按版本策略处理。
- 只对明确安全的查询重试；变更请求只有在服务端确认相同 idempotency key 与 canonical request hash 时允许重送。422/auth/config/permission 不得无限重试。
- 服务端 create 入口权威绑定 key+hash；同 key 同 body 返回同资源，不同 body 返回冲突；SDK 不能自行猜测重复。

**反例与测试：** 422 不重试；响应丢失后重送同 Run；同 key 不同 body conflict；auth/timeout/server 错误分类；base URL、connect/read/overall timeout；SDK import 零副作用；能力缺失返回 typed unsupported。

**建议触点：** `packages/sdk-python/motte_sdk/client.py`、`client_errors.py`、`client_types.py`、`__init__.py`、`packages/storage/motte_storage/requests.py`、API schemas/routes、`tests/sdk/test_client_contract.py`。

### M7-T02：Run/事件 → wait、取消和 SSE 断线恢复

**依赖：** T01。
**目标：** G02、G05。
**验收：** A05。

**范围：**

- `wait_for_run` 使用总期限与本地取消；超时只停止等待，不暗中 cancel；needs_review 作为需用户处理的终态返回，不自动 retry。
- SSE 使用稳定 event ID、Last-Event-ID/after cursor、心跳、响应大小上限和连接释放；重复事件去重，断线后按 cursor 补齐。
- 已清理事件段返回明确 gap/partial；若可通过持久查询补齐则补齐，否则不假装完整。收到终态后读取最后事件和最终报告并核对一致。
- SDK 与 Web `useRunEvents` 使用同一游标、gap、terminal 语义，stdout 保持机器数据，诊断到 stderr。

**反例与测试：** 重复事件、断线重连、终态和最后事件同时到达、不可补齐 gap、长流异常、用户中断、needs_review、等待超时不 cancel、凭据/prompt 不进入日志。

**建议触点：** client/events API、`apps/web/src/hooks/useRunEvents.ts`、`tests/sdk/test_wait_and_events.py`、`tests/api/test_events.py`、Web event tests。

### M7-T03：本地/远程配置 → CLI 一致输出

**依赖：** T01/T02、M6-T10。
**目标：** G02、G04。
**验收：** A04。

**范围：**

- 显式 `local` / `server` 模式和配置优先级；server 只通过 SDK HTTP，不访问服务端 DB；远端不可用立即失败，不回退本地执行。
- create/wait/cancel/retry/events/pass/compare/baseline/gate/export 使用相同参数校验和 M6 退出语义；stdout JSON/JSONL 稳定单行，诊断只到 stderr。
- 保留既有 CLI 参数的兼容/弃用说明；live subject/Judge/Harness 必须是显式动作；默认 replay/local deterministic 不能触发付费调用。

**反例与测试：** server 断连时本地 DB/provider/task-start 调用计数为 0；local/server 同输入错误一致；JSON 可解析；远端 404/422/auth/timeout；命令取消不误报完成。

**建议触点：** `packages/cli/motte_cli/main.py`、commands/client config、`tests/cli/test_remote_parity.py`、existing CLI tests。

### M7-T04：report/Gate → pytest、Trace 与 exporter v1 扩展

**依赖：** T01/T03、M6-T10。
**目标：** G06-G07。
**验收：** A06、A07。

**范围：**

- pytest plugin 使用显式 marker/fixture；安装插件和普通 `pytest` 均不扫描数据集、不读凭据、不提交 Run、不调用模型/Judge/Benchmark。
- Trace decorator/context manager 只执行用户 callable 一次；记录嵌套 span、异常、脱敏、截断、采样、部分 flush failure；上报失败不能重跑 callable。
- 扩展 M6 exporter v1，不另建版本主权；quality_fail→failure，执行/评分基础设施错误→error，合法 not_applicable→skipped，insufficient/not_comparable 不导出为假通过；JSON 保留全部 rule/reason/hash。
- 明确 `pytest` 报告读取与真实执行 fixture 分界，live 需要显式开关和隔离凭据。

**反例与测试：** 普通 pytest 零模型调用；callable 异常/flush 失败只执行一次；unknown/空集为 error 或明确 insufficient；JUnit properties 保留 decision/exit/conclusion hash；导出零模型/Judge/Runner 调用。

**建议触点：** `motte_sdk/tracing.py`、`pytest_plugin.py`、`export.py`、`pyproject.toml`、`tests/sdk/test_pytest_and_exports.py`。

### M7-T05：workspace → wheel、Web 和依赖隔离

**依赖：** T01、T04。
**目标：** G08-G09。
**验收：** A01。

**范围：**

- 审计各 workspace package 的 pyproject/build backend、真实依赖、extras、资源打包和 import side effects；SDK 不隐式拉 Worker/Runner/DB 依赖。
- 构建声明交付 wheel/Web 静态资源，在 checkout 外干净 venv 安装，验证 import、client、CLI、资源读取和 fake API 调用，无 `PYTHONPATH`/cwd 依赖。
- 固定 Runner/runtime 镜像与 API/client extras 边界，记录 lock、wheelhouse/provenance；Compose build/up 不能只停在 config。
- 更新 CI clean install、package metadata、Web build/TypeScript drift 和 artifact retention 检查。

**反例与测试：** checkout 外 import；缺依赖；Runner 依赖污染 API；wheel 漏资源；不同 cwd；clean venv 误读源码；Web build 与 API 类型漂移。

**建议触点：** `packages/*/pyproject.toml`、根 pyproject、`Dockerfile`、`infra/docker-compose.yml`、`.github/workflows/ci.yml`、`tests/packaging/test_clean_install.py`。

### M7-T06：旧导出包 → ImportManifest、来源验证与 dry-run

**依赖：** T00、M5 资源/转换契约。
**目标：** G10-G11。
**验收：** A08、A11。

**范围：**

- 定义 `ImportManifest`、`SourceIdentity`、`MappingRecord`、`ImportReport`、source schema/revision/content hash、scope、artifact manifest hash 和 importer/transform version。
- 只读取受控目录/明确导出包，检查路径穿越、symlink、压缩炸弹、大小/文件/record 配额、许可证和来源 revision；不接受任意宿主路径服务端直读。
- dry-run 只读 source/target metadata，输出 created/reused/rejected/conflicted、unknown fields、missing artifacts、credential rebind 清单、counts/hash；目标 DB/Artifact/credential/state 零修改。
- apply 前重新校验 source hash；源内容变化使旧计划失效，不能沿用旧批准。凭据只迁引用，不复制 secret、session、整目录。

**反例与测试：** dry-run 零修改；源文件变更阻断 apply；恶意路径、symlink、压缩炸弹和秘密字段受控拒绝/脱敏；相同源重跑为 reused；相同 ID 内容变更为 conflict。

**建议触点：** `packages/contracts/motte_contracts/imports.py`、`packages/sdk-python/motte_sdk/migration/plan.py`、`sources.py`、`tests/migration/test_import_plan.py`。

### M7-T07：迁移计划 → 版本化资源与只读历史

**依赖：** T06、M3/M5 资源契约、M6-T07。
**目标：** G11-G13。
**验收：** A10。

**范围：**

- 按配置→Dataset/Case→Scenario/Skill→历史 Run/Trial/Trace/Artifact/Score/Baseline/Gate 的顺序转换；旧 ID、raw schema、source hash、mapping version 全部可追溯。
- imported origin 不可分发，Worker/Dispatcher 明确不能 claim；需要重跑只能由用户基于可解析资源新建 Run。
- 缺 model、gold、sample set、ScoringPass、price、license、artifact 或 scorer 时保持 unknown/insufficient，不从当前配置填充；只有 legacy summary 时保存 summary，不伪造逐 case 分数。
- 不迁进行中的旧 Job；不可表达 DSL、权限或断言不发布为可执行资源；artifact 先暂存校验，引用提交后才正式关联。

**反例与测试：** 一条 legacy summary=.5 不造两题；缺 reported_model 不补当前 profile；imported completed Run 不被 Worker claim；缺许可证保持 restricted；artifact hash 不匹配拒绝；旧 baseline 不是新平台正式 pass。

**建议触点：** `motte_sdk/migration/apply.py`、`transforms.py`、`packages/storage/motte_storage/imports.py`、dispatcher claim guard、`tests/migration/test_legacy_import.py`。

### M7-T08：中断/冲突 → checkpoint、resume 与受限 rollback

**依赖：** T06/T07。
**目标：** G10、G14。
**验收：** A09、A12、A13。

**范围：**

- 唯一 key 为 source system/type/id/version/transform version；每批事务保存 checkpoint；resume 只处理未提交单元，不重复映射、关联或收费。
- 同键不同内容 conflict 停止该单元并保留诊断；数量、引用、Artifact hash、原始/归一化分数和状态生成核对报告。
- rollback 只撤销本批创建且未被其他资源引用的对象，或将批次标记 inactive；共享 Artifact、已有 baseline/pass、其他批次引用不得删除或覆盖。
- apply/resume/rollback 都需要 operator intent、import id、source manifest hash 和审计记录。

**反例与测试：** 半批崩溃恢复；同键内容冲突；重复 apply；共享 Artifact 回退保护；跨批引用阻断；回退不使历史变成 queued；checkpoint 损坏明确失败。

**建议触点：** `migration/apply.py`、`rollback.py`、storage imports/audit、`tests/migration/test_import_resume_rollback.py`。

### M7-T09：存量 DB/Artifact → 一致备份与 staging restore

**依赖：** T06、现有 maintenance/artifact/integrity。
**目标：** G15-G16。
**验收：** A14-A16。

**范围：**

- 维护窗口覆盖 Run/ScoringJob/资源发布/Judge/GC/Artifact 写入和备份；使用 SQLite online backup、PG 一致快照/导出，记录 schema/app/revision、Artifact 引用清单、hash、pins、retention class 和验证报告。
- DB 快照先确定不可变 Artifact 清单，再逐文件暂存、hash 校验；缺文件、额外文件、损坏 manifest、版本不兼容均不能标完整成功。
- 默认恢复到新 staging DB/root，先校验版本/hash/引用，再只读检查；queued/running/needs_review 不自动付费执行，未决状态需显式恢复决定。
- 升级/回退演练覆盖空库、上一支持版本、旧记录读取、migration failure、backup restore 和不认识状态；不编辑已发布 migration。

**反例与测试：** 备份时发布/评分/GC 写入被阻断或纳入一致协议；恢复缺引用 Artifact 失败；备份中 running Run 不自动 claim；SQLite/PG manifest hash；失败升级回到已验证读取配置；restore 不覆盖现有 DB，除非显式确认。

**建议触点：** `packages/storage/motte_storage/maintenance.py`、`artifacts.py`、`integrity.py`、SDK service、API/Worker maintenance mode、`tests/storage/test_maintenance.py`、`tests/integration/test_backup_restore_consistency.py`。

### M7-T10：本地/远程服务 → 安全、保留、GC 与供应链

**依赖：** T09、M6 pin 语义、M1-T06/T08。
**目标：** G17-G19。
**验收：** A17-A18。

**范围：**

- 默认 loopback；校验 Host/Origin/CORS/CSRF/DNS rebinding，执行/凭据管理端点不能被不可信网页触发；远程显式单用户认证与 TLS/安全传输，不能裸露执行 API。
- 凭据接口只返回引用/掩码；日志、错误、SSE、Trace、导出、Provider payload、Artifact 和 Web 都做脱敏；允许的内网 endpoint 与 credential binding 明确，阻断恶意 redirect 泄漏。
- retention 区分标准 Trace、原始 Provider payload、Artifact、失败、pinned Run、Baseline、ScoringPass、needs_review、import audit；GC 默认 dry-run，apply 需显式操作，删除写 tombstone；与采集/评分/备份互斥或有锁协议。
- 固定依赖/Runner 镜像/lock，执行 secret scan、dependency audit、license/data provenance 检查；公开仓库只含合成/授权脱敏 fixture。

**反例与测试：** untrusted Origin 不能 create Run/change credential；Host header/DNS rebinding；redirect 不泄漏 auth；pinned Artifact 不被 GC；needs_review/import audit 保留；dry-run 不删；删除有原因/hash/时间 tombstone；秘密/gold 不进日志；供应链检查失败阻断发布。

**建议触点：** API dependencies/middleware/config、maintenance/artifacts、Compose/CI、`tests/security/test_single_user_boundary.py`、security/retention tests。

### M7-T11：当前版本 → Release Candidate、安装和支持矩阵

**依赖：** T04/T05/T08/T09/T10、前序阶段发布证据、M6-T11。
**目标：** G16、G19-G20、G22。
**验收：** A19-A20。

**范围：**

- 固定 RC commit、lock、wheel、Web artifact、Docker image/provenance、migration head、M6 exporter/API schema 版本和 release notes。
- 建立 `docs/release/support-matrix.md`，按 OS/运行方式、SQLite/PG、Runner/runtime、Provider/Benchmark/Profile、操作（安装/执行/报告/迁移/备份/恢复）记录 tested/supported/experimental/blocked/not_run。
- 真实执行 clean install→migration→health→synthetic Run→report/Gate；上一支持版本升级、失败恢复、旧应用读取未知状态、真实 Compose build/up、PG/SQLite 和 Web/API/CLI 一致性。
- skip、compose config、静态 wheel 文件和 import 成功不能代替实际运行；未验证后端不进入 stable supported。

**反例与测试：** 空库安装；上一版本升级；migration failure 恢复；旧应用读取新状态明确不兼容而非 queued；Docker 容器实际 health/API/Worker；一个 backend live 未跑时矩阵不输出 stable。

**建议触点：** `tests/integration/test_release_smoke.py`、Compose/CI、`docs/operations/upgrade.md`、`rollback.md`、`docs/release/support-matrix.md`。

### M7-T12：三类替代场景、切换准备与最终 review

**依赖：** T11、M2-T11、M4-T10/T11、M5-T12。
**目标：** G21-G23，汇总 G01-G20。
**验收：** A21。

**三类场景：**

1. **模型评测：** 同一 C-Eval 数据/Profile，两个模型完整 Run、固定 pass、报告、比较、coverage 和 Gate 可复核；未验证来源保持 restricted。
2. **Agent Benchmark：** 固定 Terminal-Bench 版本经 Harbor，Task/Trial/Verifier/Artifact/取消/失败/恢复和成本可复核；Docker/Harbor 缺失则明确 blocked，不伪造 ready。
3. **业务回归：** 多轮 Scenario 与至少一个 Skill fixture/A-B，固定评分版本和回归 Gate 可进入 CI；Judge 未校准保持 experimental。

**切换记录：** 固定 RC、旧参考 commit、依赖与支持范围；停止旧平台新增功能和新付费任务；确认存量终态；只读备份与 dry-run；分批 apply/数量/hash/引用核对；新任务单路进入 MoTTEavl；旧服务/DB 只读保留；写回退窗口、未迁移能力分类和 release notes。不得自动归档旧仓库或反向迁移新平台进行中的 Run。

**最终 review：** 逐目标、工作包、验收场景和非编号章节核对实现、消费者、测试 node、commit、环境、限制和证据；精确记录 baseline/head 失败差异；P0/P1 必须关闭，P2/P3 必须关闭或说明不影响退出门。

## 4. 目标覆盖矩阵

| 目标 | 主责 |
|---|---|
| G01 | T01 |
| G02 | T01/T02/T03 |
| G03 | T01 |
| G04 | T03 |
| G05 | T02 |
| G06 | T04 |
| G07 | T04 |
| G08 | T05 |
| G09 | T05 |
| G10 | T06/T08 |
| G11 | T06/T07 |
| G12 | T07 |
| G13 | T07 |
| G14 | T08 |
| G15 | T09 |
| G16 | T09/T11 |
| G17 | T10 |
| G18 | T10 |
| G19 | T10/T11 |
| G20 | T11 |
| G21 | T12 |
| G22 | T11/T12 |
| G23 | T12 |

## 5. 验收矩阵

| ID | 条件 | 必须观察 | 主责 |
|---|---|---|---|
| A01 | checkout 外干净 venv 安装 SDK/wheel | 可导入调用，无 cwd/PYTHONPATH 依赖 | T05 |
| A02 | 422/auth/网络错误/重试 POST | 分类明确，不能无条件重复付费请求 | T01 |
| A03 | 服务已创建 Run 但响应丢失 | 同 key/body 得到同一 Run，不同 body conflict | T01 |
| A04 | 远程服务断开 | CLI 非零失败，本地 DB/provider/task-start 计数为 0 | T03 |
| A05 | SSE 重复、断线、终态同时到达 | 去重、续读、gap/partial、最终报告一致 | T02 |
| A06 | 安装 pytest 未启用评测 | 零模型/Benchmark/Run 调用 | T04 |
| A07 | JUnit 证据不足/空集 | error 或明确状态，不能生成假通过 | T04 |
| A08 | dry-run 后源文件变化 | apply 前 hash 复验并阻断旧计划 | T06 |
| A09 | 重复导入/同 ID 内容变化 | reused/conflict 区分，无覆盖 | T08 |
| A10 | 缺 gold/model/artifact/price/pass | unknown/不足，不从当前配置补历史 | T07 |
| A11 | 恶意路径/压缩炸弹/秘密字段 | 受控拒绝或脱敏，不读任意宿主路径 | T06 |
| A12 | 导入一半崩溃 | checkpoint 恢复，不重复关联或收费 | T08 |
| A13 | 回退涉及共享 Artifact | 不删除其他 Run/Batch 引用对象 | T08 |
| A14 | 备份时发布/评分/GC 写入 | 维护边界阻断或一致快照协议保持 | T09 |
| A15 | 恢复包缺被引用文件 | 校验失败，不标完整成功 | T09 |
| A16 | 备份含未决/进行中 Run | 恢复不自动付费执行，保持待复核 | T09 |
| A17 | 不可信网页请求本地任务接口 | Origin/Host/CSRF/认证边界拒绝，不触发执行 | T10 |
| A18 | 清理过期但被 pin 的证据 | 保留并说明 pin 原因，删除有 tombstone | T10 |
| A19 | 旧应用读取新状态/升级失败 | 明确不兼容或恢复路径，不误当 queued | T11 |
| A20 | 一个后端 live 未验证/测试跳过 | 支持矩阵标 not_run/blocked，不计 stable | T11 |
| A21 | 三类替代场景缺任意一个 | cutover readiness 不通过 | T12 |

## 6. 验证命令与证据

按当前实际目录调整命令，但不能把不存在的测试路径当作已运行：

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

补充证据按环境分层：

- clean venv/wheel 安装与 package import；
- SQLite 维护、PG 一致快照/恢复与两连接写入阻断；
- Docker Compose build/up/health/API/Worker/migrate；
- fake HTTP/SSE/CLI server parity；
- 脱敏、Origin/Host/CSRF/TLS/auth、GC pin/tombstone、dependency/license/data scan；
- DeepSeek V4.1 Flash 有界 smoke，只在用户授权、离线门禁完成且费用上限已固定后执行；
- 真实旧导出和三类替代场景单独记录来源、版本、许可、样本、费用、操作人、清理和回退窗口。

每轮测试立即更新 `docs/verification/M7.md` 和 `docs/PROGRESS.md`，记录命令、SHA、环境、passed/failed/skipped/blocked/not_run、测试数据 hash、服务 PID/端口、模型调用/费用和证据路径。测试失败必须复现、修复、focused/相邻/最终门禁重跑。

## 7. 交付、回退与主干集成

交付文档：

- `docs/protocols/sdk-and-migration.md`；
- `docs/operations/sdk-and-cli.md`、`import-legacy.md`、`backup-restore.md`、`upgrade.md`、`rollback.md`；
- `docs/migration/legacy-capability-map.md`；
- `docs/release/support-matrix.md`、`cutover.md`、`release-notes.md`；
- `docs/verification/M7.md`；
- API/OpenAPI/TS、migration、wheel/lock/provenance、Compose/CI 更新。

回退顺序：停止新远程执行、导入 apply、GC apply 和发布入口；确认活动 Run/评分/备份状态；保留不可变历史、原始 Artifact、import audit、tombstone 和 backup；优先回退应用，schema 不可逆时从 staging/backup 恢复；不把新平台进行中的 Run 反向迁回旧执行器。

最终集成只有在 T01–T12、G01–G23、A01–A21、专项测试、支持矩阵、review 和验证账本完成后进行：在 M7 worktree clean 的前提下拉取最新 main，合并 M7 分支；合并后重新跑关键 smoke、OpenAPI/TS drift、Web test/build、make check 和 release smoke；主干复验通过后再推送，并记录 merge SHA、远程 SHA、CI 状态、支持范围和切换决定。