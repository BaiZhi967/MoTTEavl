# M4 完成复核记录

状态：**implementation_complete / live_pending，已按用户要求合入主干**。本轮代码修复、独立复审和 Linux/PG 完整 CI 已通过；真实模型验收未执行。2026-09-21 用户明确要求先合并并推送，已将 `codex/m4-pi-external-harnesses` 快进合入 `main` 并推送至 `origin/main`，合入点 `adf468aec4b46ab5f17db7ded56354d93dd2673e`，该提交 [CI 35556677158](https://github.com/BaiZhi967/MoTTEavl/actions/runs/35556677158) 全绿。合入不改变 live 待验事实，也不阻止 M5 开发。前一代码验证提交为 `270bfadcb7c23097cb49e6817cde22db039462ee`，本轮起始提交 `57227fae9b670a22193b6e86467679fb99c4c390`。本文是当前状态入口；[前一轮记录](M4.md)中的历史能力边界不作为本轮最终结论。

本轮要求包含全部 T01–T11；不能用 batch 闭环代替 T10/T11，也不能用离线 fake 或本地 HTTP stub 代替真实模型验收。M5 仅编写计划与交接提示词，没有实施。

## 主要修复与实现

- Pi 使用真实 0.73.1 SDK；补完整模型参数、SDK error/aborted、native usage 存在性与原生 total、max_steps/max_tool_calls 执行门。停止确认先于产物冻结；停止未知直接隔离并保留工作区。Observation 绑定真实 CaseAttempt。
- 公共预算拒绝 bool、NaN、无限值、负数和不可落实字段；发布 Profile 与展开后的 Profile 都拒绝秘密值字段。版本发布同内容幂等；模型单因子比较不被包含模型的整体配置 hash 误伤。
- Windows 进程先挂起、加入 Job Object 再运行；双管道、回调排空、输入写入与时间/输出限额有界。batch stdin 为 DEVNULL，交互显式 opt in。会话跨进程 CAS；损坏或身份不符的记录进入复核，不自动重放。
- Codex 原生工具不再只保留 shell；终态冲突、跨 thread、终态后事件和非法 discriminator 降为错误或证据不足。未知计量不补零，费用/计数校验非负有限值。不完整工具轨迹及 opaque 工具副作用不能证明“从未禁止写入”。
- CLI 为每次执行建立独立 native home；只支持明确命名的凭据引用，主机个人登录不继承。版本探测也使用隔离环境和有界 supervisor。配置记录 binary/Git/candidate hashes、环境名称与凭据存在性；加载来源无法证明时固定 partial，strict runner 政策拒绝。Git 快照禁止外部 diff/textconv/fsmonitor，扫描和 hash 有资源上限。
- 配置秘密值不会进入已知输出、错误、产物内容/名称或快照键；脱敏产物标注 redacted，不能用于证明原始文件内容满足要求。该措施不等于证明第三方工具不能读取其进程认证材料，工具强制能力仍如实展示。
- Web 可选择完整 Runtime/Profile 版本、预算和 Case 子集；不可强制工具政策需要显式确认。交互控制区绑定当前会话/revision/hash，区分接收、投递、原生确认、请求已解决和未知；损坏 202 保留原去重键等待查询，不自动重发。
- Codex app-server 发布新版本 **@2**，保留历史 @1。原生 0.155.1 JSON-RPC transport、单 reader、thread/turn 绑定、真实 steer/审批回复/interrupt、Worker consumer、三存储事务、审计和恢复已实现。request_resolved 不宣称动作执行成功。人工干预冻结进评分 pass/报告及中央比较条件。
- Inspect 原生 EvalLog v2 按 eval_id/run_id 定位，原始 UTF-8 字节冻结；导入 Run/Case/Observation/native ScoreSet 原子发布，重复上传可完成中断登记。导入不进入 Dispatcher，retry/rescore 当前明确拒绝；缺失指标不补分。

## 目标、工作包与验收对照

“离线实现”指实现与相关回归存在；最终 gate、平台和 live 状态仍以本页后文为准。

| 目标 | 工作包 | 当前证据与边界 |
|---|---|---|
| G01/G02 | T01/T02 | 不可变资源、版本门、readiness 分离；不因 probe 成功升级真实执行支持 |
| G03/G05 | T03/T04 | 真实 Pi SDK scripted 与本地 HTTP 协议测试；真实外部模型待验 |
| G04 | T04/T06/T07/T10 | CaseAttempt/session/workspace 隔离、实际身份冻结；历史不复用 |
| G06/G07 | T02/T06/T07 | 受控 native config/auth、候选 hash 与 partial/strict；完整原生加载观测未知 |
| G08/G13 | T03/T06/T07 | 原生字段解析、未知保持未知、终态/计量/类型反例 |
| G09/G10/G11 | T05 | 有界 supervisor、Windows Job、停止未知隔离、session CAS/恢复；Linux/PG 最终完整 CI 通过 |
| G12 | T04/T06/T07 | 沿用 FrozenObservation/Artifact/ScoringPass，退出成功不等于质量通过 |
| G14 | T08 | 中央比较 Runtime 条件、模型单因子和工具政策 |
| G15 | T08 | API/CLI/Web 配置与证据，独立 retry 子 Run，历史可读 |
| G16 | T09 | 分后端离线 fixture/SDK/子进程证据；**真实小任务 live 尚未验收** |
| G17/G18 | T10 | @2 consumer 与交互 UI、明确 ack 语义、冻结人工干预；真实模型审批/取消待验 |
| Inspect 扩展 | T11 | 官方 v2 只读导入、平台查询、native 评分与幂等；不支持执行或二进制 .eval |

| 验收 | 主要测试入口 | 尚需独立证据 |
|---|---|---|
| A01 | runtime preflight/gate、native_configuration | 实际认证成功/失败 live |
| A02/A03 | pi_real_adapter、pi_run_backend | 外部模型小任务 |
| A04 | native_configuration | 原生加载清单保持 partial，不冒充完全可复现 |
| A05/A06 | supervised_process、supervisor_safety | POSIX 已纳入最终 Python 全量，完整 CI 通过 |
| A07/A10 | batch_parser_boundaries、cli_run_backend、pi_http_provider | 真实 CLI 流样本 |
| A08/A09 | supervisor_safety、session_concurrency、runtime_recovery、appserver_worker | POSIX/PG 已测；真实模型取消待验 |
| A11/A12 | interactive_commands、command_delivery、intervention_comparison、RuntimeCommands | 真实模型审批/人工干预 |
| A13 | native_configuration、appserver_worker、Pi sandbox tests | 原生工具限制不提升为平台强制 |
| A14 | inspect_v2_identity、parser/raw evidence 与评分历史 | 已取得 Inspect 0.3.266 原生 runner 日志（官方 mock 模型）；原文导入/幂等/零执行收据通过，不算模型 live |
| A15 | external_runtime_slice、runtime registry tests | 无新增外部前置 |

## 本轮实际验证

环境：Windows；项目 uv Python 3.12、Node 24、pnpm 9.15。每行是对应执行时的快照，不能相加当作唯一测试数量，也不代表之后所有修改已经重新验证。

| 命令/范围 | 实际结果 |
|---|---|
| Web 全量 test（2026-09-21） | 17 文件、239 测试通过 |
| Web build | 通过；保留既有 bundle 大小提示 |
| ruff check . | 本地及最终 Linux CI 通过 |
| mypy packages/contracts | 26 源文件通过 |
| make openapi | 已导出 API 并重生成 TS |
| 固定 Codex 原生零模型探针 | 0.155.1 initialize + initialized + thread/start 成功；返回 readOnly/networkAccess=false/on-request。空凭据、未发送 turn/start；关闭后 residual_pids=[]、truncated=false。只证明协议握手，不算 live 小任务 |
| Pi integration（停止失败/attempt 身份修复后） | 11 通过 |
| native configuration（隔离/脱敏/惰性 Git/扫描上限） | 11 通过；F5 最终独立复审通过 |
| native + CLI backend（前一快照） | 13 通过 |
| parser/capture/Inspect 定向回归 | 51 通过 |
| Task3 独立复审 | 49 通过 |
| app-server 实现者定向集 | 109 通过、5 PG 跳过；追加 session 脱敏 5 项通过；三个 P1 及最后增量均独立复审通过 |
| Windows make check | exit 2；283 failed / 1639 passed / 52 skipped / 1 deselected，323.88 秒。失败用例集合与本轮初始修复快照完全一致；不能写作全绿。最后 Git filter/不完整配置反例另以 11 项定向集补验 |
| Linux/PG 首次完整 make check | CI 35554874385：1943 passed / 29 skipped / 1 deselected，4 failed / 11 errors。迁移清理接口、固定历史 HEAD 测试、schema 换行哈希、禁止写入证据聚合/旧预期已定位修复，需重跑确认；该次不是全绿 |
| 首次 CI 修复定向验证 | 禁止写入、schema、CLI catalog：19 passed；迁移与存储相关：28 passed / 17 skipped（本机没有 PG）；ruff check . 通过。PG 与 Linux 最终结果以下次 CI 为准 |
| Linux/PG 第二轮 CI（af2adc4） | 35555547703：Python 1962 passed / 29 skipped / 1 deselected，340.31 秒；独立 Web job 通过。make check 后续 Web 测试有 1 条异步时序失败，等待 Trial 按钮的修复后本机 Web 239 passed / build 通过；完整 gate 需重跑 |
| Inspect 固定版真实原生捕获 | inspect-ai 0.3.266，EvalLog v2、2 samples、24546 bytes，SHA256 9dec3bb4c91175688bd5f93e066232da81cfdc321999a6753c2febb756479aea。真实 runner + 官方 mockllm；SQLite 导入 completed、原字节冻结、同 Run/pass 幂等、0 CaseAttempt。零外部模型请求 |
| live 执行卡零调用复核 | 独立 reviewer 用真实 API/临时 SQLite 验证导入 201、四后端请求 202 queued、13 组预算通过；禁止 spawn、未启动 Worker、0 CaseAttempt。只证明请求可执行，不证明 live 通过 |
| **最终完整 CI（270bfad）** | [35556224363](https://github.com/BaiZhi967/MoTTEavl/actions/runs/35556224363) 全绿：Linux/Python 3.12/PostgreSQL 16，Python **1963 passed / 29 skipped / 1 deselected**（224.28 秒）；Web **17 文件 / 239 passed**。make check、构建、Pi bridge 测试、Compose config、pip-audit、Web audit、Trivy 配置扫描及 OpenAPI/TS 漂移门均通过；skip 和 live 不算通过 |
| 真实模型 live | 未执行；未获得本次具体 runtime/model/凭据引用与费用上限 |

独立审查涵盖 Pi、公共契约/比较、Harness、产品、Inspect、命令 UI 和 M5 计划。最后一轮额外发现 Pi 未确认停止、parser lifecycle/类型、未知计量、native config/auth、app-server 事件身份/过期提案/错误脱敏，均要求反例验证后复审。最终报告须更新新发现的关闭情况，不以早期通过覆盖新发现。

## 合入条件与未完成项

1. **已满足**：最终独立复审关闭本轮全部阻断发现；CI 修复、异步 Web 测试和原生 Inspect 收据增量亦独立复审通过。
2. **已满足**：Linux + PostgreSQL CI 的完整 make check、audit、生成契约检查通过；本机 Windows 的失败记录仍保留，不用 Linux 结果冒充 Windows 全绿。
3. G16/T09 真实小任务需明确后端、模型、凭据引用与调用/费用上限，分别记录 Pi/Claude/Codex 及 app-server 的 live 来源。不得读取或使用操作者已有登录态推定授权。
4. **已按最新指令调整并执行**：用户后续明确要求“先合并到主干并推送”，已于上述合入点完成。该指令覆盖此前“全部验收后再合入”的顺序约束；不据此宣称真实模型验收通过。M5 基线已登记，可开展开发。

真实模型执行范围、参数、收据字段和待补配置见 [M4 live 验收执行卡](../operations/m4-live-acceptance.md)。该卡是可审阅的执行方案，不是调用授权或验收通过记录。

本轮已提交并推送开发分支：`aa63cf2`（M4 修复）、`1389fa0`（M5 计划）、`543614e`（同步 main）、`78f372a`（修复主干测试对开发机目录的依赖，27 项通过）。这不是向 main 合入。首次 Linux/PG 完整 CI：[35554874385](https://github.com/BaiZhi967/MoTTEavl/actions/runs/35554874385)，Web 通过、Python make check 失败，以上如实记录。

后续 `af2adc4` 修复首轮 CI 的迁移/证据/哈希及 CLI catalog 版本问题；`270bfad` 补原生 Inspect 收据、修复 Web 测试等待条件并移除未使用的过期兼容版本常量。最终通过记录对应 `270bfad`；之后的验收状态和 M5 交接文档更新不修改产品逻辑。原始采集脚本保留 CRLF 字节，Git whitespace 规则单独标记 `cr-at-eol`，不改写来源以消除格式提示。

固定 Windows x64 二进制静态核对收据：Claude 2.1.278 SHA256 `006ea5c8638f67f10a5ae66bb232fd267c9f6af294e3f03f4cfcf1fd3f2cced8`；Codex 0.155.1 SHA256 `eba0f32c976667cb9298efafd98513e823eeda7b576a03ec658bb8be8d336316`。均安装于忽略的专项工具目录，未替换主机已有 CLI。

回退关闭新 Runtime 创建/consumer，不覆盖既有版本、命令审计、冻结证据或评分历史。停止不明的进程保留现场，不删工作区掩盖失败。
