# M4 开发 Agent 完整提示词

下面内容可整体发送给开发 Agent。

---

你是 MoTTEavl 的开发 Agent。本次请实际完成 M4 的开发、测试、逐包 review 和最终 review。仓库为 `/Users/whitezhi/Dev/MoTTEavl`，从包含 M3 第三轮修复的最新主干建立 `codex/m4-pi-external-harnesses` 开发分支；开始前检查 HEAD、工作区和 AGENTS.md，保护已有修改，不重置或覆盖其他 Agent 工作。

请先完整阅读这些文件，再核对当前代码：

1. `docs/roadmap/README.md` 与 `docs/ROADMAP.md` 第 9 节。
2. `docs/roadmap/M4-pi-and-external-harnesses.md` 全文。
3. `docs/superpowers/plans/2026-09-19-m1-m7/M4.md`：全部 11 个工作包、18 个目标、15 个验收及非编号章节要求。
4. `docs/superpowers/plans/2026-09-20-m4-kickoff.md`：最新代码基线、接口决策、顺序和重点回归。
5. `docs/verification/M3-review-round3-fixes-2026-09-20.md`：已修复的配置、归属、完整计划和离线边界，确保不回归。
6. UI 开工前阅读 `apps/web/DESIGN.md`，执行根 AGENTS.md 指定的 UI skill 与门禁。

任务目标是让同一受控任务分别经真实 Pi、Claude CLI、Codex CLI 执行，在平台统一持久化 session、CaseAttempt、InvocationRecord、事件、Artifact、Observation、ScoringPass、取消与清理证据。必须完成公共 API/CLI/Web 链路，不能只做 wrapper、类型、probe 或 fake demo。

按以下顺序推进：

- T01/T02：RuntimeDefinition/Version/Profile、条件模型校验、不可变资源与三存储；从官方来源锁定准确上游版本、原生配置/flags、protocol fixture 和兼容矩阵。
- T03/T04：真实 Pi SDK bridge 与平台 Backend；用离线 scripted model 驱动真实 SDK 工具任务，保留全部协议/进程硬化回归，禁止 echo 或 Builtin fallback。
- T05：复用并加固 supervisor，有界双管道、受控 argv/env/cwd/config、session 启动身份、超时取消、进程树与恢复。
- T06/T07：Claude batch 与 Codex exec JSONL 各自接通原生 parser、工具/模型/计量证据、产物和评分，分别验收。
- T08/T09：API/CLI/Web 真实持久化对账、后端能力和有效配置显示、retry 子 Run、历史与支持证据；补原计划 Provider streaming 横向项，OpenAI Chat、OpenAI Responses、Anthropic Messages 三个 adapter 分别实现文本/工具增量、终态 usage、断流取消及 `tests/provider/test_streaming_contracts.py`；准备分后端 live 验收包。
- T10：单独 Codex app-server transport + RunCommand consumer，真实投递/ack、去重/过期/绑定/恢复与人工干预审计。
- T11：固定 Inspect 原始日志格式的只读导入、样本与评分来源、幂等和诊断，不实现 Inspect 执行。

T10/T11 是独立增强交付，但不能从完整 M4 范围里删除。batch 可独立验收，不能据此宣布全部 M4 完成。不要推进 M5–M7 的功能。

执行规则：

1. 使用适用的计划执行/子代理开发技能。可派子代理处理独立文件；共享契约、storage、API main 和 runtime_backends 必须有单一写入负责人。逐包先写有效失败断言、确认红灯，再实现、绿灯、review、修复和提交。
2. 复用现有 ExecutionBackendSpec/ExecutionHandle/Dispatcher/RunService。batch 是 sample backend；M3 Trial 不是 CLI event/session 的替代身份。不要另建调度器、Provider 或评分系统。
3. 每个 CaseAttempt 独立 session/workspace。所有事件和结果在写库前核验 run/case/session/operation 身份；创建失败、queued cancel、执行取消与导入拒绝仍保留计划分母和准确 disposition。
4. 已发布配置、冻结 Run、原始证据和历史评分不覆盖。请求模型/参数/工具/预算/凭据引用必须进入真实消费者；不支持则具名拒绝，不静默裁掉字段或切 backend/transport。
5. 区分 installed/protocol_ready/execution_ready 和 fixture/integration/live 证据。实际模型、usage/cost、原生工具或加载资源不可观察时显示 unknown/partial，不能填请求值/0 冒充事实；strict 策略不满足则拒绝。
6. 记录有效配置、binary/adapter/parser/schema version、工作区 revision/dirty patch hash、规则/Skill/MCP/plugin 清单与非秘密 hash、原生审批/工具/网络边界。不得无记录使用操作者 home、auth、历史 session 或全局配置。
7. env allowlist 和日志脱敏不是沙箱。Pi/CLI 原生工具必须有与声明一致的实际隔离或诚实的不可强制状态；不要为了跑通而授予任意宿主目录、Docker socket 或默认关闭审批。
8. stdout/stderr 同时有界消费，原生 terminal + exit + Artifact + Evaluator 分开判定；exit=0/模型说完成不等于质量通过。超时/取消要停止全部归属工具后再冻结；无法确定写 needs_review 和残留证据。
9. Worker/bridge 断连或重启后不自动完整重放可能已发生副作用或费用的任务。恢复只观察原操作或标不确定；显式 retry 新建子 Run/session，保留父证据。
10. Command 的 HTTP 202 仅表示持久接收。必须实际 consumer 投递与上游 ack；批准绑定原 tool request hash、参数、身份、session/revision 和过期时间。重启不重复危险批准，未知投递结果标 delivery_unknown。人工干预进入比较条件。
11. Inspect 输入只读、受限、版本化；不执行日志内容或加载任意对象。只有 aggregate 时不制造 sample；原生 imported score 与平台重评 pass 区分。
12. UI 使用 DESIGN.md tokens、STATUS_META、Radix、Phosphor；有 API 变化运行 `make openapi`，生成 OpenAPI/TS 与代码同提交。

允许受控的真实 SDK + scripted model 离线验证和 CLI help/config 静态协议探测；supervisor 故障注入使用临时目录无网络 fixture，不继承操作者密钥。默认验证全部离线，禁止自动调用真实 Provider/付费模型/个人订阅、启动真实评测或重启活动 Worker。先完成全部不依赖外部授权的实现和验证，再提交具体可执行的 live 验收包：后端/版本、模型、调用上限、预计费用边界、工作目录、凭据引用、任务与取消步骤、预期证据和清理范围。没有授权时不运行依赖步骤；继续独立工作，并把真实验收明确记为 pending/blocked。安装或探测也不能自动登录、升级全局二进制或更改个人配置。

每包执行对应 focused 回归和相邻测试，最终至少运行：

```bash
uv run pytest -q -m "not live" tests/protocol tests/harness tests/runtime tests/storage
uv run pytest -q -m "not live" tests/provider/test_streaming_contracts.py
uv run pytest -q -m "not live" tests/integration/test_external_runtime_slice.py
pnpm --dir bridges/pi test
pnpm --dir apps/web test
pnpm --dir apps/web build
make openapi-check
make check
uv run python docs/superpowers/plans/2026-09-19-m1-m7/validate_plan.py --allow-code-changes
```

缺测试文件、ImportError、skip、fake binary、SDK 参数 schema 接受都不是真实能力通过证据。PG、Docker、各 OS 与各后端 live 单独列出执行/未执行状态。

最终全面 review 必须从公共请求→资源解析→冻结→dispatch→启动→事件→持久化→评分→展示跨层检查，并主动复现：错身份/重复事件/队列取消/大 stderr/UTF-8 断帧/无 final/父退出子进程继续写/配置漂移/费用缺失/过期批准/恢复重复授权/导入路径穿越/后端禁用后历史读取。所有 P0/P1/P2 修复复验；不能用测试数量代替需求覆盖。

维护 `docs/verification/M4.md`：逐 G/T/A 和非编号章节记录实现路径、测试命令、结果、commit、环境、证据级别及限制。交付 Pi/CLI 操作手册、runtime-events、兼容矩阵、runtime-commands、inspect-import、发布/回退说明。逐包提交代码、回归与对应文档。此次开发先保留在 M4 分支供 review，未经后续指示不要自动合并主干或推送。

最终回复应包含：各工作包状态、核心改动、测试与跳过原因、review 问题及关闭情况、commit、可操作验收步骤和剩余外部条件。只有全部必备项具备对应级别证据，才能宣称完整 M4 完成；否则准确报告“代码/离线完成，live 待验收”等状态，并继续完成所有可独立推进的工作。
