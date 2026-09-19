# M4：真实 Pi 与外部 Harness 详细规划

> 状态：待实施。Pi、Claude、Codex 分别验收，不以安装成功或协议探测通过代表执行支持。实施按工作包测试先行、独立提交和评审，可使用 superpowers:subagent-driven-development 或 superpowers:executing-plans。

**Goal：** 把同一受控任务交给真实 Pi、Claude CLI、Codex CLI，统一保存实际配置、可观察行为、产物、评分和取消结果，并明确各系统的可控与不可观察部分。  
**Architecture：** Runtime/Harness 只产生标准事件和 AgentResult；RunDispatcher、CaseAttempt、Artifact、ScoringPass 保持平台主权。先 batch 后 interactive，沿用已加固的 Pi bridge 和受控进程执行。  
**Tech Stack：** Python Worker +现有 Node bridge、受控 CLI 子进程、固定的上游包/二进制；React/Vite 控制台。  
**Spec：** [总路线](../ROADMAP.md)第 9 节、[共通约束](README.md)、[M1](M1-native-agent-and-evaluation.md)。  
**代码基线：** `a668d13ee5ea0c3613648f8992ecaa4a148d3855`；上游在线文档核对日 2026-09-19，不能替代实施时的版本锁。

## 1. 进入条件与交付分层

硬依赖是 M1 的 AgentResult/Observation、工具策略、Artifact、评分和取消边界。M2 Job supervisor 可以复用，但不要求 Harbor 或 C-Eval 全部完成。已有 `motte_agent/pi.py`、`bridges/pi/` 和 `motte_harness/{claude,codex,process,protocol,install}.py` 是本阶段接线起点。[B1][B2]

当前兼容说明明确 Pi 是不可执行的协议桩，CLI 是本机 probe，RunCommand 尚无执行消费者。[B3] 本阶段不保留“echo 成功”作为 live fallback。

| 子阶段 | 必须交付 | 退出条件 |
|---|---|---|
| M4-A | 真实 Pi session、模型/工具路径与 batch Backend | 可执行实际任务，轨迹/产物/取消/评分闭环 |
| M4-B | Claude 与 Codex 各一个 batch transport | 两个后端分别通过固定版本的 fixture、集成和 live 小任务 |
| M4-C（增强） | 一个交互式 backend 的 command consumer | 消息/审批/取消有真实 ack 和恢复语义；不阻塞 A/B 首批交付 |
| M4-D（扩展） | Inspect 原始日志只读导入 | 有明确实际导入需求时实施，不作为 A/B 退出门 |

非目标：完整远程终端、任意 CLI 适配、用户账户代登录、全自动安装插件、复刻外部 Agent 内核、把所有原生工具重写成平台工具，以及因外部能力不足而默认绕过审批或沙箱。

## 2. 最终具体目标清单

- [ ] M4-G01：RuntimeDefinition/Version 固定实现、协议、配置和能力，不以字符串名称猜测支持。
- [ ] M4-G02：三种状态 installed/protocol_ready/execution_ready 分别显示，包含原因。
- [ ] M4-G03：真实 Pi 使用已核对的上游包与版本；bridge 不移植上游 Agent 源码。
- [ ] M4-G04：每个 Case 使用独立 session/workspace；显式续跑才允许绑定旧 session。
- [ ] M4-G05：Pi 的模型调用路径和工具权限可解释，不绕过声明的 Provider 策略。
- [ ] M4-G06：CLI 固定实际 transport、二进制版本、原生设置和工作区来源。
- [ ] M4-G07：宿主自动发现的规则、Skill、MCP、插件和配置不会无记录改变实验。
- [ ] M4-G08：结构化事件保留 source_id/seq、parser_version 和完整度，未知事件不变成假成功。
- [ ] M4-G09：标准输出/错误输出并行消费，有单行/总量/时间限制，不出现管道死锁。
- [ ] M4-G10：取消、超时、进程树终止和背景任务残留都有实际测试。
- [ ] M4-G11：无终态事件、启动不确定、可能发生副作用时不自动重放任务。
- [ ] M4-G12：Artifact 与评分沿用 M1；进程退出 0 不自动表示质量通过。
- [ ] M4-G13：实际模型、工具、usage/cost 不可观察时显示 unknown，支持策略可拒绝运行。
- [ ] M4-G14：不同 Harness 的模型可控度与运行约束进入可比性理由。
- [ ] M4-G15：API/CLI/Web 可配置、预检、运行、查看证据与 retry 子 Run。
- [ ] M4-G16：每个正式支持的后端保留离线协议 fixture 和真实小任务记录。
- [ ] M4-G17：交互增强只有消费者接通后开放按钮，消息不再有虚假的 accepted。
- [ ] M4-G18：人工干预、审批与补充提示进入审计并改变比较条件。

## 3. 模块与实现范围

| 模块 | 现有触点 / 拟新增文件 | 输入 / 输出 | 本阶段责任 |
|---|---|---|---|
| Runtime 资源契约 | `packages/contracts/motte_contracts/`；新增 `runtime.py` | RuntimeDefinition/Version、capabilities、运行配置 | 固定 subject_kind、model_control、transport 与权限声明 |
| Backend 接线 | `motte_sdk/execution_backends.py`；新增 `runtime_backends.py` | manifest → Pi/CLI handle | 各 backend 的不同必需字段校验，禁止隐式降级 Direct |
| Pi Python 侧 | `packages/agent-runtime/motte_agent/pi.py`、`protocol.py` | 平台命令/事件 ↔严格 bridge 消息 | 握手、事件转换、工具与取消桥接、现有硬化回归 |
| Pi Node 侧 | `bridges/pi/bridge.mjs`、workspace manifest/lock | 原生 session ↔ JSONL v1/后续显式版本 | 真 SDK、模型和工具注入、session 生命周期，不写平台 DB |
| CLI supervisor | `packages/harness-runtime/motte_harness/process.py`；新增 `session.py` | argv/env/cwd →受控进程与事件 | 进程树、管道、输出配额、截止时间、终态判定 |
| Claude adapter | `motte_harness/claude.py`；新增 `parsers/claude.py` | 固定 native config → Observation 来源事件 | -p/结构化结果与可选 stream-json 的所选版本兼容 |
| Codex adapter | `motte_harness/codex.py`；新增 `parsers/codex.py` | batch 事件或后续 RPC →统一事件 | 首版 batch；app-server 单独 transport/version，禁止无记录切换 |
| 能力探测 | `motte_harness/install.py`、`probe.py`、API 目录 | 本机可用条件 → ProbeResult | 静态 probe 不调用付费模型；live test 是显式操作 |
| 命令通道 | 现有 RunCommand repository；新增 `motte_sdk/commands.py` | Command →投递状态/ack | 仅 M4-C 实施，复用已有存储而不新建另一条消息系统 |
| UI | 现有 Agent/Harness 目录；新增 `apps/web/src/evalTypes/harness/` | API 状态/证据 →工作区 | 安装与执行分离、session、限制、产物和评分历史 |

## 4. Runtime 与模型控制契约

RuntimeDefinition 固定 id、version、kind、transport、upstream_version、adapter/parser version、config_schema、supported_modes、interactive、tool_control、model_control、evidence_capabilities。RuntimeProfile 引用它并固定 native settings、workspace、工具与网络政策、预算和凭据引用。

model_control 为 `platform-controlled/runner-configured/externally-managed`。第一类必须解析发布 ModelProfile 并通过现有 Provider；第二类固定 CLI 模型配置并核对实际回报；第三类只能评价整个系统，严格 require_match 无足够证据时拒绝或按既定政策给证据不足。

不同 backend 的必需字段不同：CLI 使用原生认证时不强制伪造 provider.kind；但必须有 RuntimeProfile 和认证可用性证据。禁止为了让 CLI 能创建 Run 而全面关闭模型/凭据验证。协议能力声明不能授予工具权限。

## 5. Pi 接入详细范围

### 5.1 上游选择与版本

先完成包名、导出接口、许可证、Node 版本和 SDK 示例核验，将所用 commit/package version 固定在 lockfile 与兼容矩阵。核对时 `badlogic/pi-mono` 页面已重定向到 `earendil-works/pi`；因此不使用搜索到的第三方 fork 作为长期 SDK 权威来源。[U1]

选择能提供 Agent loop、事件、工具和模型注入的最小上游层。完整 coding-agent 的文件发现、默认插件与会话能力只有在确实需要并能固定时启用。Pi 不接通时保持 execution_ready=false，不临时回退到 Builtin Agent。

### 5.2 模型与工具路径

优先使用上游公开注入点连接平台 Provider/工具边界；Pi 自己持有原生 Provider 时明确改为 runner-configured，并验证固定参数/模型与预算，不能声称仍由平台统一拦截全部请求。

平台工具请求通过 call_id 对应 ToolRegistry/Sandbox，schema、权限、参数、返回值与错误均可见。使用 Pi 原生工具时记录原生权限和 enforcement_owner，不假装受到平台 ToolRegistry 的逐调用强制控制。

### 5.3 Bridge 协议

保留 protocol/version/run_id/seq/type/payload；定义 session_id、case_id、operation_id 关联。probe 明确返回真实包版本和能力。严格校验长度、帧边界、UTF-8、序列与事件归属；stdout 只放协议，诊断写 stderr 并脱敏。

覆盖 init/ready、run、model/tool events、final/error、interrupt/ack。未识别的扩展事件保留受限原文引用并降低 coverage；未识别的关键控制消息显式拒绝。断连后不能自动重发已经可能执行的 run/tool 请求。

## 6. Claude/Codex 批处理范围

### 6.1 原生语义与配置固定

Claude 官方支持非交互调用与 JSON/stream-json；Codex 官方支持 exec 的 JSONL，而 app-server 用于更深入的会话/审批集成。[U2][U3][U4] 首版分别选择一个固定 batch transport；所需 flag、事件结构和可用能力通过本机版本 probe 和 fixture 验证，不只按在线文档推断。

ConfigSnapshot 应记录工作目录、Git revision/dirty patch hash、原生配置文件清单与非秘密 hash、system/prompt 附加项、模型/推理设置、加载规则/Skill/MCP/插件、环境 allowlist、工具和网络限制、审批模式、transport 和 parser version。

默认使用干净受控配置，或固定列出加载的宿主资源。无法禁用自动发现且不能获得加载清单时，配置可复现性标为 partial。不得默默使用操作者 home 下的历史 session、全局规则或 MCP。

### 6.2 Supervisor 与结果采集

同时读取 stdout/stderr，限制单行、累计字节、空闲超时和总时长。以结构化 terminal event 加退出状态综合判定；只有文本/退出 0 而缺关键结果时标证据不完整。重复 final、乱序事件、残缺 JSON 和未知 schema 有明确政策。

产物在受控工作区收集；记录输入快照和实际修改，不扫描整个磁盘。模型最终说“完成”不能替代文件/测试断言。

取消先写审计，再请求原生中断；超出宽限期后终止受控进程树。CLI 的后台工具可能比最终输出活得更久，必须验证所有受控子进程和文件写入都已停止，再冻结最终 Artifact。无法确认时记录残留并 needs_review。

### 6.3 费用与隐私

原生 reported cost、平台 PriceTable 估算、实际账单不混为一个字段。只记录已有证据，缺计量时保留 unknown。上游原生重试可能产生额外请求，平台不能在外围再自动完整重跑。

不自动登录，不读取或提交整个 auth 文件；只在明确配置的受控进程范围提供所需凭据。任务能执行本地代码时，秘密保护应优先通过隔离和受控模型调用边界，而非仅靠环境变量命名。无法满足所声明安全策略时拒绝该运行配置。

## 7. 交互命令增强 M4-C

在 batch 真实链路稳定后，只选一个后端接入 command consumer。Command 字段：command_id、run_id、case_id、session_id、kind、payload_ref、created_at、expires_at、expected_session_revision、dedupe_key、delivery_state。

投递状态区分 queued、delivered、acknowledged、rejected、expired、delivery_unknown；具体枚举与现有 RunCommand 兼容演进。HTTP 202 只代表持久接收，不表示送达；前端必须等待 ack。非交互 backend 返回 409，无消费者返回 501 的现有边界继续保留。

审批必须绑定原始 tool request hash、参数、请求身份与过期时间，旧批准不能授权新请求。重启后不重复提交危险批准；无法确认消费结果则 delivery_unknown。人工消息、批准/拒绝和文件修改标記 intervention=true，进入 M6 比较条件。

本增强不阻塞 M4-A/B 的交付；没有做到时持续关闭按钮，不能留下假的交互体验。

## 8. 详细实施工作包

| 任务 | 输入 → 产出 | 实施范围 | 测试（拟新增/扩展） |
|---|---|---|---|
| M4-T01 | 当前目录/probe → RuntimeDefinition/Profile | 类型、版本、model_control、能力与预检规则 | `tests/contract/test_runtime_profile.py`：不同 backend 必填项正确，声明不等于授权 |
| M4-T02 | 上游版本 →兼容锁定 | 官方包/二进制来源、依赖、协议 fixture、加载资源声明 | `tests/protocol/test_runtime_compatibility.py`：未知版本/关键能力缺失 fail closed |
| M4-T03 | Pi SDK →真实 bridge | 真 session、固定模型、事件、工具注入、错误 | 扩展现有 `tests/protocol/test_pi_bridge_hardening.py`；新增 `test_pi_real_adapter.py` |
| M4-T04 | Pi bridge →平台 Run | Backend 注册、Case 隔离、CaseAttempt、Observation、评分 | `tests/integration/test_pi_run_backend.py`：非 echo，工具产物和评分真实 |
| M4-T05 | argv/config →公共 supervisor | 管道边界、背压、总时长、进程树与工作区 | `tests/harness/test_supervised_process.py`：大 stderr 不死锁、阻塞/子进程可清理 |
| M4-T06 | Claude batch →事件/产物 | 原生配置、结果 parser、错误与费用来源 | `tests/harness/test_claude_batch.py`：成功/失败/无终态/未知事件 |
| M4-T07 | Codex batch →事件/产物 | 固定 exec transport、session、item/event/usage 映射 | `tests/harness/test_codex_batch.py`：JSONL partial、turn failure、config drift |
| M4-T08 | 三个 backend →用户工作区 | API/CLI、能力目录、监控、产物、限制与评分视图 | `tests/api/test_harness_runs.py`；`apps/web/src/evalTypes/harness/HarnessPages.test.tsx` |
| M4-T09 | 实际小任务 →支持证据 | 真 Pi、Claude、Codex 分别验证；脱敏回放 fixture | `tests/integration/test_external_runtime_slice.py` +手动 live 验收记录 |
| M4-T10（增强） | RunCommand →真实消费/ack | 消息/批准/拒绝/取消、去重、过期与恢复 | `tests/runtime/test_command_delivery.py`：重启不重复授权、ack 不等于任务成功 |
| M4-T11（扩展） | Inspect log →只读证据 | 原始日志/样本身份/评分引用导入，明确缺失 | `tests/harness/test_inspect_log_import.py`：源版本不支持拒绝、不执行日志内容 |

执行顺序：T01/T02→T03→T04；T05→T06/T07；T04+T06+T07→T08/T09。T10/T11 分开验收且不与 batch 支持混写。每项先以 fake binary/transport 制造失败，再接真实实现，不用真实费用代替单元回归。

### 协议 fixture 示例（平台归一化目标，不是原生 CLI schema）

```json
{"run_id":"fixture-run","case_id":"case-1","session_id":"session-1","seq":1,"type":"runtime_started","payload":{"runtime":"codex-cli","transport":"batch"}}
{"run_id":"fixture-run","case_id":"case-1","session_id":"session-1","seq":2,"type":"tool_call","payload":{"call_id":"call-1","name":"file_write"}}
{"run_id":"fixture-run","case_id":"case-1","session_id":"session-1","seq":3,"type":"runtime_finished","payload":{"exit_code":0,"artifact_count":1}}
```

该 fixture 只证明事件归一化；是否完成任务必须另查 Artifact 与 Evaluator，不能由最后一个事件推导质量通过。

## 9. 强制验收矩阵

| ID | 场景 | 预期 |
|---|---|---|
| M4-A01 | 未安装/版本不兼容/缺认证 | 明确不同原因，创建前或启动前失败，不假成功 |
| M4-A02 | Pi prompt 无真实 SDK | execution_ready=false，禁止 echo fallback |
| M4-A03 | 两个 Case 连续运行 | session/workspace 无交叉污染 |
| M4-A04 | 主机存在未声明规则/插件 | 隔离或完整记录；否则可复现性 partial |
| M4-A05 | stdout 单行超限/UTF-8 截断 | 协议错误，不无限缓存 |
| M4-A06 | 大量 stderr 与慢消费者 | 有界消费，无管道死锁 |
| M4-A07 | 正常退出但缺 final/产物 | 证据不足或质量失败，不自动通过 |
| M4-A08 | 超时/取消/后台工具不退出 | 原生 interrupt 加受控强制终止，残留可见 |
| M4-A09 | Worker 崩溃但 CLI 已产生副作用 | 不完整重放；恢复观察或 needs_review |
| M4-A10 | 模型/费用未回报 | unknown，不能填请求模型或零费用充当事实 |
| M4-A11 | 人工补充提示或批准 | 记录 intervention，比较条件变化 |
| M4-A12 | 重复 command/过期批准/旧 session | 去重或拒绝，不跨 session 授权 |
| M4-A13 | 工具读取凭据/宿主路径 | 按策略阻断，不能泄漏到 Artifact/SSE |
| M4-A14 | 同输出被不同 parser 版本解析 | 保留 source/parser version；不重写历史评分 |
| M4-A15 | backend 关闭 | 历史可读，其他后端不受影响 |

## 10. 验证与交付

```bash
uv run pytest -q -m "not live" tests/protocol tests/harness tests/runtime
uv run pytest -q -m "not live" tests/integration/test_external_runtime_slice.py
pnpm --dir bridges/pi test
pnpm --dir apps/web test
pnpm --dir apps/web build
make check
```

真实后端验证逐个使用一个受控文件任务；另测取消与非零退出，保存版本/参数/产物/计量/限制。普通 CI 不使用个人订阅登录，也不自动触发付费调用。Windows 原生、WSL2、Linux 各自标明进程/沙箱支持，POSIX 通过不代表 Windows 已通过。

交付：`docs/operations/pi.md`、`docs/operations/cli-harnesses.md`、固定版本兼容矩阵、`docs/protocols/runtime-events.md`、`docs/verification/M4.md`；增强交付再增加 commands runbook。runtime 构造不允许自动下载或升级上游；依赖更新必须单独通过 fixture 与集成门。

回退按 backend/version 关闭新运行，保留原始事件与 parser 版本、Artifact 和评分历史。存量 session 先确认停止/完成；不擅自清理用户原有 home/config/auth 目录。

M4 向 M5 提供可注入 Skill 的能力声明，不承诺所有 runtime 都支持相同注入；向 M6 提供 model_control、tool_control、原生设置和人工干预信息。

## 11. 来源

- [B1] [当前 Pi 适配器](https://github.com/BaiZhi967/MoTTEavl/blob/a668d13ee5ea0c3613648f8992ecaa4a148d3855/packages/agent-runtime/motte_agent/pi.py)
- [B2] [当前 Harness 目录](https://github.com/BaiZhi967/MoTTEavl/tree/a668d13ee5ea0c3613648f8992ecaa4a148d3855/packages/harness-runtime)
- [B3] [能力矩阵](https://github.com/BaiZhi967/MoTTEavl/blob/a668d13ee5ea0c3613648f8992ecaa4a148d3855/docs/protocols/provider-compatibility.md)
- [U1] [Pi 上游入口](https://github.com/earendil-works/pi)
- [U2] [Claude Code programmatic usage](https://code.claude.com/docs/en/headless)
- [U3] [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive/)
- [U4] [Codex App Server](https://developers.openai.com/codex/app-server/)
- [U5] [Inspect logs](https://inspect.aisi.org.uk/eval-logs.html)
