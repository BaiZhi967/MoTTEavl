# MoTTEavl 支持矩阵（M8 当前软件证据）

当前结论只取自 [M8唯一索引](../verification/m8-evidence-index.json) 与其绑定的
[软件执行收据](../verification/m8-software-execution-2026-09-30.json)，
[修复报告](../verification/M8-repair-2026-09-30.md)说明G01–G12及原T/A验收范围。
下文M7与2026-09-23记录均为历史，不能替代当前候选。

实现状态和证据层是两个轴。tested只说明指定源码/参数/环境实际执行；supported是另需满足的支持承诺；
experimental、blocked、not_run各有自身含义，不按线性等级排名。skip不计入通过或支持。
137个原目标未被改写，当前窄软件映射不授予整体验收；`stable_supported=false`、`cutover_ready=false`。

## M8 软件基线与后续接入证据

本地47dec4f（tree167a52409c05959a1028e407abe255a90121e615）make check退出0：
Python5972 passed /60 skipped /1 live deselected，Web304 passed；PG16.15实际选中节点已运行。
远端同tree源码ae69的[CI run36746343605](https://github.com/BaiZhi967/MoTTEavl/actions/runs/36746343605)
截至17:35 UTC该已观测代码候选五项CI全部success：Windows83、Compose实际演练/清理且零Provider、
packaging、Web304与Python。CI Python5948 passed /84 skipped /1 live deselected /3 warnings（2668.22s），
额外PG分配/恢复2项及Trace事务114项通过，重叠计数不累加。本地与CI结果分别绑定环境；
后续文档HEAD的CI仍需单独核验，本次成功不自动继承。

| 范围 | 当前实现 | 已有软件证据及未验收边界 |
|---|---|---|
| 固定Pass比较 / 未知成本 | 受限实现已修复 | local/server CLI及SDK/API冻结引用；无币种保持unknown；真实UI未复验 |
| preview / 批量冻结 / 幂等恢复 | 受限实现已修复 | Memory/SQLite与PG参数组通过；无hash仅create_revalidated |
| StatisticalReport | 已实施独立不可变发布 | Memory/SQLite/PG与SDK/API/CLI、JSON/JUnit、GC/backup保护；不证明样本充足或live Trial独立 |
| Judge公共校准 / pairwise Gate | 受限持久实现 | 实际模型身份、冻结alias、no-rebill、角色及完整指标、两套显式Gate的软件路径；真人与真实Judge准确性未验收 |
| Trace retention / 历史读取 | 已实施，默认关闭 | 持久归档先于receipt+trim；SQLite/PG选中路径与历史partial/gap；Memory不apply，Windows Trace apply不支持 |
| Direct/GSM/native Agent/Scenario/Skill | 受限实验装配已实施 | 冻结输入、真实Dispatcher/Worker配脚本模型；Skill仅builtin context，native/executable Experiment拒绝 |
| C-Eval固定OC0.4.2 | 受限Experiment已实施 | 真实安装Runner与localhost HTTP；官方数据和真实模型成绩未验收 |
| Harbor / opaque外部Runtime Experiment | fail-closed | 未证明完整发送上界的profile拒绝；没有通用Harbor assembler或真实Agent支持结论 |
| GC / rollback / backup | 引用与屏障修复已实施 | 真实PG并发/在途快照/恢复与SQLite反例通过；真实私有导出和生产恢复未验收 |
| Windows普通工件 / 已有receipt归档保护 | 已实施，聚焦CI通过 | 83 passed、0 skipped；晚到无receipt归档别名限制保留；不代表完整Windows/Trace apply支持 |
| Compose | CI隔离软件演练通过 | 实际build/up、迁移/health/Worker、重启持久性、镜像hash、cleanup；零Provider；不等于生产验证 |
| 发布依赖 / six wheels | 当前主工作区审计及安装通过 | urllib3 2.8.0，其余100锁定包未改；183源码条目一致，隔离Python -I真实HTTP CLI通过；严格TS另有49条相同既有诊断 |
| 浏览器G04/T11/A18 | not_reverified | 当前build与HTTP10资产一致；浏览器/预览阻断，无UI、窄屏、>500事件或鉴权过期证据 |
| 真实支持组合 / 私有迁移 / 三个替代场景 / RC与cutover | 未闭合 | 新付费、人审、私有来源、完整平台、正式发布与生产授权及验收仍需补齐 |

## a400 精确源码回归与 Go 功能验收补充（2026-09-30 21:22 UTC）

`a400aef8d6d81a2d09792e92d049e0853717f5de` 与本地 `2373820` 的完整树为
`809793f16bf141664753870a584b1a15c0a64598`。
[PR CI 36773403850](https://github.com/BaiZhi967/MoTTEavl/actions/runs/36773403850)
及 [push CI 36773398634](https://github.com/BaiZhi967/MoTTEavl/actions/runs/36773398634)
各五项全部成功：每轮 Python 6131 passed / 84 skipped / 1 deselected / 3 warnings，
另有 PostgreSQL 分配/恢复 2 项及 Trace 事务 114 项；Web 304、Windows 83、
真实 Compose 镜像/迁移/Worker/重启/清理、仓库外六包干净安装通过。
这些数量存在重叠，不累加为覆盖目标数；跳过不计通过。
这是 a400 的已执行结果，不证明后续 Go 验收脚本变更已通过其各自最终 CI。

当前 Go 限定编码功能的逐项状态和原始失败历史见
[功能矩阵](../verification/opencode-go-feature-matrix.md)及
[真实调用证据](../verification/opencode-go-live-2026-09-30/summary.json)。
它们补充原生 Agent、Scenario、Skill 和 Judge 的受限真实模型路径，
不覆盖本文件表格中注明的正式人审资格、非编码数据集或其他 Provider/Runtime。
第 8 节原始 Task 4 表中的“本轮没有新增真实模型证据”仅描述当时范围，
不能用于否认这些后续单独绑定源码的编码证据。
功能路径能够执行并正确拒绝不稳定结果，不等于选定模型获得校准资格；
当前模型仍未取得正式资格，必须另有真实、合规复核的样本和资格证据。

浏览器验收仍未完成：旧 `47dec4f` 构建和 HTTP 资产校验不能证明当前 UI。
云浏览器本地导航被策略阻止，当前环境没有受支持的预览入口；没有绕过访问限制。
`stable_supported=false`、`cutover_ready=false` 继续保留。
CI 通过说明该限定增量的回归门禁通过，不把生产/完整 M8 验收自动标记完成。

## 历史 M7 记录（以下不是当前候选结论）

## 1. 操作系统 / 运行方式

| 环境 | 状态 | 证据 |
|---|---|---|
| Windows 11 + Git Bash（开发/离线测试） | tested | docs/verification/M7.md 命令账本（本机全量离线门禁） |
| Linux（CI ubuntu-latest + 真实 PG service） | tested | GitHub Actions（合并主干后 CI run 链接，见 release notes） |
| WSL2 | not_run | 无环境（README 声明为支持路径，未单独验证） |
| macOS | not_run | 无环境 |

## 2. 存储

| 后端 | 状态 | 证据 |
|---|---|---|
| SQLite（本地开发/单机） | tested | tests/storage/**、M7 平台表升级、备份/恢复集成测试 |
| PostgreSQL 16（CI service） | tested（CI） | CI 真实 PG 参数组（合并后回填 run 链接） |
| PostgreSQL（本机直连） | blocked | 本机无 docker/MOTTE_PG_DSN（docs/verification/M7.md §1） |

## 3. 执行后端 / Runtime

| 后端 | 状态 | 证据 |
|---|---|---|
| replay@1 / json_extract@1（确定性 fixture） | tested | 全量离线套件（零网络零费用） |
| direct-llm@1（含 v2 scorer） | tested | tests/api/test_direct_llm_api.py 等 |
| builtin-agent@1 | tested（离线） | tests/sdk/test_agent_task_scoring_gaps.py 等 |
| external-benchmark@1（C-Eval/CMMLU） | experimental | 离线 fixture 完整；真实外部执行未在 M7 复验 |
| terminal-bench@1（Harbor） | blocked（本机）/ experimental | 本机无 docker；离线 Harbor 测试全绿 |
| scenario/skill（M5） | tested（离线） | tests/scenario/**、tests/skill/** |

## 4. Provider / 模型 / Judge

| 项 | 状态 | 证据 |
|---|---|---|
| OpenAI-compatible / Responses / Anthropic 适配器 | tested（离线 fixture） | tests/provider/** |
| DeepSeek V4.1 Flash live | tested（M6 有界验收） | docs/verification/M6.md §5.1；M7 live smoke 见命令账本 |
| Judge（独立评分） | experimental | 无 ≥30 人工校准样本（M5 起已知边界，正式门禁 fail-closed） |

## 5. 操作能力（M7 验收面）

| 操作 | 状态 | 证据 |
|---|---|---|
| SDK 安装（clean venv/wheel） | tested | tests/packaging/test_clean_install.py 8 passed（e4d54bd，主审复跑同结果；CI packaging job 合并后补链接） |
| SDK 调用（Run/事件/报告/比较/Gate/导出） | tested | tests/sdk/test_client_contract.py + test_wait_and_events.py 27 passed（8389659/1db0a54，主审复跑同结果；内存幂等修复 133a2d5 附回归测试） |
| CLI local/server 双模式 | tested | tests/cli 97 passed 含 test_remote_parity.py 18 项（c4da983，主审复跑同结果；A04 断连零本地副作用）。tests/cli/test_terminalbench_cli.py 2 项为基线同样失败的 Windows O_DIRECTORY 环境族，非 M7 引入 |
| pytest 门禁读取 | tested | tests/sdk/test_pytest_and_exports.py 26 passed（287a3ca，主审复跑同结果；普通 pytest 零模型/零 Run 有网络 monkeypatch 反证） |
| 历史导入 dry-run/apply/resume/rollback（合成来源） | tested | tests/migration 19 passed/1 Windows-symlink skip（148e26a，主审复跑同结果）；真实旧导出 not_run |
| 备份/恢复（SQLite） | tested | tests/integration/test_backup_restore_consistency.py + tests/storage/test_maintenance.py 14 passed/1 PG skip（ec9be0a，主审复跑同结果） |
| 备份/恢复（PostgreSQL） | not_run | 无本机 PG；CI 未见 pg_dump 断言（如实登记） |
| GC/retention | tested | tests/security/test_gc_retention.py 4 passed（8c8cf3d）；trace DB 行裁剪 not_implemented（无事件时间戳，plan 如实报告） |
| 升级/回退演练（SQLite） | tested | tests/integration/test_release_smoke.py 4 passed（含旧形状升级、备份恢复路径、冻结退出码）；PG 降级阻断 not_run（无 DSN） |
| Docker Compose build/up | blocked | 本机无 docker；仅 compose config 通过 |
| 旧平台切换 | not_run | 需单独授权（docs/release/cutover.md） |

## 6. 阶段结论规则

以上是M7时点的证据账本，不产生当前发布结论。tested表示限定执行事实，supported还需对应支持承诺及原验收门槛；二者不是线性等级，不能由测试数量相互转换。

## 7. 历史 M8 候选增量（2026-09-23，非当前结论）

| 能力 / 环境 | 本候选状态 | 证据或缺口 |
|---|---|---|
| Direct/GSM8K 固定 Pass 比较 | offline_verified + live_scoped | M8 T04 的 HTTP 与 Web 测试；付费 Direct 单 Case 双 Run 的固定 Pass/CNY 成本比较通过。截图未复验，其他输入变体未获 live 覆盖 |
| Direct/GSM8K/Agent Tasks Experiment | offline_partial + live_scoped | M8 T02/T03/T05；付费 Direct 和单个合成 GSM8K Case 经隔离 Worker 完成；Agent Tasks 仅离线，C-Eval、Scenario/Skill、Harbor 未接入。GSM8K 非 1024 输出上限在 preview/create 一致拒绝 |
| 固定 Pass Case/Task 与 Terminal Trial 统计 | offline_partial + live_scoped | M8 T06 SDK/HTTP/CLI JSON/Web 消费固定引用、政策、`k`、缺失资格和计划 Trial pass@k；付费 Direct 单 Case 双 Run 的统计明确 `insufficient_tasks`。另行持久发布的统计报告与 live Trial 独立性仍未闭合 |
| PostgreSQL 隔离迁移与实验分配 | integration_scoped | 原 T01 migration/owner CI `f5c4374` / `35815300827`；M8 T03 两进程同 key、单 initial Run 在 task-owned PG 库通过两次（`ac05062` / `35829305820`, `35829308936`）。本机无 DSN |
| PostgreSQL dump/restore（一次性 staging） | integration_scoped | M8 T09 `ac05062` 手工、`e96407e` guarded helper 各两次 `-vv` 节点通过；空目标、Run/TrialPlan/Pass/Baseline/Artifact/guard 对账，损坏 dump 离线拒绝。生产恢复、在途写入与 GC/Compose 仍未演练 |
| Docker Compose build/up | blocked_local | 本机无 Docker；Linux CI 仅 compose config，通过不代表 build/up/health/Worker 演练 |
| 配置网关的 DeepSeek 单轮 Provider | live_scoped | 用户授权下 6 次真实 HTTP 尝试、0 重试；5 个隔离合成 Run 完成，显式 CNY 回执的双 Run 比较质量/成本合格。网关上游模型版本和实际账单未独立核验；见 M8 付费收据 |
| 其他真实 Harness/Judge 与生产切换 | external_pending | C-Eval/Harbor/native Agent/Scenario/Skill/Judge 尚无本轮 live 收据；真实标签、环境和单独执行边界仍缺，未发布或切换 |

## 8. 当前受限实验范围附注（2026-09-30）

本节与当前索引的 suite_assemblers 范围一致，不升级整个 M8 的发布状态。
“代码拒绝”“离线证明”“真实 Runner + 合成服务”“真实模型/官方数据验收”分列；
前一层不能代替后一层。以下所有 Task 4 新测试都没有执行模型、CLI Agent 或容器。

| Profile / 路径 | Experiment 准入 | 离线证据 | 实际 Runner / 模型证据 |
|---|---|---|---|
| native `builtin-agent@1`，legacy-json / native-tool | 保留既有原生路径；Case × 累积 model steps × 显式 Provider attempts | `test_m8_native_agent_experiments.py`；`test_m8_experiment_retry.py` 的真实 Worker + scripted Provider | 本轮没有新增真实模型证据；外部 Runtime 不借用此公式 |
| typed Scenario / Skill，固定 Workflow + builtin Target | 受限准入，复用 standalone builder 和冻结 Cell | `test_experiment_scenario_skill.py`；不允许 Runtime 因素或未证明 Target | scripted Provider/Job 生命周期不是 CLI Agent 验收 |
| `motte-ceval-oc042-bounded@1`，Linux x86_64 / Python 3.10.20 / 147-pin lock | 准入，固定单模型/worker/partition/batch，N × 2 客户端 sends / Run | `test_experiment_ceval.py` 的精确 revision、冻结重放、错误计数、无隐藏 retry/redirect、身份漂移拒绝 | 当前47dec4f执行收据中的固定 OpenCompass0.4.2 CLI + localhost合成服务通过选中节点；f4deeca仅为历史实现来源；[执行条件](../operations/ceval.md#6-实验矩阵的固定发送上界m8-task-3)。无官方数据/真实模型成绩 |
| 旧 C-Eval / CMMLU standalone OpenCompass profile | 不能获得 Experiment 硬预算；旧 standalone 合约保留 | 旧 profile/未知 profile 在 preview/create/retry 拒绝；无 profile 的 proxy/redirect 兼容性已回归 | Task 3 的旧 standalone 合成 runner 测试只证明兼容；旧上游错误循环没有调用上界 |
| Harbor 0.23.0 + Claude Code | 拒绝：`SUITE_UNSUPPORTED`；伪装为 native 的冻结输入为 `EXPERIMENT_BUDGET_UNPROVABLE` | `test_experiment_external_budget_guards.py`：max_turns、max_budget_usd、零 runner retry 都不构成 transport 证明 | 本轮真实 Harbor/Claude Agent 验收 `not_run`；CLI 内重试、fallback/helper 请求仍未被平台计数 |
| Harbor Task × Trial / oracle | 拒绝，Trial 计划或 oracle 标签不等于模型 sends | 同上，Task=1、Trial=1 及 oracle 均拒绝；standalone fixture 的 Trial refreeze 保留 | oracle fixture 只证明确定性脚本/导入路径；任意任务脚本不能宣称零模型调用，更不是实际 Agent 支持 |
| `claude-cli@1` / `codex-cli@1` | 拒绝 Runtime 因素：`FACTOR_UNSUPPORTED`；存量外部执行输入在 allocate/retry 先拒绝 | timeout、max_turns 与外部预算声明不升级为模型请求上界 | 本轮实际 CLI/model `not_run`；固定 CLI 版本本身不是 transport limiter |
| `pi-agent@1` / 未知 opaque Runtime | 同上；不接受 caller 的 retries=0 或 streamFn 次数证明 | 累积 streamFn 上限只覆盖外层调用；内部 SDK retry、redirect、operation 重建仍需完整证明 | 本轮实际 Pi/provider `not_run`；不把某个 SDK 的 fetch 次数推成所有 provider 的 transport 上界 |

`tests/sdk/test_experiment_external_budget_guards.py` 逐 profile 覆盖 preview/create
零 Spec/Cell/Run/Job 写入；allocate/retry 还检查历史无 `prepared_run` 的原 Run 和
已冻结 Cell 两份执行输入。只要出现未证明 Runtime/Harbor，便在整个恢复批次 claim、
Trial refreeze、创建新 Run 或启动 Job 之前拒绝；不读取当前资源来替换历史配置。
无关 native Cell 不得先分配后才发现另一 Cell 是外部 Runtime。
这些拒绝并未关闭独立 standalone Runtime/Harbor 功能，也不把历史 native Cell
补写成更强的冻结/审计保证。调用方不能提交任意 manifest、call_bound、retry_policy
或执行证明来绕过窄 DTO；scalar timeout/turn/stream 条件也不能被静默忽略。

### 未来接入口（保留接口，不注册新正向 profile）

- `motte_sdk.terminalbench.build_run_inputs(record, run_id, job_id, profile, task_keys, work_root)`
  继续负责从准备好的固定 revision 冻结任务内容、选择、Profile、Trial 计划和原生配置；
  它是内部 standalone builder，不是 Experiment 的任意 manifest 输入通道
- `refreeze_for_run(manifest, run_id, job_id)` 继续只重建新 Run/Job/Trial 身份，保留任务
  内容和受控条件；已有 Job 恢复只能观察，显式 retry 才能创建独立新身份
- 将来只有内部注册、源码/依赖身份固定且实际执行验证的窄 profile 才能接入 assembler：
  必须控制所有 transport/SDK/CLI/runner 重试、redirect、fallback/helper、并发/进程
  fanout 与恢复后的 allowance。不能用用户自报 cap、总耗时、费用或 Trial 数代替
- `tests/integration/test_harbor_round2_service.py::test_retry_refreezes_child_trial_and_job_identity`
  保留该 standalone 接口的离线回归，证明父子 Trial/Job 身份独立；它不授权 Harbor
  Experiment 正路径，也不证明真实 Agent 的模型调用预算

初始 factor × repeat 矩阵只对初始 Runs 求和。显式 retry 的新 Run 单独计其已证明
上界；不能声称初始预算约束无限未来 retries 的终身累计请求或费用。

上游缺口依据固定源：Harbor
[`TrialQueue`](https://github.com/harbor-framework/harbor/blob/1e5c5c6db929a10a140d05e606882c671ae20729/src/harbor/trial/queue.py)、
[`Claude Code`](https://github.com/harbor-framework/harbor/blob/1e5c5c6db929a10a140d05e606882c671ae20729/src/harbor/agents/installed/claude_code.py)、
[`oracle`](https://github.com/harbor-framework/harbor/blob/1e5c5c6db929a10a140d05e606882c671ae20729/src/harbor/agents/oracle.py)；
Pi 桥接 `session.mjs` 与仓库锁定的 Pi AI 0.73.1 内层 OpenAI 6.26.0 / Anthropic 0.91.1
重试边界。Harbor 外层 Trial retry=0 不会禁用 CLI 内层请求，Pi 外层 streamFn 计数
也不会自动计入 SDK 内部 retries。
