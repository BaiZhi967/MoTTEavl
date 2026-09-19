# M1–M7 开发执行总计划

日期：2026-09-19。范围：**七阶段详细计划与计划 review**，依据用户本次明确选择，不实施功能、不触发 live、不执行迁移或发布。

需求依据：[路线索引](../../../roadmap/README.md)、其中七份阶段文档及[总路线](../../../ROADMAP.md)。需求原件保留；本目录把需求转成依赖明确、可分批实施、可逐项验收的执行卡。

静态核对基线：`b6c402b62f0a19f79cde719af60c8bb73672c877`，当前分支 `main`。它晚于原路线基线 `a668d13ee5ea0c3613648f8992ecaa4a148d3855`。本次所有功能状态仍是 `planned / not_run`；“计划审阅完成”不代表实现或测试完成。

## 1. 入口与交付安排

| 阶段计划 | 主要交付 | 目标 / 工作包 / 验收 | 初步工程量 |
|---|---|---|---|
| [M1](M1.md) | Observation、多指标存储、Builtin Agent、受控文件与证据 | 18 / 10 / 15 | 27–45 人日 |
| [M2](M2.md) | ExternalJob、C-Eval、共用 Lite Gate、独立 CMMLU 追加 | 17 / 11 / 15 | 27–46 人日 |
| [M3](M3.md) | Task/Trial、Harbor、Verifier、取消/清理与结果下钻 | 18 / 10 / 15 | 26–41 人日 |
| [M4](M4.md) | 真 Pi、Claude/Codex batch、交互 consumer、Inspect 只读导入 | 18 / 11 / 15 | 33–53 人日 |
| [M5](M5.md) | Workflow/Fixture、Skill 三组对照、Judge 作业和校准 | 22 / 12 / 18 | 33–54 人日 |
| [M6](M6.md) | Experiment、比较、统计、固定 Baseline、统一 Gate/CI | 21 / 11 / 20 | 28–46 人日 |
| [M7](M7.md) | 同步 SDK、迁移、备份恢复、安全部署、发布切换 | 23 / 12 / 21 | 37–59 人日 |
| 合计 | 全部七阶段 | **137 / 77 / 119** | **211–344 人日** |

工程量按一名熟悉项目的工程师估算，含实现、工作包 review 与回归，不含等待外部环境、数据使用证据、真实调用预算和人工标注；不是日历承诺，也不是对 Agent 运行时长的预测。M2-T09 只估装配成本，M6-Lite 算法成本只计入 M6，避免重复计数。前两包完成后，以实际吞吐与返工量更新估算。

每份阶段计划包含：进入条件、批次、逐包文件触点、输入/输出、具体实施步骤、反例与预期、focused 命令、137 个目标的责任映射、119 个场景的主责映射、原文所有二级章节覆盖、阶段 review 和退出门。

[coverage.json](coverage.json) 是可机读依赖与覆盖清单；[REVIEW.md](REVIEW.md) 记录本次计划审阅、发现、修订和验证范围。

## 2. 推荐实施方式与顺序

推荐按纵向能力顺序推进，每个工作包包含测试、代码、接口、文档和 review。全部前后端一次铺开会让共享契约同时漂移；只先做七阶段后端则会把用户入口与联调风险留到最后。按当前单执行器、共享存储与单用户架构，采用以下顺序更容易核验每次交付。

```text
B0  开工核对、回归基线、隔离开发分支
B1  M1-T01…T10：评分 → Agent → 文件/证据 → 用户闭环
B2  M2-T01…T07：ExternalJob → C-Eval 数据/Parser → 公共入口
B3  M6-T01…T03 → M2-T09 → M2-T08/T10 → M2-T11
B4  M3-T01…T10：Task/Trial → Harbor → 校准与证据
B5  M4-T01…T09 → M4-T10/T11：真 runtime → batch → 增强
B6  M5-T01…T12：场景 → Skill → Judge → 业务闭环
B7  M6-T04…T11：Experiment → 统计 → 完整 Gate/CI
B8  M7 已提前建设项收口 → T11 RC → T12 全范围切换 review
```

- 硬依赖以各卡和 coverage.json 的 DAG 为准。M6-T01/T02/T03 只需 M1 固定证据，不依赖 M2-T09；M2-T09 消费它们，避免循环。
- M3 环境阻塞时可推进依赖已满足的 M4/M5；不能把 Harbor 全完成变成它们的隐含前置。
- M7-T01/T02/T05 的契约/打包准备，以及 T09/T10 的维护模式/安全设计与反例，从 M1/M2 起穿插。完整 T09 验收需导入清单，完整 T10 的 retention 验收需 baseline；提前准备不等于提前完成。
- M6-T04/T05 可在 Lite 稳定后穿插；T06 等 M3 Trial 资格，T08 等 M5 Judge 校准契约。人工干预测试用合成已持久证据，不把可选 M4-T10 变成正式 Gate 核心硬依赖。
- 在制品上限是一个主功能包与一个验证/文档包。同一存储迁移、公共契约、API 或生成类型不并行修改；独立 fixture 可提前准备。
- M2-T11、M4-T10、M4-T11 全部有独立计划与验收，不从 77 项中删除。它们不阻塞 C-Eval/batch 首批 Core；但“全部七阶段全部工作包完成”必须包含它们，不能用 Core 完成替代全范围完成。

后续实现默认由当前执行者逐包完成，配合阶段 review；本次不创建子代理、不创建其他任务、不创建排程。开工若需要隔离 checkout，使用项目 worktree 流程并保留无关工作，不直接在共享 main 上积累功能改动。

## 3. 当前实现与原计划的差异

以下是本次直接阅读代码得到的事实；没有将静态阅读描述为运行验证。

| 已核对事实 | 代码证据 | 计划处理 |
|---|---|---|
| Observation 已存在，只有 name/value/source | `packages/contracts/motte_contracts/evidence.py` | M1-T01 版本化兼容演进，不再定义平行同义模型 |
| ScoreSet 只允许每 Case 一条；三 store 的 score_sets 查询按 pass/case | `evidence.py`、`packages/storage/motte_storage/audit_store.py`、`pg_audit_store.py` | M1-T01/T03 共同改唯一键与查询，预留 Trial；M3-T02 消费 |
| Direct LLM v2 已有封闭 ScorerRegistry 和 choice/numeric/json_equal 等 | `packages/evaluators/motte_eval/scorer_registry.py` | M1-T02 复用注册策略与算法；不新造可动态 import 的入口 |
| Direct C-Eval/CMMLU 纯转换器已存在，但来源受限、官方口径未建立 | `packages/sdk-python/motte_sdk/adapters/restricted_chinese.py`、`datasets/direct-llm/sources/ceval.json`、`cmmlu.json` | M2-T04/T06/T11 复用有用部分；不据此宣称正式 Runner 接通或绕过治理 |
| 后端仍只有 direct-llm/replay 可执行，external-benchmark unavailable | `packages/sdk-python/motte_sdk/execution_backends.py` | M1/M2/M4/M5 完成真实接线后才能提高 readiness |
| Builtin runtime 请求模型硬编码、未补齐 assistant 历史，事件在实例列表 | `packages/agent-runtime/motte_agent/builtin_react.py` | M1-T04/T05/T07 接真实模型、完整消息与持久调用边界 |
| ScenarioSpec.model 当前必填；SkillManifest.entrypoint 当前必填 | `packages/contracts/motte_contracts/scenario.py`、`packages/skill-runtime/motte_skill/manifest.py` | M4-T01 按 backend 演进模型要求；M5-T06 按 kind 演进入口要求 |
| benchmark service 新增暂态失败 second-chance sweep | `packages/sdk-python/motte_sdk/service.py` | M1-T07、M2-T03、M5-T05/T09 明确副作用/Job/Judge 不继承整段补跑 |
| 当前迁移文件链到 0003_resource_publications | `migrations/versions/0003_resource_publications.py` | 新任务从开工实际 head 追加；不相信旧兼容文档的 0002 head |
| Vitest 只收集 tests/**/*.test.{ts,tsx} | `apps/web/vite.config.mts` | 所有计划 Web 测试改到 apps/web/tests，避免原建议 src 测试未被执行 |
| make check 包含递归 Web/bridge 测试；CI 另外有审计、扫描、类型漂移检查 | `Makefile`、`.github/workflows/ci.yml` | 阶段门禁分列，不能把 make check 等同全部 CI |
| 备份按目录复制工件、restore 覆盖目标、GC 按 mtime | `packages/storage/motte_storage/maintenance.py` | M7-T09/T10 做引用一致性、staging restore 和 pin 感知 GC |
| 本 checkout 未发现 .agents/skills 目录 | `AGENTS.md` 引用的项目路径核对 | UI 包开工先定位指定设计 skills；找不到时报告该前置缺口，不用另一套设计规范代替 |

第三方包版本、官方数据 revision、旧平台源码许可与最新上游 API 本次未联网核验。它们是 M2-T04/T05/T06、M3-T03/T04、M4-T02、M5-T01、M7-T06/T11 明确的实施输入；准备好固定证据后才能锁定版本，不在计划中虚构 digest 或“latest 已支持”。

## 4. 共享契约的实现决定与交接

下列是拟实施设计，不是现成 API。字段全集与语义沿原阶段文档；这张表解决跨阶段接口碰撞。

| 契约/边界 | 唯一责任 | 实施决定与消费者约束 |
|---|---|---|
| Observation | M1-T01/T08 | 旧 name/value/source 保持兼容读取；新 schema 显式保存 run/case/attempt、终止、引用、coverage 与 evidence_hash。消费侧不得读取可变 workspace 来补证据 |
| MetricResult → ScoreSet | M1-T01/T03 | 新写入以 pass/case/可空 trial/metric/evaluator/version 唯一；SQL 中用明确的无 Trial 规范键避免 NULL 唯一性差异。旧单指标字段以 legacy 投影兼容，不伪造旧 scorer provenance；多指标查询需显式 metric 或返回集合，不能随意取第一行 |
| hash 与证据冻结 | M1-T01/T08 | 复用 canonical_sha256 的规范 JSON 约定，输入先拒绝 NaN/Infinity；事件引用按协议序列，Artifact 清单按稳定标识排序；请求秘密不参与公开 hash 原文。评分冻结视图与脱敏版本绑定 |
| InvocationRecord | M1-T07 | 每次模型/工具调用有 prepared/dispatching/settled；Run/CaseAttempt 仍是主状态机。Judge 同样记录调用用途，未知结果不自动完整重放 |
| ExternalJob | M2-T01/T03 | prepare/start/poll/interrupt/collect/cleanup 分开；launch token 与资源启动身份持久化；导入 key+内容 hash+checkpoint 事务化。M3 仅扩展 Task/Trial 载荷 |
| Task/Trial | M3-T01/T02 | Task 稳定身份不取 basename；planned repeat 才是 Trial；CaseAttempt 冲突域随 Trial 扩展；同一调用的成本不能在 attempt/trial/run 重复累计 |
| Runtime/Profile | M4-T01 | 版本固定 transport、模型/工具控制、可观察能力；CLI 原生认证与平台 Provider 分别校验。纯字符串名称和协议握手不等于 execution_ready |
| Workflow/Skill | M5-T01/T06/T07 | Scenario 是外层身份；Workflow 管步骤、Skill 管版本与注入；有效权限求交，deny 优先。旧 DSL 无法表达的条件返回诊断 |
| ScoringJob/Judge | M5-T09 | 与 Run 共用 Worker 执行权和排队/恢复基础；评分作业可有自己的用途状态但不是第二套调度系统。完成且满足发布政策才切 current pass |
| ReportRef/Baseline/Gate | M6-T01/T03/T07 | 固定 run+pass+policy+证据。重复求值同语义输入有同结论；evaluated_at 只作审计元数据。历史结论不重写，新证据缺失会降低新求值资格 |
| 迁移/恢复 | M7-T06…T10 | imported origin 不可 claim；只读源、明确映射和 staging 校验；恢复不自动执行备份中的 queued/running；GC 服从 baseline/pass/needs_review pin |

不建立第二份 C-Eval Gate、第二个 Benchmark Catalog 数据事实库、另一个 Run 调度器或另一个评分历史库。存储改造同时覆盖 SQLite/PG/InMemory；旧读兼容不是只让 Pydantic 能 parse，还要验证查询、报告和消费者。

## 5. 横向要求不遗漏

| 总路线要求 | 责任工作包 | 具体验收 |
|---|---|---|
| 全局 UI、公共类型与状态 | M1-T09、M2-T08、M3-T09、M4-T08、M5-T11、M6-T09、M7-T03/T12 | 按 DESIGN/token/STATUS_META；公共 API→TS；禁用/加载/空/错/长日志/键盘；真实路由可访问 |
| Provider 原生工具与结构化输出 | M1-T02/T04/T05 | 三类现有 HTTP adapter 保持 canonical 工具历史；不支持参数付费前拒绝；JSON Schema 断言有缺失与错误分支 |
| Provider 流式与探针证据 | M4-T02/T08，M7-T11 收口 | 将模型流与 Run SSE 区分；实现的协议需有 chunk/tool 增量/终态/断流/取消 fixture、能力目录与独立 live 证据；静态 probe 不收费，真实探针是显式动作。M1 不因 token 流而阻塞 |
| Benchmark 来源治理 | M2-T04/T05/T11、M3-T01/T03、M7-T10 | 固定 revision/checksum/代码与数据许可；smoke/full/Profile 不能混名；未核验来源保持 restricted/unprepared |
| 证据与安全 | M1-T06…T08、M2-T03、M3-T05/T07、M4-T05、M5-T02/T09、M7-T09/T10 | 隐藏断言、日志脱敏、配额、权限交集、原始 hash、覆盖与实际清理 |
| 持续工程质量 | 每包 focused + 每阶段 gate；M7-T05/T11 | 当前 Direct/GSM8K/Replay/Direct v2 来源治理回归；make check；生成类型、真实安装、PG/Docker 分层验证 |
| 旧平台迁移资产 | M2-T06、M3-T04、M4-T11、M5-T01/T10、M6-T03/T08、M7-T06…T12 | 每项记录旧 commit/文件、许可、保留/修改语义、移除耦合、golden 差异与验证层级；无许可资产不公开 |

Provider 流式横向子任务纳入 M4-T08，不新增第 78 个工作包：触点为 `packages/provider-runtime/motte_provider/base.py`、`transport.py`、`openai_chat.py`、`openai_responses.py`、`anthropic_messages.py`、`packages/contracts/motte_contracts/messages.py`；拟测试 `tests/provider/test_streaming_contracts.py`。执行时先锁定各协议真实事件 fixture；未知或未实现的流式能力显示 unavailable，不伪造 token。三个 adapter 都必须独立说明实现/验证状态，正式支持声明由 M7-T11 核对。M4-T08 已计入该子任务的初步工期，取得固定协议证据后再次估计。

总路线的多租户/RBAC、Kubernetes、多 Agent、任意 MCP、商店、全量更多 Benchmark、RAG/多模态等非目标仍维持原范围，进入 M7 旧能力分类表，不在七阶段借机实现。

## 6. 逐工作包与阶段 review 流程

1. 核对基线、依赖契约、现有同职责代码、用户改动与设计约定。迁移先读实际 head，外部协议先取固定来源证据。
2. 从本卡和对应 A 行构造合成输入与可观察结果；写可收集的测试并确认真实行为红灯。反例不使用真实密钥、订阅或业务数据。
3. 按卡片步骤实施最小完整链路，使用既有执行主权；冻结身份/配置，保留错误、缺失、取消和不确定结果。
4. focused 复验和相邻回归；API 改动同步 OpenAPI/TS；UI 改动遵循指定设计 skills 并跑 test/build；涉及 DB 的三 store 契约和 PG 集成分别记录。
5. 逐包 review 需求、正确性、故障/并发、安全、兼容与文档。登记问题编号、复现输入、严重度、责任包、修复和复验结果；关闭问题后提交一个可独立评审的变更。
6. 阶段最后一包执行全部 G/A/原文章节对账及完整用户链路，检查消费者和回退；实际证据合格才勾选功能目标。
7. review 发现缺陷时回到责任包修复，复验受影响 focused/相邻测试及阶段门禁；不必无理由重复所有外部付费任务。失败、跳过、缺环境、未授权均不能记 passed。
8. M7-T12 做全路线 review。全部 77 包及三个替代场景有证据才称“全部完成”；仅 Core 满足或调整发布范围，要明确记录，不能抹去未完成目标。

本次计划 review 是当前执行者的静态自审与机械检查，不声称独立代码审查。后续阶段代码 review 必须读最终 diff 和真实测试输出，不能只检查清单已打勾。

## 7. 验证层级和外部依赖处理

| 层级 | 实现时要求 | 不可替代 |
|---|---|---|
| Contract/unit | 固定边界、版本、类型、错误和分母 | 不能证明外部调用可执行 |
| Golden/replay | 固定 raw hash 与旧新 Parser 差异 | 合成内容不代表官方数据或真实质量 |
| 服务集成 | 公共 API/Worker/存储、本地假 HTTP/CLI、实际 PG/Docker | fake binary 不代表真实 Harness 支持 |
| 显式 live | 具体模型/runtime/数据/Profile、预算、费用和工件 | 未跑配置不能继承别人的支持记录 |
| 发布/运维 | 仓库外干净安装、真实 Compose、升级、备份恢复、切换对账 | compose config 和 build 不代表启动/恢复成功 |

实际开发中遇外部阻塞：记录受阻目标、环境/权限/版本、可独立继续的工作包与解除条件；继续满足依赖的离线工作。只有真实 subject/Judge/Harness 调用、真实数据 apply、生产替换/发布等具体操作才核对相应授权；日常可逆代码/文档工作无需反复确认。

验证记录在实现时写入 `docs/verification/MN.md`，至少包含 milestone、work_package、goal_ids、acceptance_ids、implementation_commit、source_baseline、verification_layer、command、environment、result、evidence_refs、limitations、review_findings。result 仅使用 not_run/passed/failed/blocked/not_applicable；不适用必须说明边界和替代证据。文档内示例数据不是实际记录。

## 8. 首批可开工交付单

后续进入实施时先执行 M1-T01，然后 T02/T03，完成一个冻结 Observation 可产生多个指标并追加 ScoringPass 的离线切片，再接 Agent。首批预期评审材料如下：

- 旧 Observation/Score roundtrip 与新多指标契约，拒绝非法引用、NaN、重复身份。
- exact/contains/regex 兼容对照及文件/工具/JSON Schema 评分反例。
- SQLite/PG/InMemory 多指标存储与查询、旧行读取、追加 pass、不改变历史 hash、rescore 零 Provider 调用。
- 新迁移从实际 head 追加，协议/操作文档与回归证据随代码提交。

后期阶段继续以已合入前置契约复核本计划；允许修正落位路径与私有签名，但不得以“计划过时”为由缩减 G/A 或改变指标/副作用语义。
