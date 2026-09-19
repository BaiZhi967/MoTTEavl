# M7：SDK、历史迁移、运维与稳定发布详细规划

> 状态：待实施。打包、安全、迁移和恢复从前期随功能建设，本阶段统一完成稳定交付与旧平台切换。文档提交不代表数据已迁移、服务已部署或版本已发布。实施按测试先行、独立提交与评审推进，可使用 superpowers:subagent-driven-development 或 superpowers:executing-plans。

**Goal：** 在声明支持的环境中完成干净安装、类型化外部调用、可机器读取的 CI 结果、安全的历史导入及备份恢复，使 MoTTEavl 能替代旧平台主线而不丢失历史解释能力。  
**Architecture：** Python 客户端面向新公共 API；本地/远程 CLI 共享契约，不共享数据库访问假设。历史导入是一条只读来源、可审计、可幂等执行的数据管道，不是长期双写。发布以能力矩阵和替代场景为验收门。  
**Tech Stack：** 现有 Python 3.12/uv、pnpm、FastAPI/React、SQLite/PostgreSQL、Docker/Compose、现有 migration 与 Artifact store。  
**Spec：** [总路线](../ROADMAP.md)第 12、16、17 节、[阶段索引](README.md)、[M6 结果与退出语义](M6-experiments-comparison-and-gates.md)。  
**基线：** MoTTEavl `a668d13ee5ea0c3613648f8992ecaa4a148d3855`；旧项目 `b661bcdf83e1c3dfb8d6062ee78817d249e86a4c`。

## 1. 依赖与范围

发布的功能范围由 M1–M6 的实际验收决定，不由目录存在或 CI 总体成功决定。SDK 的 Run/ScoringPass 读取可以提前交付，M6 的实验/Gate API 稳定后再增加对应客户端方法；备份、安全和 wheel 构建不应等到本阶段才第一次验证。

当前已有 CLI、本地 sdk-python 执行服务、维护工具和完整性升级说明。[B1][B2] 旧 SDK 的客户端、Trace、pytest/导出能力是迁移参考，但旧路径、权限模型、资源 ID 和返回结构不能直接复制。[B3]

### 本阶段包含

类型化同步 Python 客户端；CLI 本地/远程一致性；SSE 恢复；pytest/CI/JSON/JUnit；workspace 独立构建与安装；旧配置/数据/历史结果导入；备份恢复与工件一致性；单用户安全部署；证据保留；版本兼容与发布清单；旧平台停止新增开发的切换流程。

### 不包含

自动归档/删除旧仓库、生产数据无确认覆盖、多语言 SDK、同时重写异步客户端、迁移进行中的旧任务、跨平台任意二进制完全一致、企业多租户/RBAC、长期双数据库同步以及强制采用 Kubernetes。

## 2. 最终具体目标清单

- [ ] M7-G01：Python 同步客户端有稳定类型、错误、版本兼容和能力协商。
- [ ] M7-G02：提交、等待、取消、retry、事件、指定 pass 报告、比较和 Gate 都能通过新 API 使用。
- [ ] M7-G03：客户端重试不会无条件重复付费 POST；创建请求幂等键与内容一致性可验证。
- [ ] M7-G04：远程服务不可用时 CLI 明确失败，不静默转为本地执行。
- [ ] M7-G05：SSE 重连可恢复游标、补齐缺口，终态与最后事件不丢失。
- [ ] M7-G06：pytest/CI 默认不触发付费评测，启用真实评测需明确授权。
- [ ] M7-G07：JSON/JUnit/退出码保留质量失败、证据不足、不可比、执行错误与安全区别。
- [ ] M7-G08：wheel 和 Web 构建在干净环境可安装，运行不依赖开发者 cwd/PYTHONPATH。
- [ ] M7-G09：每个工作区包声明真实依赖，Runner 依赖不污染 API 运行环境。
- [ ] M7-G10：旧资源导入支持 dry-run、来源/版本映射、逐条诊断和幂等重跑。
- [ ] M7-G11：凭据只迁引用，不复制密钥、登录会话或整个本机配置目录。
- [ ] M7-G12：历史 Run/Score/Artifact 以只读来源记录导入，不进入执行队列。
- [ ] M7-G13：缺失模型、评分、费用、许可或证据明确 unknown，不从当前配置反推历史。
- [ ] M7-G14：迁移中断恢复和回退只处理本次新增资源，不删除共享工件或覆盖既有记录。
- [ ] M7-G15：数据库与 Artifact 备份属于一致快照，恢复后引用与 hash 可核对。
- [ ] M7-G16：升级、回退、运行中断与旧 schema 读取在支持环境完成演练。
- [ ] M7-G17：默认本地访问安全；远程部署具备显式认证与加密传输，不裸露执行接口。
- [ ] M7-G18：保留/删除政策保护基线、已固定结果和 needs_review 证据，删除有审计。
- [ ] M7-G19：公开发布前完成密钥、私有数据、依赖与数据许可检查。
- [ ] M7-G20：每个正式支持的后端有指定版本和环境证据，跳过测试不计为通过。
- [ ] M7-G21：三个旧平台替代场景全部验收，未迁移功能有明确归类。
- [ ] M7-G22：发布文档、示例、兼容矩阵和实际命令一致，不声称所有后端均稳定。
- [ ] M7-G23：功能切换与历史迁移分别验收，不以迁移行数代替产品可用性。

## 3. 模块与文件范围

| 模块 | 现有触点 / 拟新增文件 | 具体实现范围 | 不承担 |
|---|---|---|---|
| Python API 客户端 | 在 `packages/sdk-python/motte_sdk/` 新增 `client.py`、`client_errors.py`、`client_types.py` | 类型化 HTTP、超时、错误映射、幂等、安全等待/SSE | 复制旧 API 的资源层级、直接操作服务端 DB |
| SDK 包边界 | 现有 sdk-python `__init__.py`、`pyproject.toml`及必要 extras | 客户端使用不隐式导入全部 Worker/Runner 依赖 | 为“轻 SDK”仓促重排整个 monorepo |
| CLI | `packages/cli/`现有命令 | local/remote 分流、统一参数/退出码、JSON/JSONL、显式 live | 网络失败后静默改执行地点 |
| Trace/pytest | 新增 `motte_sdk/tracing.py`、`pytest_plugin.py` | 外部 callable 观测、测试门禁、显式开关 | 无授权自动运行大型评测或重复调用被测函数 |
| 导出 | `packages/evaluators/`与报告服务；新增 `motte_sdk/export.py` | 固定报告/Comparison/Gate 的 JSON/JUnit | 导出时重新运行 scorer/Judge |
| 迁移契约 | 新增 `packages/contracts/motte_contracts/imports.py` | ImportManifest、SourceIdentity、Mapping、Report | 将历史记录伪装成新执行产生 |
| 迁移服务 | 新增 `motte_sdk/migration/`、相应 storage repository | validate/plan/apply/resume/report、内容校验和回退 | 长期双写或任意 SQL 执行 |
| 备份恢复 | 扩展 `packages/storage/motte_storage/maintenance.py` | 一致快照、引用清单、恢复 staging、核对 | 无确认覆盖用户现有数据库 |
| 发布与构建 | 根/各包 pyproject、Dockerfile、`infra/`、CI、Web package | 可安装产物、依赖锁、矩阵验证与安全检查 | 把 compose config 当作启动成功 |
| 单用户访问保护 | 现有 API/代理配置、配置与凭据入口 | loopback/Origin、认证、TLS 部署、安全配置 | 多租户、角色与组织系统 |
| 操作界面 | 现有设置/报告页，按需新增导入与恢复报告视图 | dry-run 诊断、历史来源、支持层级和操作结果 | 浏览器直传任意宿主路径或读取凭据正文 |

新的客户端类与本地执行服务命名清楚区分；`import motte_sdk` 不应启动数据库、Worker 或隐式模型调用。将来需要独立客户端 wheel 时先以兼容 re-export 保留已公开入口，不在本阶段同时维护两套 API 定义。

## 4. Python 客户端与 CLI 契约

### 4.1 客户端能力

首批方法域：providers/models 的必要读取与发布；create/get/list/wait/cancel/retry Run；事件游标；读取指定 ScoringPass 的报告/Artifact 元数据；发起明确模式的 rescore；Experiment、Comparison、Baseline、Gate 与导出。

响应类型来自公共 Contract/OpenAPI，使用兼容薄封装，不把所有返回值变成无约束 dict。保留服务端 error code、分类、请求 ID、可重试标志及安全 detail；不能把 HTTP 422 配置错误当网络错误无限重试。

客户端设置：base_url、认证引用、connect/read/overall timeout、重试政策、poll interval、响应/事件大小上限。默认重试只覆盖已知安全查询；变更请求只有服务端支持幂等键且请求 hash 相同才允许安全重送。SDK 不承诺外部 Provider exactly-once。

Run 创建的 idempotency_key 必須绑定规范化请求 hash；相同 key 不同 body 返回冲突，同 key同 body 返回同一创建结果。该机制在公共入口实现，CLI/Web/SDK 共用，不由各客户端自己猜测是否重复。

### 4.2 等待、取消与 SSE

wait_for_run 使用总期限和取消能力；遇到 needs_review 返回需要用户处理的明确结果，不自动调用 retry。超时只停止等待，不等于远端任务已取消；是否取消要另行显式操作。

SSE 支持 Last-Event-ID/after 游标、心跳、断线和终态。客户端按稳定 event ID 去重；服务端已清理某段事件时返回可识别缺口并使用可用的持久查询补齐，无法补齐则标记 partial，而不是假装完整。收到终态后要确保最后一批事件/最终报告读取一致。

长流式错误和用户中断必须释放连接；stdout 保持机器数据，诊断到 stderr。凭据、完整 prompt 和敏感 payload 不自动写 SDK debug 日志。

### 4.3 本地/远程模式

CLI 显式选择 local 或 server。远程模式只通过 API，不能拿服务端资源 ID 去访问本地数据库；远程不可达时失败，不回退为本机执行。所有创建和评分参数共享公共验证，服务端仍是最终校验者。

默认 local replay/确定性验证零模型费用；真实 subject、Judge、Harness 调用分别需明确动作和授权。`--json` 输出稳定 envelope；既有参数/退出码变化需要版本策略与弃用说明。

## 5. pytest、Trace 与 CI 导出

pytest 插件提供显式 marker/fixture：只有用户开启对应评测模式才提交 Run 或执行 Gate。普通 `pytest` 不因安装插件就扫描全部数据集、读取凭据或发起模型请求。已完成报告的门禁读取与真实执行分成不同 fixture。

Trace decorator/context manager 记录用户提供 callable 的一次执行，不为采集结果重复调用。嵌套 span、异常、脱敏、采样/截断、部分 flush 失败有明确行为；Trace 上报失败不能悄悄把被测业务执行两次。

导出固定 report/scoring_pass/policy 引用。JSON 包含全部结果状态、来源、分母和缺失原因。JUnit 需要明确将质量断言失败映射 failure、执行/评分基础设施故障映射 error、合法未适用映射 skipped；证据不足默认 error，不能导出成看似通过的空测试集。安全阻断按 Gate 高优先级单独保留。

CLI 退出语义沿 [M6](M6-experiments-comparison-and-gates.md) 的统一版本，不复制另一份规则。CI 示例先运行合成 fixture/replay；显式 live 使用隔离环境与密钥作用域，不能将真实凭据写进 workflow 或测试报告。

## 6. 历史迁移模型与流程

### 6.1 导入契约

| 对象 | 最小字段 |
|---|---|
| ImportManifest | import_id、importer_version、source_repository/revision、source_schema_version、exported_at、record_counts、artifact_manifest_hash、scope |
| SourceIdentity | source_system、source_record_type、source_id、source_version/content_hash |
| MappingRecord | SourceIdentity、target_type/id/version、transform_version、status、diagnostic_refs |
| ImportReport | planned/created/reused/rejected/conflicted counts、missing_artifacts、unknown_fields、warnings、checkpoint、verification hashes |

这些名称是拟议契约。imported 与 newly_executed 是来源维度，不在状态机中伪造成功动作。历史 Run 处于只读集合或带不可分发 origin 标记，Worker 明确不能 claim；真实需要重跑必须由用户基于可解析资源创建新的 Run。

### 6.2 迁移次序

1. **配置：** ProviderConnection 与 ModelProfile 先建立映射；旧 Alias/Deployment 只按可证明的固定关系解析。实际密钥不复制；生成需要重新绑定的凭据引用清单。
2. **数据：** DatasetVersion、Case、内容 hash、来源/许可和选样信息。缺源文件时可导入元数据，但不能标为 ready。
3. **场景与 Skill：** 使用 M5 的受控转换，输出无法表达的字段/权限/断言诊断。不能静默删除分支、循环或 checker 后发布可执行版本。
4. **历史结果：** Run、Attempt/Trial、Trace、Artifact、Score、Baseline/Gate 来源记录。保留原始 schema 和字段；映射出未知信息而非猜测补齐。
5. **核对：** 数量、幂等映射、引用完整性、Artifact hash、原始/归一化分数及状态；生成只读差异报告。

不迁进行中的旧 Job。先等待明确完成或由操作者停止并记录结果；本导入器不能跨两套执行器接管付费请求。

### 6.3 dry-run 与幂等

dry-run 只读取来源与目标元数据并生成独立报告，不修改目标业务数据、凭据、状态或 Artifact。apply 按经过核对的 manifest 执行，检测源内容是否在 dry-run 后改变；改变则重新验证，不能沿用旧批准。

唯一映射键为 source_system + source_record_type + source_id + source_version + transform_version。相同来源/内容重复导入为 reused；同键内容不一致为 conflict，不覆盖目标。每个批次事务保存 checkpoint，恢复只处理未提交单元。

Artifact 先按内容 hash 校验并存入受控暂存区，数据库引用提交后才作为正式工件。路径穿越、symlink 逃逸、压缩炸弹、超配额和不受允许的 URL 拒绝。API 不接受任意宿主路径来读取旧库；来源必须由操作者在受控目录/明确导出包中提供。

### 6.4 历史信息不足

缺 requested/reported model、价格表、ScoringPass、样本集合或原始文件时，保留 unknown 与来源限制。不能用当前 ModelProfile/PriceTable 填历史事实。旧聚合只有总分时可保存 legacy summary，不能伪造逐样本分数。

旧 baseline/gate 可作为原系统的历史结论导入，但在新平台参与正式 Gate 前必须符合 M6 的比较和证据资格；原系统 pass 不是新平台 pass 的自动证明。

### 6.5 回退

导入记录持有 import_id 与新建/复用标志。回退只撤销本次创建且尚未被其他资源引用的目标，或者将导入批次标记 inactive 保留审计；不能删除内容去重后被其他 Run 使用的 Artifact。涉及跨批次引用时输出阻塞诊断并让操作者处理。

旧数据库与导出包保持只读，不对旧平台反向写入。不建立持续双写、双调度或双数据库同步。

## 7. 备份、恢复、升级与证据保留

### 7.1 一致备份

备份必须包含数据库 schema/version、应用版本、Artifact 引用清单、文件 hash、保留 pin、配置元数据及校验报告。秘密凭据默认不进入普通备份；单独的秘密备份必须由用户明确选择并采用独立安全存储流程。

首版建议维护窗口：暂停新的 Run/ScoringJob 和资源写入，确认单执行器达到可备份状态，并阻止 GC 与 Artifact 删除。SQLite 使用在线 backup API 获取一致 DB；PostgreSQL 使用一致的数据库快照/导出。再按该 DB 快照列出的不可变 Artifact 清单复制并校验，而不是扫描当前目录就假设对应同一时点。

暂停调度不等于停止所有写入；模型资源发布、Judge 评分和 UI 修改也必须被维护模式挡住或纳入快照一致性协议。备份完成前缺少引用文件必须报失败/不完整，不能只因 dump 命令退出 0 就标记成功。

### 7.2 恢复演练

默认恢复到新的 staging 数据库和 Artifact root，先校验格式、版本、hash 和引用，再启动只读检查。确认没有 queued/running 记录会被恢复环境自动付费执行；未决任务保持 needs_review 或需要显式恢复决定。

核对至少包含 Run 数、历史 pass 数、baseline 引用、抽样 Artifact hash、资源版本、模型凭据待绑定状态。替换用户现有运行环境必须另行明确确认；本规划不授权覆盖现有生产数据。

### 7.3 升级与回退

每次 release 测试空库安装、上一支持版本→当前版本、旧记录读取、备份后升级失败恢复。migration 沿实际 head 追加；不能编辑已发布 migration 改历史。

优先回退应用并保留新增审计表。确需 schema downgrade 时先说明会删除哪些证据及旧版本不能识别哪些状态；不可逆迁移使用恢复备份，不承诺无损降级。回退后 Worker 不得把不认识的状态当 queued。

### 7.4 Retention/GC

保留政策区分标准 Trace、原始 Provider payload、Artifact、失败与 pinned Run。基线、固定 ScoringPass、未决 needs_review、导入审计所需引用优先保护；不能只按文件 mtime 删除仍被使用的证据。

cleanup 默认 dry-run，明确 apply 才删除；删除保留 hash、大小、原因和时间的 tombstone。GC 与活跃采集/评分/备份互斥或遵循明确锁协议。缺少已删除必要证据时，rescore/compare 返回不足原因，不能重新读取可变工作区假装原证据仍在。

## 8. 单用户安全与供应链边界

默认 loopback 监听，不因此忽略浏览器 Origin/CORS/CSRF 与 DNS rebinding 风险。能创建任务、管理凭据或执行工具的接口必须防止不可信网页通过浏览器触发。远程访问需显式启用单用户认证与加密传输，不能为了省去多用户功能而裸露执行 API。

凭据接口只返回掩码/引用；日志、错误、SSE、Trace、导出、模型原生配置与 Artifact 都有脱敏回归。允许用户配置内网模型端点是产品需求，网络策略应明确允许的目的地址/域名与凭据绑定，不能既全面封杀合法局域网又允许任意重定向泄漏认证信息。

新 Runner/runtime 包来源与版本固定，依赖扫描、安全配置检查、许可清单与构建 provenance 可查。公开仓库只提交合成或明确授权脱敏 fixture；私有业务导出、真实响应、用户数据与模型密钥不作为测试便利而上传。

不把扫描零告警当作系统绝对安全；对受控执行、工作区、权限与数据流做实际集成验证。远程单用户保护不是企业 RBAC，保持范围有限。

## 9. 构建、CI 与支持矩阵

| 验证层 | 必须执行的内容 | 不可替代的边界 |
|---|---|---|
| Python/Contract | 单元、公开类型、错误/退出码、旧读兼容、依赖声明 | import 成功不等于 wheel 可安装 |
| Web | 类型生成、测试、构建、路由/状态/长日志交互 | build 成功不等于实际 API 接线 |
| 本地集成 | SQLite、PG、API/Worker、假 HTTP/CLI、确定性 Docker | fake binary 不能证明真实 CLI 支持 |
| 安装发布 | 干净 venv 安装 wheel、内嵌资源、Compose build/up、migration/health/Run | compose config 不代表容器启动 |
| 显式 live | 每个正式支持的 Provider/runtime/Benchmark 配置 | 只验证实际版本和范围，不推断全组合 |
| 运维演练 | 备份、恢复、升级、取消、未决结果与残留 | 不能只检查命令退出码 |

支持矩阵按操作系统/运行方式、存储、Runner/runtime 版本、任务/Profile、已验证能力登记。Linux 与 WSL2 可以先作为环境型任务的一等支持；Windows 原生仅对实际验证功能声明。未运行、跳过、环境缺失和失败分开记录。

v1.0 是建议的产品退出范围，不是当前已有标签。正式发布前给出 release candidate 的明确范围；某外部 CLI 未满足门槛时标 experimental 并在范围决策中说明，不能静默从清单删掉又声称全部完成。

## 10. 详细实施工作包

| 任务 | 输入 → 产出 | 具体范围 | 测试（拟新增/扩展） |
|---|---|---|---|
| M7-T01 | 公共 API →SDK 类型/错误 | 同步客户端、超时、兼容协商、安全重试与幂等请求 | `tests/sdk/test_client_contract.py`：422 不重试、相同 key 同 Run、不同 body 冲突 |
| M7-T02 | Run/事件 →等待与流 | wait、needs_review、取消、游标、终态与断线恢复 | `tests/sdk/test_wait_and_events.py`：超时只停等待、无重复/丢失终态 |
| M7-T03 | 本地/远程配置 →CLI 一致输出 | server/local 明确路由、同参数验证、错误和 JSON | `tests/cli/test_remote_parity.py`：网络失败不执行本机任务 |
| M7-T04 | report/Gate →pytest/导出 | 显式 marker、一次 callable Trace、JSON/JUnit/退出码 | `tests/sdk/test_pytest_and_exports.py`：默认零费用、unknown 不导出为空通过 |
| M7-T05 | workspace →可安装产物 | 真实包依赖、wheel、资源打包、Web/Runner 隔离 | `tests/packaging/test_clean_install.py`：干净 venv 无源码路径依赖 |
| M7-T06 | 旧导出包 →迁移计划 | ImportManifest、来源验证、数量/hash、dry-run、凭据待绑定 | `tests/migration/test_import_plan.py`：dry-run 目标 DB 零修改 |
| M7-T07 | 迁移计划 →版本化资源/历史 | 配置→数据→Scenario/Skill→Run/Score/Artifact，历史不可分发 | `tests/migration/test_legacy_import.py`：缺字段 unknown、历史 Run 不被 Worker claim |
| M7-T08 | 中断/冲突 →可恢复导入 | checkpoint、幂等、冲突、引用核对与受限回退 | `tests/migration/test_import_resume_rollback.py`：重跑不重复、回退不删共享 Artifact |
| M7-T09 | 存量 DB/工件 →一致备份恢复 | maintenance mode、引用清单、staging restore、hash 检查 | 扩展 storage maintenance tests；`tests/integration/test_backup_restore_consistency.py` |
| M7-T10 | 本地/远程服务 →安全发布边界 | Origin/认证、凭据脱敏、retention pin/GC、供应链检查 | `tests/security/test_single_user_boundary.py`：不可信 Origin 不能创建执行/改凭据 |
| M7-T11 | 当前版本 →发布候选 | 空库/升级/真实 Compose/平台矩阵/回退/示例验证 | `tests/integration/test_release_smoke.py`与受控发布验证流程 |
| M7-T12 | 替代场景与迁移结果 →切换记录 | 三场景验收、旧功能归类、只读旧库、回退窗口和最终 release notes | `tests/integration/test_cutover_readiness.py`：缺任何必备证据不输出 ready |

T01/T02/T05/T09/T10 可从 M1 起持续建设；T03/T04 等待相应 API 稳定；T06–T08 可独立使用合成旧数据，T11/T12 以完整能力矩阵收口。每项先写失败测试，验证最小实现、focused/相邻回归、文档和构建，再独立提交。

### 合成导入 fixture（拟议规范）

```json
{
  "source_system": "evalstudio",
  "source_schema_version": "fixture-v1",
  "source_records": [
    {"type": "run", "id": "legacy-run-1", "status": "completed", "reported_model": null},
    {"type": "summary", "run_id": "legacy-run-1", "score": 0.5, "case_details": null}
  ],
  "artifacts": []
}
```

预期：可导入一个只读 legacy Run 和有来源的 summary；reported_model、样本明细和不可证明的评分版本保留 unknown。不能合成两道题的分数来凑 0.5，也不能使用当前模型档案填入 reported_model。重复导入为 reused，Worker 不执行这条记录。

## 11. 强制验收矩阵

| ID | 场景 | 必須结果 |
|---|---|---|
| M7-A01 | SDK 在干净 venv 安装 | 可导入与调用，无源码/PYTHONPATH 依赖 |
| M7-A02 | 422/认证失败/重试 POST | 分类明确，禁止无条件重复提交付费请求 |
| M7-A03 | 服务端已创建 Run、响应丢失 | 同幂等键重送得到同一 Run |
| M7-A04 | 远程服务断开 | CLI 失败，不静默在本地执行 |
| M7-A05 | SSE 重复、断线、终态同时到达 | 去重/续读/最终报告一致，缺口显式提示 |
| M7-A06 | 安装 pytest 插件但未启用评测 | 零模型/Benchmark 调用 |
| M7-A07 | JUnit 导出证据不足或空集 | error/明确状态，不生成假通过 |
| M7-A08 | dry-run 后源文件改变 | apply 前重新校验并阻断旧计划 |
| M7-A09 | 重复导入/相同 ID 内容不同 | reused/conflict 区分，无覆盖 |
| M7-A10 | 缺旧 gold/模型身份/工件 | unknown/不足，不能从当前配置补历史 |
| M7-A11 | 恶意路径/压缩炸弹/秘密字段 | 受控拒绝或脱敏，不读取任意宿主路径 |
| M7-A12 | 导入一半崩溃 | 从 checkpoint 恢复，不重复关联或收费 |
| M7-A13 | 回退批次使用共享 Artifact | 不删除其他 Run 的引用对象 |
| M7-A14 | 备份时发布模型/评分/GC 写入 | 维护边界阻断或快照协议保持一致 |
| M7-A15 | 恢复包缺一个被引用文件 | 恢复校验不通过，不标完整成功 |
| M7-A16 | 备份含未决/进行中 Run | 恢复不自动付费执行，保持待复核 |
| M7-A17 | 不可信网页请求本地任务接口 | 安全边界拒绝，不触发执行 |
| M7-A18 | 清理过期但被 baseline pin 的证据 | 保留并说明原因 |
| M7-A19 | 旧应用读取新状态或迁移失败 | 明确不兼容/恢复路径，不误当 queued |
| M7-A20 | 一个后端 live 未验证/测试跳过 | 矩阵标未验证，不计作稳定支持 |
| M7-A21 | 三个替代场景缺任意一个 | cutover readiness 不通过 |

## 12. 发布与旧平台切换清单

### 三个替代场景

**模型评测：** 相同 C-Eval 数据/Profile 下两个模型完整执行、样本报告、可比性与覆盖门禁可复核。  
**Agent Benchmark：** 固定 Terminal-Bench 版本经 Harbor，Task/Trial/Verifier/Artifact/取消/失败完整。  
**业务回归：** 多轮 Scenario 与至少一个 Skill fixture/A-B，固定评分版本与回归 Gate 可进入 CI。

### 切换步骤

1. 固定 MoTTEavl release candidate、迁移参考提交、依赖与支持范围。
2. 停止旧平台新增功能和新的付费任务，确认存量任务终态或由操作者处理。
3. 备份旧库/Artifact；以只读导出进行 dry-run，核对诊断与凭据重新绑定清单。
4. 分批 apply 并验证数量/hash/引用；历史与新执行来源清楚区分。
5. 在新平台跑三类替代场景及恢复演练，形成明确验收记录。
6. 新任务只进入 MoTTEavl；旧服务/数据库保留只读参考与已定义回退手册，不双写。
7. 发布实际兼容矩阵、限制、迁移报告和未迁移功能清单。归档旧仓库是另外的明确操作，不由本计划自动执行。

失败时回到已验证的读取/执行配置，不尝试把新平台进行中的 Run 反向迁回旧执行器。切换不能用“不报错”替代结果和成本核对。

## 13. 验证命令与交付文件

```bash
uv run pytest -q -m "not live" tests/sdk tests/cli tests/migration tests/security
uv run pytest -q -m "not live" tests/integration/test_backup_restore_consistency.py
uv run pytest -q -m "not live" tests/integration/test_release_smoke.py
uv run ruff check .
pnpm --dir apps/web test
pnpm --dir apps/web build
make check
```

新目录/测试文件由相应任务创建；以上是执行要求，不是本次已运行记录。wheel 构建与安装步骤按各包实际 build backend 写进 release workflow；真实 Compose build/up、PG、Docker、原生 CLI 以及付费 live 分环境记录，不能只跑配置检查。

交付文档：`docs/operations/sdk-and-cli.md`、`docs/operations/import-legacy.md`、`docs/operations/backup-restore.md` 更新、`docs/operations/upgrade.md`、`docs/operations/rollback.md`、`docs/migration/legacy-capability-map.md`、`docs/release/support-matrix.md`、`docs/verification/M7.md`。发布时每项必备目标引用 commit、测试和操作记录，未通过项保持未勾选。

## 14. 固定来源

- [B1] [当前 SDK/执行服务](https://github.com/BaiZhi967/MoTTEavl/tree/a668d13ee5ea0c3613648f8992ecaa4a148d3855/packages/sdk-python)
- [B2] [当前完整性升级、恢复与评分说明](https://github.com/BaiZhi967/MoTTEavl/blob/a668d13ee5ea0c3613648f8992ecaa4a148d3855/docs/operations/platform-integrity.md)
- [B3] [旧 SDK 状态与能力来源](https://github.com/BaiZhi967/llm_agent__evaluation_platform/blob/b661bcdf83e1c3dfb8d6062ee78817d249e86a4c/docs/core-package-status.md)

这些来源仅说明迁移起点；最终交付状态以对应实现提交和验证记录为准。任何版本范围变化必须更新本阶段验收表，不能以“文档已提交”代表产品已经发布。
