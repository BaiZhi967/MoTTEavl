# Direct LLM 题库扩充完整实施计划

- 日期：2026-09-19
- 基线分支：`main`
- 基线提交：`a668d13ee5ea0c3613648f8992ecaa4a148d3855`
- 远端状态：本地 `main` 已 fast-forward 至 `origin/main`，工作区干净
- 计划状态：仓库可实现的 Phase 0-11 已完成；stable 来源许可证、人工复核、有限 live smoke、Docker 与本地 PostgreSQL 证据仍按发布门禁 fail closed

基线验证：

- Direct LLM 相关 Python 测试：`96 passed`，2 条既有依赖弃用警告；
- Web 全量测试：`8` 个 test files、`101 passed`；
- 同步后 `main` 与 `origin/main` 一致，规划落地前工作区干净。

实施收口（Phase 0-11）：

- Phase 0-8 的 v1 兼容修复、v2 contract/scorer/selected-only snapshot、受管来源平台、TruthfulQA/MMLU-Pro/MoTTE Core ZH 转换能力、typed API、profiles、dry-run 和 Web 来源视图均已落地；GSM8K 始终保留独立 suite/plugin/scorer/版本语义。
- Phase 9 的 C-Eval/CMMLU 仅提供严格离线实验 adapter，状态保持 `restricted`；Phase 10 的 IFEval preview 与 LongBench v2 full-context adapter 使用独立 metric/协议，状态保持 `pending`，不伪装成 Direct accuracy 或官方榜单结果。
- Phase 11 离线门禁：`982 passed / 15 skipped`，Ruff、contracts mypy、compileall、OpenAPI/TypeScript 确定性再生成、TypeScript typecheck、Web `130 passed`、Web production build 与 Pi selftest 通过。fake-provider E2E 覆盖三个首批转换器、四个 v2 scorer、失败 outcome、selected-only snapshot、retry 与 rescore。
- 最终 P1 复核已补齐：numeric 极端指数资源上限、完整 selected snapshot hash、provider execution-only manifest、显式版本 fail-closed、data/code 双许可证据、portable publication receipt、通用 API 原子发布门禁、bundle 关系绑定、当前 SourceSpec/provenance 与普通 retry 复核、managed converter 权威 provenance、cache ancestor/junction 检查、Web v2 `selected_cases` 读取和 judged/unjudged 报告语义。GSM8K 与 Direct LLM v1 行为保持独立兼容。
- `make check` 只在最后的 Compose config 步骤失败：本机没有 `docker` 可执行文件。PostgreSQL 专项因未设置 `MOTTE_PG_DSN` 为 `10 skipped`。这两项未被绕过或伪造为通过。
- 当前 7 个来源均无 stable 可发布项：5 个 `pending`，2 个 `restricted`。因此 stable 许可证批准、MoTTE Core ZH ownership/privacy 双人复核与三模型校准、官方 parity、有限付费 live smoke 都仍是外部发布退出门禁；CLI/API/SDK 默认继续阻断，不以技术测试替代人工证据。

## 0. 执行摘要

本计划扩充 Direct LLM 的确定性、单轮文本评测题库，同时保持 GSM8K 为独立标准基准，不进行 GSM8K 到 Direct LLM 的转换。

计划采用四类题库：

1. 仓库内置 smoke 样例：保留现有 23 题，只验证导入、运行、评分和展示链路。
2. 受管公开题库：通过固定上游 revision、受控下载、确定性转换和许可证门禁按需导入，不把大文件提交到 Git。
3. 项目自编题库：通过程序化生成器和独立答案 oracle 生成 1,000 道中文题，不直接采用 LLM 自问自答结果。
4. 受限及高级题库：C-Eval、CMMLU、IFEval、LongBench v2 等在基础能力稳定后分阶段接入。

第一条可发布主线不是“马上下载更多题”，而是先处理会被大题库放大的正确性和容量问题：

- Direct LLM v1 聚合分母与概览分母存在不一致路径；
- 数据集自动版本只比较 `cases_sha256`，未覆盖 scorer、provenance 和转换配置；
- dataset 与 scenario 目前两次独立写入，发布不是原子操作；
- 每个 Run 当前冻结整份数据集，导入 12K 题后即使只跑 50 题也会重复保存整库；
- 通用资源列表会返回完整 dataset JSON；
- Web 多数据集题目选择存在跨数据集残留风险；
- Web 对可选空字段和 API 的“缺席才默认”语义不一致；
- Direct LLM API 仍以无类型 `dict` 为主，OpenAPI 无法表达稳定契约。

因此，关键路径为：

```text
Phase 0 治理基线
  -> Phase 1 v1 正确性与扩容阻断项
  -> Phase 2 Direct LLM v2 契约与按所选题冻结
  -> Phase 3 确定性评分器 v2
  -> Phase 4 数据源获取与转换平台
  -> Phase 5/6/7 首批题库并行接入
  -> Phase 8 产品化、分层抽样与可观测性
  -> Phase 11 发布硬化

Phase 9 受限许可证中文题库、Phase 10 高级题型不阻塞首批发布。
```

### 0.1 阶段总览

| 阶段 | 优先级 | 主要产物 | 退出结果 |
| --- | --- | --- | --- |
| Phase 0 | P0 | ADR、来源准入表、许可证门禁、范围冻结 | 关键决策可审计，无许可证模糊项被标为“可直接发布” |
| Phase 1 | P0 | v1 正确性修复、原子发布、身份哈希、Web 选择闭环 | 现有 23 题和历史 v1 Run 行为兼容，扩容阻断缺陷关闭 |
| Phase 2 | P0 | Direct LLM contract/plugin v2、所选题快照、摘要资源接口 | Run 存储量与完整题库规模解耦，v1 仍可读和重评分 |
| Phase 3 | P0 | `choice`、`numeric`、`json_equal` 评分器及注册表 | 新题型全部确定性评分，解析失败可诊断 |
| Phase 4 | P0 | 数据源注册表、下载缓存、转换器框架、CLI | 同一 revision/config 重复执行得到同一 fingerprint |
| Phase 5 | P1 | MMLU-Pro 5-shot CoT 协议适配器；0-shot 单独分榜 | 12,032 道测试题可按许可和协议 parity 状态受控导入 |
| Phase 6 | P1 | TruthfulQA Binary Direct 适配器 | 817 道真实性二选一题可确定性洗牌和评分 |
| Phase 7 | P1 | `motte-core-zh` 1,000 题生成器 | 中文业务与格式能力形成可复现自有基线 |
| Phase 8 | P1 | profile/分层抽样、API/CLI/Web 管理、维度统计 | smoke/regression/full 可重复，操作者能看清来源和限制 |
| Phase 9 | P2 | C-Eval、CMMLU 可选适配器 | 仅在显式非商业许可确认后可用 |
| Phase 10 | P2 | IFEval、LongBench v2；代码/LLM Judge 仍后置 | 高级评分与长上下文能力独立发布 |
| Phase 11 | P0 release | 性能、迁移、运行手册、发布证据 | `make check` 全绿并完成离线与有限 live 验证 |

编号按能力工作流组织，不代表第三方题库可越过治理门禁。实际题库稳定晋级顺序固定为：先完成 Phase 7 `motte-core-zh` 试点，再推进 Phase 6 TruthfulQA，最后在许可与协议一致性关闭后推进 Phase 5 MMLU-Pro。Phase 5 的 adapter 可以提前做隔离 PoC，但不能先于自编题试点进入 stable。

### 0.2 里程碑

| 里程碑 | 包含阶段 | 可交付能力 |
| --- | --- | --- |
| M0：范围和治理冻结 | Phase 0 | 可审计的来源、许可和版本策略 |
| M1：可扩容内核 | Phase 1-3 | 兼容 v1 的 Direct LLM v2 与新评分器 |
| M2：可复现数据供应链 | Phase 4 | 任意受支持来源可固定 revision 后转换、校验、导入 |
| M3：首批扩充发布 | Phase 5-7 | MMLU-Pro、TruthfulQA、自编中文包 |
| M4：日常运营可用 | Phase 8 | profile、过滤、来源展示、维度统计和成本预估 |
| M5：可选高级覆盖 | Phase 9-10 | 非商业中文基准、指令遵循、长上下文 |
| GA：稳定发布 | Phase 11 | 完整测试证据、升级/回滚手册和已知限制 |

## 1. 已确认决策

以下决策在本计划中视为已冻结；改变时必须新增 ADR，而不是在实现中静默偏离。

1. 不转换 GSM8K。GSM8K 保留独立 suite、prompt、Decimal scorer、统计口径和官方来源身份。
2. 现有 23 道 Direct LLM 内置题继续是 smoke fixture，不包装成权威基准，不扩充成大体量内置文件。
3. 大型第三方数据不提交到仓库，不打进 wheel，不在安装阶段自动下载。
4. 网络访问只能由操作者显式触发；运行和评分路径不隐式联网。
5. 任何公开题库必须固定上游 revision 和原始 artifact SHA-256；“latest”只允许作为一次解析动作，落库必须是具体 revision。
6. 转换器必须确定性；同一来源 revision、artifact、converter version、converter config 和 seed 必须生成相同 dataset fingerprint。
7. 数据集和场景一经发布不可修改；修题、换 prompt、换 scorer 或换 metadata 都产生新版本。
8. 首批发布只包含确定性评分的单轮文本任务；不引入代码执行、工具调用、多轮会话和 LLM-as-a-Judge。
9. 第三方题库在 MoTTEavl 中均标记为 Direct 变体，除非逐项证明与官方协议等价，否则不得使用“官方同分”或直接对齐官方排行榜的表述。
10. API 和 Web 不把题目标准答案发送给 Provider。expected、scorer config 和 provenance 只存在于服务端快照与评分路径。
11. 服务端远程题库下载默认关闭。首批稳定入口是 CLI；Web 一键获取只有在建立可恢复的导入任务或明确接受同步操作边界后再开放。
12. 许可证状态不是自由文本装饰。`approved`、`restricted`、`pending` 三态决定可见性和可执行动作。

## 2. 当前实现基线

### 2.1 已有能力

| 能力 | 当前实现 | 位置 |
| --- | --- | --- |
| Direct LLM JSONL 导入 | `input|prompt`、`expected?`、`scorer?`、`case_id?`；整份校验 | `packages/contracts/motte_contracts/direct_llm.py` |
| 确定性评分 | `exact`、`contains`、`regex` | `packages/evaluators/motte_eval/direct_llm.py` |
| 不可变资源 | dataset/scenario/version insert-only | `packages/storage/motte_storage/resource_store.py` |
| 运行级子集 | `all`、`ids`、`random + seed` | `packages/contracts/motte_contracts/selection.py` |
| 评测插件 | 按 `suite_id + plugin_version` 分派 prepare/score/aggregate | `packages/sdk-python/motte_sdk/benchmark_plugins.py` |
| 审计证据 | resolved manifest、evaluation descriptor、append-only scoring pass | `packages/contracts/motte_contracts/run.py`、`evidence.py` |
| API | overview、builtins、import、cases、runs | `apps/api/app/main.py` |
| CLI | builtins、list、import、run | `packages/cli/motte_cli/main.py` |
| Web | 操作、题目、过程、结果、对比 | `apps/web/src/evalTypes/directllm/` |
| 测试 | contract、evaluator、SDK、API、CLI、runtime、Web | `tests/**/test_direct_llm*`、`apps/web/tests/evalTypes.test.tsx` |

### 2.2 当前硬约束

- Direct LLM suite 识别依赖 `eval.suite/id/version` 三元组；当前 `DATASET_VERSION = 1`。
- `SCORER_VERSION = direct-llm-answer-v1` 写进 dataset preset 和运行 provenance。
- 每题 metadata 只允许 `source_line` 与 `scorer`，未知字段拒绝。
- `expected` 只能是非空字符串；结构化 expected 需要编码为字符串或升级契约。
- dataset 的 `eval` 必须与 `_preset()` 完全相等，不能额外挂 profile 或能力字段。
- v1 plugin 缺省版本是 `1`；历史 scenario 没有 `plugin_version`。
- 当前 Run 的 `benchmark_snapshot.dataset` 是完整 dataset 深拷贝。
- resource store 对所有版本化资源执行不可变校验，不只 managed suite。
- Generic `GET /api/v1/datasets` 返回完整 dataset 记录，会把所有 cases 一次性送出。
- CI 必须离线，测试不得下载真实数据或产生模型费用。

### 2.3 扩容前必须关闭的已知缺陷

#### 2.3.1 分母不一致

`score_answer_case()` 对“没有 expected 且调用失败”的题产出 `outcome=call_failed, judged=false`；`aggregate_answers()` 目前按所有 `call_failed` outcome 计入 judged，而 `_direct_llm_accuracy()` 按 `score.judged` 计算。概览与报告可能得到不同 accuracy。

处理原则：

- v1 先统一到 `judged` 标志，不改变有 expected 题的既有结果；
- 增加“无 expected + 调用失败 + 混合题集”的聚合回归测试；
- v2 明确定义 `accuracy`、`coverage`、`completion`、`attempt_rate` 的不同分母。

#### 2.3.2 自动版本身份不完整

`next_dataset_version()` 当前只比较 `cases_sha256`。同一 cases 但 scorer、license、source、prompt、converter config 不同会错误复用旧版本，随后触发不可变冲突。

处理原则：

- v1 保留兼容读取；
- 新导入使用 `dataset_fingerprint`，至少覆盖 cases、eval、provenance 中影响解释和复现的字段；
- fingerprint 相同视为幂等；cases 相同但 fingerprint 不同必须创建新版本。

#### 2.3.3 dataset/scenario 非原子发布

当前 `import_direct_llm_split()` 先 `datasets.put()`，再 `scenarios.put()`；第二步失败会留下孤立 dataset。

处理原则：

- 为 SQLite、PostgreSQL 和 InMemory resource store 增加同事务发布 dataset + scenario 的接口；
- 发布前做键冲突预检，但不以预检代替事务；
- 失败注入测试必须证明不会留下半发布资源。

#### 2.3.4 Run 快照随完整题库线性增长

导入 12K 题后，随机跑 50 题仍保存完整 dataset，导致数据库、Run API 和 Web 响应重复膨胀。

处理原则：

- v1 快照保持原样，保证历史重评分；
- v2 只冻结所选 cases、dataset identity、eval/scorer/provenance 摘要和哈希；
- 完整 dataset 仍是不可变资源，可按 `name@version` 核验；Run 自身不依赖未来资源可变性，因为资源不可删除。

#### 2.3.5 Web 跨数据集选择闭环不完整

题目页载入 sessionStorage 时未按 stored dataset 对齐当前 dataset，切换数据集也没有立即清理 selected；操作页载入选择后没有可靠切换至 `ids` 模式。数据集增多后容易带入其他题库 case ID。

处理原则：

- Stored selection 必须绑定 dataset fingerprint，而不只 dataset 名称；
- 数据集切换立即清空或隔离选择；
- 回到操作页后显式显示来源 dataset 并切换到指定题目模式；
- 服务端继续作为最终边界拒绝未知 ID。

#### 2.3.6 Web 空可选字段与 API 语义不一致

Web 本地 JSONL 导入会发送 `name: ""`，API 规定只有字段缺席才使用默认值，显式空串返回 422。

处理原则：

- 客户端构造请求时省略空可选字段；
- API 使用 Pydantic request model 表达缺席、空值和默认值；
- 增加真实 client/API 契约测试，不只 mock UI 调用。

#### 2.3.7 类型与集成测试缺口

- Direct LLM API request/response 多为 `dict`，OpenAPI 无法稳定生成类型。
- `tests/integration/test_direct_llm_run.py` 实际只覆盖 replay，不能替代真实 Direct LLM 集成测试。
- Generic datasets 列表完整返回 cases，没有大数据集网络载荷测试。

#### 2.3.8 CLI suite 隔离不完整

`motte direct-llm run` 只检查 scenario 是否存在，没有像专用 API 一样确认它属于 Direct LLM。当前可能把 GSM8K scenario 交给通用 `prepare_run()`，再由 GSM8K plugin 创建运行，造成 CLI 命令语义与 API 不一致。

处理原则：

- Direct LLM CLI 只能接受 `is_scenario()` 判定为 Direct LLM 的 scenario；
- GSM8K、Replay 和未知 suite 都在创建付费 Run 前拒绝；
- API/CLI 双向 suite isolation 使用同一个 helper 和错误码。

#### 2.3.9 Compare 缺少可比性守卫

当前 Direct LLM Compare 以第一列的 snapshot、case IDs 和 scorer 为题目基准，没有验证其他 Run 是否来自相同 dataset/version 或相同 case set。跨题库、跨版本或不同子集时可能遗漏题目并产生误导。

处理原则：

- 先比较 dataset fingerprint、selection hash、prompt/scorer 版本和运行参数；
- 不可直接比较时显示差异，不计算伪共同错题；
- 若支持不同 case set，必须做 union 并显式显示 missing，而不是以第一列为准。

#### 2.3.10 上下文窗口没有调用前门禁

ModelProfile 虽可声明 `context_window`，当前 Direct LLM 创建和执行路径没有可靠的 token-fit preflight。MMLU-Pro 5-shot prompt 和 LongBench v2 可能在调用后才由 Provider 拒绝，或发生供应商侧不可见截断。

处理原则：

- v2 Run 创建前计算或保守估算每题输入 tokens、预留输出 budget，并与模型 context window 比较；
- 不适配题目在零模型调用前拒绝，或通过显式 eligibility profile 排除；
- 禁止静默截断；任何 head/tail/full 策略必须版本化；
- LongBench v2 发布前必须具备 tokenizer-aware eligibility matrix。

#### 2.3.11 资源发布审计缺口

当前资源表保存最终 JSON payload，但没有“谁、何时、通过哪个来源/命令发布”的 append-only publication event。provenance 能解释内容来源，不能替代操作者审计。

处理原则：

- 原子发布同时写入不可变 `ResourcePublication` 审计记录；
- 记录 actor/入口、dataset/scenario ref、fingerprint、source/converter receipt hash 和时间；
- 审计记录不放题目正文和 secret；
- 若本期不引入资源审计表，必须把它列为 stable public-source 发布阻断项，而不是用日志代替。

#### 2.3.12 文档与实现的不可变语义漂移

`direct_llm.py` 顶部注释称非 managed 记录“不享受不可变”，但 resource store 已对所有 versioned dataset/scenario 执行 insert-only。Phase 1 必须同步注释和操作文档，避免实现规划基于过时假设。

## 3. 目标、非目标和成功指标

### 3.1 目标

1. 支持 10K 以上题目的不可变 Direct LLM 数据集，而单个小规模 Run 的持久化体积只与所选题目数量相关。
2. 支持选择题、数值题和 JSON 结构化输出的确定性评分。
3. 建立可复现、可核验、默认离线的数据源获取和转换链路。
4. 首批形成英文知识推理、真实性和中文业务/格式三类互补题库。
5. 支持 smoke、regression、full 三档固定 profile，并保存实际 case ID 与哈希。
6. 在结果中展示数据来源、许可状态、converter/scorer/prompt 版本和样本覆盖维度。
7. 保持既有 Direct LLM v1 dataset、scenario、Run、ScoringPass 可读、可展示、可重评分。
8. 在任何模型调用前完成 suite、许可、题目选择、上下文窗口和预算门禁，不依赖 Provider 付费失败来发现配置问题。
9. 让任意报告可凭归档 artifact、source/conversion manifest、Run snapshot 和 scorer version 离线复核。

### 3.2 非目标

- 不转换或替代 GSM8K。
- 不实现官方排行榜复刻框架。
- 不在第一批支持 HumanEval/MBPP 等不可信代码执行。
- 不在第一批支持 MT-Bench/AlpacaEval 等 LLM Judge。
- 不在第一批支持多轮对话、工具调用或 RAG。
- 不把随机抽题视为统计学上自动可比；只有相同 dataset fingerprint、profile/case IDs、prompt/scorer 和运行参数才可直接比较。
- 不声称公开 benchmark 能检测训练数据污染。
- 不把许可证字符串当作法律意见；状态为 pending 的来源不能进入默认稳定目录。

### 3.3 量化成功指标

| 维度 | 指标 |
| --- | --- |
| 兼容性 | 所有既有 Direct LLM v1 测试不改预期通过；历史 v1 Run 可重评分 |
| 可复现性 | 相同 source revision/artifact/config/seed 在两次独立目录转换后 `dataset_fingerprint` 相同 |
| 容量 | 50 题 v2 Run 快照不包含未选中的题；Run payload 大小不随完整数据集题数线性增长 |
| 正确性 | scorer golden tests、metamorphic tests 和对抗格式 tests 全绿；聚合与概览 accuracy 同源 |
| 数据质量 | 0 个重复 case ID；0 个越界 choice label；0 个无法解析的 expected；转换拒绝未知 schema drift |
| 供应链 | 每个 artifact 有 URL、固定 revision、SHA-256、字节数；下载使用临时文件后原子落盘 |
| 许可证 | 每个来源有 `approved/restricted/pending` 状态、证据 URL 和人工复核日期 |
| 运维 | 网络失败、哈希不符、上游 schema 漂移、依赖缺失均返回结构化错误且不落半个资源 |
| 调用前门禁 | suite、license、context-fit、selection 和预算错误全部在零模型调用前拒绝；无静默截断 |
| 审计 | dataset+scenario 发布与 publication event 同事务；任一报告可离线重算 |
| 测试 | CI 零网络、零真实模型费用；`make check`、Web test/build、compose config 全绿 |

## 4. 题库组合与准入策略

### 4.1 分层

| 层级 | 定义 | 分发策略 | 默认可用性 |
| --- | --- | --- | --- |
| `builtin-smoke` | 项目仓库内的小型演示数据 | 随代码分发 | 开启 |
| `managed-public` | 许可证和来源经复核的公开数据 | 按需下载，固定 revision | 开启或显式安装 |
| `generated-internal` | 项目拥有生成器和答案 oracle | 本地确定性生成 | 开启 |
| `restricted-public` | 非商业、署名/相同方式共享或访问受限 | 不随包分发，显式确认 | 默认隐藏或禁用 |
| `advanced` | 需要专用 evaluator、长上下文或沙箱 | 独立能力门禁 | 默认禁用 |

### 4.2 候选来源登记

许可证结论必须在 Phase 0 由人工核对具体 revision 的仓库许可证、数据卡和数据文件声明。下表是工程准入状态，不是法律意见。

| 来源 | 已知规模 | 主要能力 | 当前证据 | 工程状态 | 首批范围 |
| --- | ---: | --- | --- | --- | --- |
| 现有 Direct LLM builtins | 23 | 链路 smoke | `internal-sample` | approved | 保留不扩容 |
| MMLU-Pro | test 12,032；validation 70 | 英文知识与推理选择题 | HF 数据卡标 MIT；GitHub repo LICENSE 为 Apache-2.0，存在标记差异 | pending，人工核验后才能稳定发布 | 适配器可先实现为 experimental |
| TruthfulQA | 817 | 真实性、反常识二选一 | 官方 repo 标 Apache-2.0 | pending review -> approved 后发布 | 只接新版 binary MC，不接生成评分 |
| `motte-core-zh` | 1,000 | 中文计算、分类、抽取、格式遵循 | 项目自有生成器 | 需完成权属与隐私清单 | 首批 |
| C-Eval | 13,948 | 中文 52 学科 | 数据集 CC BY-NC-SA 4.0 | restricted | Phase 9 |
| CMMLU | 67 主题，每主题 100+ test | 中文知识与中国语境 | 数据集 CC BY-NC-SA 4.0 | restricted | Phase 9 |
| IFEval | 541 | 可验证指令遵循 | 数据卡 Apache-2.0 | evaluator 未就绪 | Phase 10 |
| LongBench v2 | 503 | 8K 到 2M 词长上下文选择题 | HF 数据卡 Apache-2.0；repo LICENSE 为 MIT，需核对数据与代码边界 | pending + 高成本 | Phase 10 |
| BIG-bench | 200+ tasks | 多类型能力 | repo Apache-2.0，但任务可能有独立来源和 canary | 按 task 单独审查 | 不作为整库导入 |
| HumanEval/MBPP | 代码生成 | 函数正确性 | 需要执行不可信代码 | blocked by sandbox | 本计划不接入 |
| MT-Bench/AlpacaEval | 开放问答 | 对话质量 | 需要多轮与 LLM Judge | blocked by judge governance | 本计划不接入 |
| GPQA | 高难科学问答 | 专业推理 | 访问受限，需鉴权和条款确认 | restricted/pending | 本计划不接入 |

### 4.3 来源准入检查表

一个来源只有同时满足以下条件才能从 `experimental` 升为 `stable`：

1. 官方主页、仓库、数据卡、论文和许可证 URL 已登记。
2. 数据许可证与代码许可证分开记录，冲突已人工裁定。
3. 商业使用、再分发、署名、相同方式共享和访问限制有明确状态。
4. 上游 revision 可固定，所有下载 artifact 可计算 SHA-256。
5. 转换不执行远程代码，不加载 pickle，不启用 Hugging Face `trust_remote_code`。
6. 原始字段、题数、split 和答案范围有 schema contract tests。
7. prompt 变体、选项顺序和 answer extraction 规则有版本号。
8. 是否可与官方结果比较有明确布尔结论和解释。
9. 数据污染、canary、敏感内容和 PII 风险已记录。
10. 删除、下架或许可证变化时的禁用策略已登记；已发布不可变资源不被静默改写。

## 5. 目标架构

```text
Operator / CI
  -> CLI (默认唯一联网入口)
       -> SourceRegistry
       -> RevisionResolver
       -> SafeFetcher
       -> ArtifactCache (var/datasets/direct-llm/<source>/<revision>/)
       -> ConverterRegistry
       -> DatasetValidator
       -> Atomic Dataset+Scenario Publisher

Web / API
  -> Source catalog (只读许可与可用性)
  -> Dataset catalog summaries
  -> Paginated case browser + metadata filters
  -> Profile/ids/random run creation

Run preparation (Direct LLM plugin v2)
  -> resolve immutable dataset@version
  -> expand profile/selection to exact case IDs
  -> snapshot dataset identity + selected cases only
  -> provider snapshot without expected values

Worker
  -> Direct LLM execution backend
  -> provider receives selected prompts only
  -> scorer registry validates/extracts/compares
  -> immutable ScoringPass + per-case details
  -> aggregate by explicit denominators and dimensions
```

### 5.1 模块边界

| 模块 | 职责 | 禁止事项 |
| --- | --- | --- |
| contracts | v1/v2 schema、身份、验证、选择语义 | 网络、文件写入、模型调用 |
| evaluators | 纯函数 scorer、parse、compare、aggregate | 读取当前 dataset 资源、联网 |
| SDK source layer | revision、fetch、cache、convert、publish 编排 | 绕过 contract/store |
| storage | 原子发布、不可变版本、摘要查询 | 理解具体题库业务 |
| API/CLI | typed boundary、错误映射、显式动作 | 重复实现转换和评分 |
| Web | 来源/许可展示、筛选、选择、运行 | 客户端自行裁定未知 case ID |
| worker | 执行已解析快照、追加评分证据 | 运行期下载题库或补写资源 |

### 5.2 建议新增结构

```text
datasets/direct-llm/
  README.md
  direct-llm-*.jsonl
  sources/
    mmlu-pro.json
    truthfulqa.json
    motte-core-zh.json

packages/contracts/motte_contracts/
  direct_llm.py                 # v1 兼容 facade + version dispatch
  direct_llm_v2.py              # v2 case/eval/provenance/scorer contracts
  dataset_sources.py            # source catalog contracts

packages/evaluators/motte_eval/
  direct_llm.py                 # 保留 v1
  direct_llm_v2.py              # v2 scoring/aggregation
  scorer_registry.py

packages/sdk-python/motte_sdk/
  direct_llm.py                 # v1 + v2 manifest/publish dispatch
  direct_llm_sources/
    __init__.py
    registry.py
    fetch.py
    cache.py
    convert.py
    mmlu_pro.py
    truthfulqa.py
    core_zh.py

apps/web/src/evalTypes/directllm/
  DirectLlmSources.tsx
  DirectLlmCases.tsx
  DirectLlmOperate.tsx
  sourceMeta.ts
```

不要求为了目录美观先搬动现有实现。新模块只有在形成清晰版本边界时创建，避免无行为收益的重构。

## 6. Direct LLM v2 契约

### 6.1 版本策略

| 记录 | v1 | v2 |
| --- | --- | --- |
| `eval.version` | `1` | `2` |
| scenario `plugin_version` | 缺省，解释为 `1` | 显式 `"2"` |
| prompt | `direct-llm-verbatim-v1` | 每个 dataset 明确 prompt template version |
| scorer | 字符串，global version v1 | scorer spec + registry version |
| snapshot | 完整 dataset | dataset identity + selected cases |
| metadata | `source_line/scorer` | typed source/dimensions/tags/template fields |
| compatibility | 保留原实现 | 新 plugin，不改写 v1 |

`motte_contracts.suites` 必须能识别 v1 和 v2，并按 `eval.version` 分派 validator。不得把 `DATASET_VERSION` 从 1 原地改为 2 后让旧记录失去 managed 身份。

### 6.2 v2 输入行

建议保留简单 JSONL 体验，但允许 metadata 和 scorer spec：

```json
{
  "case_id": "mmlu-pro-test-143",
  "input": "...\n\nAnswer with a final marker [ANSWER:X].",
  "expected": "C",
  "scorer": {
    "id": "choice",
    "version": "1",
    "config": {
      "labels": ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"],
      "marker": "[ANSWER:{value}]"
    }
  },
  "metadata": {
    "source_id": "143",
    "language": "en",
    "subject": "physics",
    "category": "physics",
    "difficulty": null,
    "split": "test",
    "tags": ["multiple-choice"],
    "template_family": "mmlu-pro-direct-v1"
  }
}
```

约束：

- `input` 与 `prompt` 仍二选一；落库统一为 `input`。
- `expected` 首批 v2 仍限定为字符串，避免一次性扩大通用 `Case.expected` 的语义。
- `scorer` 接受字符串 shorthand 或完整 spec；落库统一规范化为完整 spec。
- metadata 必须 typed 且 `extra=forbid`；不接受任意对象无限扩张。
- `source_line` 由 importer 添加，用户 metadata 不得伪造。
- `case_id` 必须稳定，不依赖当前文件排序；公开来源优先使用上游稳定 ID。
- 每题 scorer config 经 scorer registry 在导入期验证。

### 6.3 ScorerSpec

```json
{
  "id": "choice",
  "version": "1",
  "config": {},
  "config_sha256": "..."
}
```

规则：

- `id + version` 决定实现；未知版本拒绝导入和重评分。
- config 先规范化再哈希；默认值必须显式展开，避免不同输入得到相同语义却不同哈希。
- dataset 可声明默认 scorer，单题可覆盖。
- 每个 Score 保存 scorer id、version、config hash、parsed value 和失败原因。
- EvaluationDescriptor 对混合 scorer 数据集使用 `scorer_id=direct-llm-deterministic`，具体 scorer 以每题 Score 为准。

### 6.4 CaseMetadataV2

首批字段：

| 字段 | 类型 | 用途 |
| --- | --- | --- |
| `source_line` | positive int | 转换后 JSONL 物理行号 |
| `source_id` | string | 上游稳定 ID |
| `language` | enum/string | 语言过滤 |
| `subject` | string/null | 学科或业务主题 |
| `category` | string/null | 高层分类 |
| `difficulty` | string/null | 上游难度或生成器难度 |
| `split` | string | test/validation/generated 等 |
| `tags` | distinct string list | multiple-choice/json/format 等 |
| `template_family` | string/null | 程序化模板族或 prompt 模板 |
| `scorer` | normalized ScorerSpec | 单题覆盖，内部落库字段 |

禁止把 rationale、标准解题过程或版权敏感附加文本无条件复制进 metadata。若来源包含 `cot_content`，首批 MMLU-Pro 只用于上游校验，不落进运行 prompt，也不作为 expected。

### 6.5 ProvenanceV2

```json
{
  "source_id": "mmlu-pro",
  "source_kind": "managed-public",
  "homepage": "https://huggingface.co/datasets/TIGER-Lab/MMLU-Pro",
  "upstream_revision": "<immutable commit>",
  "artifacts": [
    {
      "logical_name": "test",
      "url": "https://.../<revision>/...parquet",
      "sha256": "...",
      "bytes": 123
    }
  ],
  "artifact_manifest_sha256": "...",
  "license": {
    "id": "MIT",
    "status": "pending",
    "evidence_urls": ["..."],
    "commercial_use": "unknown",
    "redistribution": "unknown",
    "reviewed_at": null
  },
  "converter": {
    "id": "mmlu-pro-to-direct",
    "version": "1",
    "config": {"split": "test", "prompt_version": "mmlu-pro-direct-v1"},
    "config_sha256": "..."
  },
  "synthetic": false
}
```

时间戳不得参与 dataset fingerprint。`reviewed_at` 是治理证据；若修改它会改变不可变记录，应创建新数据集版本或放入独立 source catalog，而不是原地更新已发布 dataset。

### 6.6 Dataset fingerprint

定义 canonical payload：

```text
{
  contract_version,
  eval_without_selected_count_derived_noise,
  normalized_provenance_identity,
  cases_sha256,
  profiles_sha256
}
```

`dataset_fingerprint = sha256(canonical_json(payload))`。

不参与 fingerprint 的字段必须列入白名单，默认没有字段可排除。name/version 不参与，以便同一内容可在显式名称下识别，但发布冲突仍按 `(name, version)` 判断。

幂等规则：

1. 同 name + 同 fingerprint：复用已有 version。
2. 同 name + 同 cases hash + 不同 fingerprint：分配新 version。
3. 显式 name@version 已存在且 fingerprint 不同：409。
4. source artifact 相同但 converter version/config 不同：新 fingerprint、新 version。
5. fingerprint 相同但 record canonical 内容不同：视为实现错误，拒绝发布并记录审计错误。

### 6.7 v2 Run 快照

```json
{
  "schema_version": 2,
  "scenario": {"name": "mmlu-pro-direct", "version": "1", "plugin_version": "2"},
  "dataset": {
    "ref": "mmlu-pro-direct@1",
    "fingerprint": "sha256:...",
    "cases_sha256": "...",
    "eval": {},
    "provenance": {},
    "total_cases": 12032
  },
  "selection": {
    "mode": "profile",
    "profile": "smoke",
    "count": 56,
    "case_ids": [],
    "case_ids_sha256": "...",
    "seed": null
  },
  "selected_cases": []
}
```

约束：

- `selected_cases` 只含本次 case IDs，顺序与执行顺序一致。
- `manifest.cases` 只投影 prompt 和执行所需字段，不含 expected/scorer/provenance。
- scorer 从 `selected_cases` 读取 expected 和 scorer spec。
- rescore 只依赖 Run 快照，不重新读取当前 dataset 资源。
- retry 复用相同 resolved snapshot 和 selection，不重新抽样。
- 旧 v1 Run 继续读取 `benchmark_snapshot.dataset.cases`。

### 6.8 v2 指标语义

| 指标 | 分子/分母 |
| --- | --- |
| `accuracy` | `correct / judged` |
| `coverage` | `judged / selected` |
| `completion` | `responded / selected` |
| `attempt_rate` | `attempted / selected` |
| `format_failure_rate` | `invalid_format / judged` |

`judged = correct + wrong_answer + invalid_format + call_failed`，但只有有 expected 的题可 judged。`not_attempted` 不 judged。所有指标同时返回原始计数和明确 `denominator` 描述，禁止出现 attempt rate 大于 1 的定义。

## 7. 确定性评分器 v2

### 7.1 统一接口

每个 scorer 实现：

```python
validate_expected(expected, config) -> NormalizedExpected
parse_output(content, config) -> ParsedValue | ParseFailure
compare(parsed, expected, config) -> bool
score(result, expected, config) -> ScoreEvidence
```

注册表只加载内置显式实现，不在首批动态加载第三方 Python 代码。

### 7.2 `choice@1`

用途：MMLU-Pro、TruthfulQA、C-Eval、CMMLU、LongBench v2。

规则：

- labels 必须是 2 至 26 个不重复 ASCII 大写字母。
- expected 必须恰好是 labels 中一个值。
- 默认只解析最后一个非空行的严格 marker，例如 `[ANSWER:C]`。
- marker 必须全行匹配；不从分析正文中的任意 A/B/C 猜答案。
- 多个 marker、非法 label、缺 marker 均为 `invalid_format`。
- 是否允许 bare final label 是显式 config，默认 false。
- 解析值保存在 `Score.parsed`；原始输出已在 CaseRun result 中，不重复存入 score details。

### 7.3 `numeric@1`

用途：自编算术、日期差、单位换算。

规则：

- 使用 `Decimal`，不经二进制 float。
- 支持 exact、absolute tolerance、relative tolerance；三者互斥或按明确优先级验证。
- 默认要求最终 marker `[ANSWER:<number>]`。
- 首批只支持 ASCII 十进制、小数、合法千分位和可配置正负号。
- 不在首批自动换算单位；单位换算由题目 oracle 计算后比较标准数值。
- NaN、Infinity、科学计数法、分数是否支持必须显式配置，默认拒绝。
- parse failure 与数值错误分开统计。

### 7.4 `json_equal@1`

用途：中文订单、日志、公告和表单结构化抽取。

规则：

- expected 是 JSON 字符串；导入期解析并规范化。
- 输出必须是单个 JSON value，不接受 Markdown fence 或前后解释。
- object key 顺序无关，array 顺序相关。
- duplicate keys、NaN、Infinity 拒绝。
- bool 与 number 类型严格区分；JSON number 使用 Decimal 规范化。
- 首批做完整值相等，不做 JSONPath 子集、schema-only 或模糊文本比较。
- parsed canonical JSON 可写入 score details；敏感字段由题库准入阶段阻止进入数据。

### 7.5 v1 scorer 保持不变

`exact/contains/regex` 的 v1 语义和版本号不原地改变。若 v2 数据仍需使用这些 scorer，则注册为 v2 对应实现，并明确：

- `exact@2` 是否与 v1 strip 语义相同；
- `contains@2` 是否保持大小写敏感；
- `regex@2` 的表达式长度、输出长度和安全边界。

首批公开来源不使用 regex，避免让上游数据控制任意正则。

## 8. 数据源平台设计

### 8.1 SourceSpec

每个 `datasets/direct-llm/sources/*.json` 只保存可审查的静态声明：

- `id`、label、description、tier；
- homepage/repository/dataset card/citation URLs；
- license claims 与 review status；
- revision 类型和 resolver；
- artifact 列表模板、格式、大小上限；
- converter id/version；
- 支持的 split；
- 默认 prompt/scorer/profile；
- 是否需要 optional dependency；
- `official_comparability` 与说明。

JSON 由 Pydantic contract 校验，未知字段拒绝。

### 8.2 下载缓存布局

```text
var/datasets/direct-llm/
  <source-id>/
    <revision>/
      source-manifest.json
      artifacts/
        <logical-name>.<ext>
      converted/
        dataset.jsonl
        conversion-manifest.json
```

流程：

1. 创建同目录临时文件；
2. 限制连接/读取超时、响应大小和允许协议；
3. 下载时增量计算 SHA-256；
4. 校验状态码、Content-Length（若有）、实际字节数和预期哈希；
5. fsync 后原子 rename；
6. 已存在文件只有 hash 相同才复用；
7. 失败清理临时文件，不覆盖好缓存；
8. cache 文件不进 Git，由 `.gitignore` 覆盖。

不自动解压任意 archive。若必须支持 zip/tar，需单独实现路径穿越、symlink、解压大小和文件数限制；首批尽量直接下载单文件 artifact。

### 8.3 格式支持

- JSON/JSONL/CSV：标准库解析。
- Parquet：使用显式 optional dependency `pyarrow`，不引入 `datasets` 的远程代码加载。
- 缺少 optional dependency 返回 `SOURCE_DEPENDENCY_MISSING` 和安装方式，不静默改用在线 rows API。
- 上游字段、类型、split 和行数不符合 SourceSpec 时返回 `SOURCE_SCHEMA_DRIFT`。

### 8.4 CLI

建议命令：

```bash
motte direct-llm sources list
motte direct-llm sources inspect --source mmlu-pro
motte direct-llm sources fetch --source mmlu-pro --revision <sha>
motte direct-llm sources convert --source mmlu-pro --revision <sha>
motte direct-llm sources verify --source mmlu-pro --revision <sha>
motte direct-llm sources import --source mmlu-pro --revision <sha> --name mmlu-pro-direct
motte direct-llm sources prepare --source mmlu-pro --revision <sha> --name mmlu-pro-direct
motte direct-llm generate --source motte-core-zh --seed 20260919
```

`prepare` 是 fetch + verify + convert + validate + atomic publish 的便捷编排，每一步仍写结构化 receipt。CI 只测注入的 fake fetcher 和小 fixture。

### 8.5 结构化错误

| code | 语义 |
| --- | --- |
| `SOURCE_NOT_FOUND` | 未登记来源 |
| `SOURCE_LICENSE_BLOCKED` | restricted/pending 来源未满足门禁 |
| `SOURCE_REVISION_INVALID` | revision 不是允许的不可变格式 |
| `SOURCE_UNAVAILABLE` | 网络/状态码/超时失败 |
| `SOURCE_TOO_LARGE` | 超出 artifact 大小上限 |
| `SOURCE_HASH_MISMATCH` | artifact 哈希不符 |
| `SOURCE_SCHEMA_DRIFT` | 上游字段、类型、题数或答案范围漂移 |
| `SOURCE_DEPENDENCY_MISSING` | 缺少 Parquet 等可选依赖 |
| `CONVERSION_FAILED` | 转换逻辑失败 |
| `DATASET_VALIDATION_FAILED` | 转换结果违反 v2 契约 |
| `RESOURCE_CONFLICT` | 显式 version 与已有不同内容冲突 |
| `PUBLISH_FAILED` | 原子发布未完成，保证无半发布资源 |

## 9. 运行 profile 与比较规则

### 9.1 Profile 定义

profile 是 dataset v2 不可变记录的一部分：

```json
{
  "name": "regression",
  "strategy": "stratified-fixed-ids",
  "count": 560,
  "case_ids_sha256": "...",
  "case_ids": ["..."],
  "dimensions": ["category"],
  "seed": "..."
}
```

首批统一提供：

- `smoke`：约 50 至 100 题，覆盖所有高层 category，验证链路和明显退化；
- `regression`：约 300 至 600 题，按 category/subject 分层固定 case ID；
- `full`：整个 split；只在正式评测时使用。

profile 在转换期展开成固定 IDs，并纳入 dataset fingerprint。运行时选择 profile 不重新抽样。

### 9.2 可比性

只有以下条件全部相同，Web 才标记为“可直接比较”：

- suite/plugin version；
- dataset fingerprint；
- prompt template version；
- scorer registry/scorer specs；
- selected case IDs hash；
- reasoning level、temperature、max output tokens 等影响答案的参数；
- scoring pass 的 scorer version。

模型、Provider 和价格表可以不同，这是比较对象和成本信息。条件不一致时仍可并列展示，但必须显示差异原因。

### 9.3 维度统计

v2 aggregate 除 overall 外，按数据集中实际存在的维度计算：

- language；
- category；
- subject；
- difficulty；
- template_family；
- scorer id。

每个分组返回 selected/judged/correct/accuracy/coverage，样本数小于配置阈值时标记 `low_sample=true`，不隐藏数据。

## 10. 分阶段实施

## Phase 0：治理、ADR 与范围冻结

### 目标

在写业务代码前冻结数据身份、版本、许可、比较声明和默认联网策略。

### 前置条件

- 基线提交为 `a668d13ee5ea0c3613648f8992ecaa4a148d3855`；
- 本计划获技术评审；
- 明确谁负责许可证人工复核。

### 工作包

1. 新增 ADR：Direct LLM v1/v2 共存、GSM8K 不转换、selected-only snapshot。
2. 新增数据源治理文档：license status、证据、reviewer、review date、下架策略。
3. 为 MMLU-Pro、TruthfulQA、C-Eval、CMMLU、IFEval、LongBench v2 建来源登记草案。
4. 固定错误码、source tier 和 official comparability 字段。
5. 固定首批稳定范围：TruthfulQA、`motte-core-zh`；MMLU-Pro 在许可证冲突未裁定前只能 experimental。
6. 确认 Parquet 解析策略：默认采用 `pyarrow` optional extra，不使用远程代码和在线 rows API 回退。
7. 确认服务端远程下载默认关闭，CLI 是首批联网入口。

### 计划修改文件

- `docs/superpowers/specs/adr/2026-09-19-direct-llm-dataset-v2.md`
- `docs/operations/dataset-source-governance.md`
- `datasets/direct-llm/sources/*.json`
- `docs/README.md`

### 测试与审查

- SourceSpec JSON schema/contract 测试；
- 文档链接检查；
- 人工许可证 checklist 签字或明确 pending。

### 退出标准

- 所有来源都有 tier 和 license status；
- 没有 pending 来源被标成 stable；
- ADR 明确 v1 兼容和不转换 GSM8K；
- 技术负责人和来源/许可证负责人批准。

### 回滚

纯文档和登记阶段；撤销登记不会影响现有运行数据。

### 估算

1 至 2 工程日，加许可证人工复核时间。

## Phase 1：修复 v1 正确性与扩容阻断项

### 目标

在引入新契约前，让现有 Direct LLM v1 的统计、导入、发布和多数据集选择行为可信。

### 工作包

1. 统一 aggregate、overview 和 report 的 judged 计算，修复无 expected + call_failed 分母不一致。
2. 定义 `dataset_fingerprint` 通用 helper；v1 新导入 receipt 可额外返回 fingerprint，但不改变旧记录必填字段。
3. 修改自动版本策略：优先 fingerprint，旧记录没有 fingerprint 时按受控兼容规则比较。
4. 增加 dataset + scenario 原子发布接口，覆盖 SQLite/PostgreSQL/InMemory。
5. 为 Direct LLM importer 使用原子发布；注入 scenario 冲突验证无孤立 dataset。
6. 定义 `ResourcePublication` append-only 审计契约；原子发布同事务写 actor/入口、资源 refs、fingerprint 和 receipt hash。若本阶段暂不建表，必须保留为 Phase 4 stable source 的硬门禁。
7. 修复 CLI suite isolation，`direct-llm run` 与专用 API 共用 Direct scenario 判定和错误码。
8. 修复 Web session selection：绑定 dataset fingerprint、切换清理、返回操作页进入 ids 模式。
9. 修复 Direct Compare：先校验 fingerprint/selection/protocol；不同 case set 使用 union + missing 或明确拒绝，不再以第一列静默覆盖。
10. Web 省略空可选 import 字段；API 增加 typed DirectLlmImportRequest 并拒绝未知顶层字段。
11. 增加真实 Direct LLM integration test，不能用 replay 代替。
12. 为 generic dataset list 记录大 payload 风险，Phase 2 提供摘要接口；本阶段先增加保护性测试和 deprecation 注释。
13. 修正 Direct LLM 契约注释和操作文档中“非 managed 资源不享受不可变”的过时表述。

### 重点文件

- `packages/evaluators/motte_eval/direct_llm.py`
- `packages/sdk-python/motte_sdk/datasets.py`
- `packages/sdk-python/motte_sdk/direct_llm.py`
- `packages/storage/motte_storage/resource_store.py`
- PostgreSQL resource store 对应实现；若落地 publication audit，新增 Alembic migration
- `packages/cli/motte_cli/main.py`
- `apps/api/app/schemas.py`
- `apps/api/app/main.py`
- `apps/web/src/evalTypes/directllm/DirectLlmCases.tsx`
- `apps/web/src/evalTypes/directllm/DirectLlmOperate.tsx`
- `apps/web/src/evalTypes/directllm/DirectLlmCompare.tsx`
- `apps/web/src/evalTypes/selection.ts`
- `docs/operations/direct-llm.md`

### 必测场景

- expected 缺失、调用成功/失败混合聚合；
- overview/report/scoring pass accuracy 一致；
- cases 相同而 scorer/license/source 不同分配新版本；
- fingerprint 相同重复导入幂等；
- dataset 写入后 scenario 故障回滚；
- 并发发布相同和不同 fingerprint；
- 从 dataset A 勾选后切到 B 不保留 A IDs；
- 不填 name 的 Web 粘贴导入不发送空串；
- Direct CLI/API 均拒绝 GSM8K、Replay 和未知 suite scenario；
- Compare 对跨 dataset/version/protocol 和不同 case set 明确阻止或显示 missing；
- 原子发布成功时 publication audit 同事务存在，失败时两者都不存在；
- v1 builtins 23 题、CLI/API receipt 和历史 fixtures 不变。

### 退出标准

- 第 2.3 节中属于 v1 的所有阻断缺陷均有回归测试；
- 现有 Direct LLM test suite 全绿；
- 资源发布失败无半发布状态；
- OpenAPI 明确 Direct LLM import body。

### 回滚

- fingerprint 可作为新增字段向后兼容；
- 原子发布接口保留旧 repository API，但 importer 切回前必须证明不会留下孤儿；
- Web 修复可独立回滚，不改变持久格式。

### 估算

4 至 6 工程日。

## Phase 2：Direct LLM v2 契约与容量治理

### 目标

建立不改写 v1 的 contract/plugin v2，并让 Run 快照大小只与所选题数相关。

### 工作包

1. 新增 v2 case、metadata、provenance、profile、scorer spec Pydantic contracts。
2. `motte_contracts.direct_llm` 支持 v1/v2 识别和 validator dispatch。
3. scenario v2 显式写 `plugin_version="2"`。
4. benchmark plugin registry 同时注册 `direct-llm@1` 和 `direct-llm@2`。
5. v2 resolver 展开 profile/selection 后只 snapshot selected cases。
6. v2 scoring 从 selected snapshot 读取，不查询 dataset 资源。
7. retry/rescore 保持同一 selected IDs 和 snapshot hash。
8. 新增 dataset catalog summary endpoint，Web ResourcesPage 不再拉完整 cases。
9. cases endpoint 增加 metadata filters，但仍分页；搜索和过滤在服务端执行。
10. Run/Report API 对 v1/v2 snapshot 提供兼容 view，不把大 snapshot 无条件返回到列表页。
11. 增加 context preflight：根据 provider/model 的 tokenizer capability 或保守估算计算 input tokens + output reserve；超 `context_window` 在创建期拒绝，禁止静默截断。
12. 将 v2 selection、score lookup 和结果聚合预建 case ID map，避免大题集页面和报告中的重复 `array.find` 退化为 O(n²)。
13. 增加 manifest payload size、database growth、context rejection 和 lookup complexity 回归测试。

### 重点文件

- `packages/contracts/motte_contracts/direct_llm.py`
- `packages/contracts/motte_contracts/direct_llm_v2.py`
- `packages/contracts/motte_contracts/suites.py`
- `packages/sdk-python/motte_sdk/direct_llm.py`
- `packages/sdk-python/motte_sdk/benchmark_plugins.py`
- `packages/contracts/motte_contracts/run.py`（仅当 snapshot typed view 需要）
- `apps/api/app/main.py`、`schemas.py`
- `apps/web/src/pages/ResourcesPage.tsx`

### 兼容要求

- 缺 `plugin_version` 的 Direct LLM scenario 必须继续分派 v1；
- 旧 `benchmark_snapshot.dataset.cases` 仍可重评分；
- v2 不能覆盖 v1 常量或复用同一 scorer version；
- suite registry 未知显式版本必须 fail closed，不回落 v1/GSM8K。

### 性能验收

使用合成 12,000 题 dataset：

- 导入可完成且 fingerprint 稳定；
- 选 50 题创建 Run，snapshot 只含 50 题；
- 两个不同完整题库、相同 50 个所选题时，payload 不携带未选题；
- dataset catalog 响应不含 `cases`；
- Run list 响应不含大题面正文；
- context 不适配题在 fake Provider 调用计数仍为 0 时被拒绝；
- 对所选题构建 map 后，报告/Compare 不再对每个 case 重复线性扫描。

### 退出标准

- v1/v2 双 plugin 注册和 dispatch 测试全绿；
- 12K 合成题容量验收通过；
- v2 rescore/retry 不访问网络、不重新读取可变来源、不重新抽样。

### 回滚

禁用 v2 scenario 创建即可；v1 plugin 不受影响。已经发布的 v2 dataset 保持不可变但可标记 unsupported，不能篡改成 v1。

### 估算

5 至 8 工程日。

## Phase 3：评分器 v2

### 目标

提供选择题、数值题、JSON 抽取所需的严格确定性评分，并保留完整解析证据。

### 工作包

1. 实现显式 scorer registry 与版本分派。
2. 实现 `choice@1`、`numeric@1`、`json_equal@1`。
3. 为 v2 `exact/contains/regex` 定义是否支持和明确版本；公开首批不依赖 regex。
4. 导入期调用 `validate_expected/config`，运行期不因坏 gold/config 崩溃。
5. 增加 `invalid_format` outcome，更新 aggregate、Score details、结果页词汇。
6. EvaluationDescriptor 使用聚合 scorer 身份；每题 Score 写具体 scorer。
7. rescore 选择 scorer 实现时只依据 snapshot 中的 id/version/config。
8. 增加 scorer config hash 和 scoring pass source snapshot hash。

### 测试矩阵

- choice：合法 marker、正文干扰字母、多个 marker、非法 label、缺 marker、bare label 配置；
- numeric：正负、千分位、小数、前导零、tolerance 边界、NaN/Infinity、科学计数法策略；
- JSON：key 重排、array 顺序、duplicate keys、数字规范、bool/number、fence、前后解释、非法 UTF-8 来源；
- mixed scorer dataset；
- 无 expected；
- call_failed/not_attempted；
- scorer 版本未知；
- scorer config 被篡改导致 snapshot hash 不符；
- Web outcome 显示与 STATUS_META/Direct LLM outcome vocabulary 一致。

### 退出标准

- 所有 scorer 都有 golden、边界、对抗和 property/metamorphic tests；
- `invalid_format` 与 `wrong_answer` 可区分；
- 同一 snapshot 重评分产生相同 per-case scores 和 summary；
- v1 scorer 测试零变化。

### 回滚

v2 scorer 通过 registry feature flag 禁用；已存在 v2 Run 若实现不可用必须明确返回 unsupported scorer version，不允许回落其他算法。

### 估算

4 至 6 工程日。

## Phase 4：数据源获取与转换平台

### 目标

建立与具体题库解耦的安全、可复现、可离线测试的数据供应链。

### 工作包

1. 实现 SourceSpec contract 和静态 JSON registry。
2. 实现 revision resolver；明确 Git SHA、HF commit 等允许格式。
3. 实现 SafeFetcher、临时文件、hash、大小限制、原子缓存。
4. 实现 artifact/source/conversion manifest。
5. 实现 converter registry 和统一输出 canonical v2 cases。
6. 实现转换后整库 validator、分布统计和 dataset fingerprint。
7. 实现 CLI `sources list/inspect/fetch/convert/verify/import/prepare`。
8. 实现 optional Parquet dependency 检测；不使用远程代码。
9. 实现 license gate：pending/restricted 来源默认拒绝 prepare，必须显式 policy override；override 写入 receipt，但不能修改全局审查状态。
10. prepare 成功时持久化 publication audit；没有 actor/入口、receipt hash 和 dataset fingerprint 的远程来源不得升 stable。
11. 所有网络测试注入 fake transport；增加断流、短读、超长、hash mismatch 和 cache corruption 测试。
12. 更新 `.gitignore`、安装和运维文档。

### 输出 receipt

每次 prepare 至少返回：

- source id、effective revision；
- artifact URLs/hashes/bytes；
- converter id/version/config hash；
- raw/converted cache paths；
- source/converted case count；
- rejected count 和按原因统计；
- dataset/scenario ref；
- dataset fingerprint；
- license status 和 override evidence；
- profile IDs/hashes。

默认不允许静默丢题。若 converter 支持过滤，过滤规则必须在 config 中，receipt 列出每类数量，并参与 fingerprint。

### 退出标准

- 小型 JSONL、CSV、Parquet fixtures 均可转换；
- 两个全新临时目录产物 fingerprint 相同；
- 网络失败和进程中断不留下有效缓存假象或半发布资源；
- CLI 输出机器可读 JSON，错误走 stderr 且 exit code 2；
- CI 全程零网络。

### 回滚

来源平台是附加能力；禁用 source registry 不影响本地 JSONL 和 builtins。缓存可删除，已发布不可变 dataset 不受缓存删除影响。

### 估算

5 至 7 工程日。

## Phase 5：MMLU-Pro 协议化适配器

### 目标

接入 MMLU-Pro test split，并把“官方协议复刻”和“项目自定义 0-shot Direct 变体”严格分开。只有在 prompt、answer extraction 和 aggregation 对同一预测与 pinned upstream runner 逐项一致后，才能将前者标为 protocol-compatible；后者始终单独命名、单独分榜。

### 门禁

- Phase 0 对 HF MIT 与 GitHub Apache-2.0 标记差异有书面结论；
- 还需核对 MMLU、STEM 网站、TheoremQA、SciBench 等上游题目权利，仓库许可证不能自动覆盖所有内容；
- 若仍 pending，只允许隔离 PoC、experimental catalog 和显式 override，不进入 stable、镜像或默认推荐；
- Phase 2 的 selected-only snapshot 与 context preflight 已完成；
- Phase 3 的 `choice@1` 已能配置并复现官方 final-answer extraction。

### 协议 A：`mmlu-pro-5shot-cot-direct-v1`

- 固定 dataset revision 和官方 evaluation code commit，两者分别记录 hash。
- test 12,032 题是计分集；validation 70 题按 14 category 各 5 题作为官方 few-shot demonstrations，不混入计分分母。
- 按 pinned 官方 runner 生成 category-specific 5-shot CoT prompt；validation 的 `cot_content` 只能出现在对应 demonstration，绝不把 test gold/rationale 发给模型。
- answer labels 为 A-J；校验 `answer_index` 与 `answer` 一致。
- answer extractor、最终答案格式和 category aggregation 与 pinned upstream runner 保持一致，并由同一批预测做逐题 parity test。
- `case_id = mmlu-pro-test-<question_id>`，metadata 保留 category、src、split、language、source ID 和 protocol ID。
- source options 顺序保持，不在 converter 中洗牌。
- 禁止 ground-truth-aware retry，尤其不得实现或启用上游新出现的 `retry_wrong`。只有未获得可用响应的传输故障可按平台统一策略处理；当前 Direct LLM 默认仍为零重试。
- context preflight 计算 question + category demonstrations + output budget，任何超限在零调用前拒绝。

### 协议 B：`mmlu-pro-zero-shot-direct-v1`（可选）

- 使用 question + options，要求项目统一的最终 marker，例如 `[ANSWER:X]`；
- scorer 使用 `choice@1` 严格 marker；
- 不使用 validation demonstrations 和 `cot_content`；
- 必须使用独立 dataset/scenario/prompt version 和结果分组，`official_comparability=false`；
- 不与协议 A 或官方排行榜直接比较。

prompt 不同会改变每道 case 的 `input` 和 dataset fingerprint，因此协议 A/B 不能只做同一 dataset 的运行 profile。

### 运行 Profile

每个协议各自提供：

- smoke：56 题，14 category 各 4 题；不足时按确定性规则补齐并写统计；
- regression：560 题，14 category 各 40 题；
- full：12,032 题。

选择使用稳定 case ID 排序和固定 seed，产出的 IDs 直接写入对应 dataset。

### 数据质量检查

- 12,032 test、70 validation、14 category 与 pinned source 全量对账；
- question_id 唯一，options 数量/空值/answer label/index 合法；
- 5-shot demonstration category 和顺序与 pinned runner 一致；
- test prompt 不包含 test gold/cot；
- Unicode/LaTeX 保真；
- normalized prompt 近重复报告；
- 上游题数、字段或清洗变化触发 schema drift review，不能自动接受；
- 对上游缓存预测和自建 adversarial 输出集，答案抽取逐题一致；
- overall 和 category aggregate 对相同预测与 pinned runner 一致。

### 退出标准

- 协议 A 在相同预测上的 prompt/extractor/aggregate parity 达到 100%；
- profiles 覆盖、context preflight 和 fake Provider 全链路通过；
- `retry_wrong` 有显式反向测试保证不可启用；
- 许可证未 approved 时 UI/CLI 明确 experimental/pending，且不能稳定发布或再分发；
- 协议 B 若实现，报告和 Compare 不会与协议 A 合并。

### 回滚

从 catalog 禁用来源，不删除已发布 dataset；新运行创建可由 policy 拒绝，历史 Run 保持可读。

### 估算

5 至 8 工程日，加许可证和上游内容权利复核时间。

## Phase 6：TruthfulQA Binary Direct 适配器

### 目标

以新版二选一设置接入真实性与常见误解评测，不使用开放生成裁判。

### 转换规则

- 固定官方 repository commit 和 `TruthfulQA.csv` hash。
- 使用 Question、Best Answer、Best Incorrect Answer 构造两个选项。
- 每题按 converter seed + source ID 做确定性选项洗牌，避免正确答案固定在 A/B。
- 洗牌算法、seed 和实际映射进入 converter config/metadata。
- prompt template `truthfulqa-binary-direct-v1`，最后一行 `[ANSWER:A|B]`。
- scorer 使用 `choice@1`。
- case ID 基于稳定 source row ID；若上游没有稳定 ID，使用规范化 question hash，并保存 source line。
- metadata 保留 category、type、split、language。
- 名称 `truthfulqa-binary-direct`，不导入生成版 metrics。

### Profile

- smoke：50 题，按 category 比例分层且保证尽可能广覆盖；
- regression：300 题，按 category 比例分层；
- full：817 题。

### 特殊质量门禁

- A/B 正确位置总体接近平衡并有精确统计；
- 同题在同 seed 下顺序稳定；换 seed 必须改变 converter config hash 和 dataset fingerprint；
- Best Answer 与 Best Incorrect Answer 不得相同或规范化后重复；
- 原仓库 canary/污染说明原样记录在 source governance，不复制进 prompt。

### 退出标准

- 固定 revision 得到预期 817 题；
- deterministic shuffle 和 profiles 测试通过；
- 许可 approved 后才进入 stable；
- Direct 结果页面明确“Binary Direct 变体”。

### 回滚

同 MMLU-Pro，禁用 catalog 不删除历史资源。

### 估算

2 至 3 工程日。

## Phase 7：`motte-core-zh` 自编中文题库

### 目标

构建 1,000 道项目自有、可商用、可复现、答案由程序或双人复核得到的中文基础评测题。

### 题型配额

| 题型 | 数量 | scorer | 生成/校验方式 |
| --- | ---: | --- | --- |
| 算术、比例、折扣与聚合 | 160 | numeric | Decimal oracle |
| 日期、时间、时区边界 | 90 | numeric/choice | datetime oracle，固定时区数据 |
| 单位与币种表达换算 | 80 | numeric | 固定换算表，不联网取汇率 |
| 集合、排序、条件逻辑 | 70 | choice/exact | 穷举或独立求解器 |
| 中文工单/意图分类 | 200 | choice | 模板矩阵 + 人工标签复核 |
| 订单/日志/公告 JSON 抽取 | 200 | json_equal | 结构化源对象先生成再渲染文本 |
| 格式和指令遵循 | 120 | exact/json_equal | 程序校验输出约束 |
| 噪声、否定、缺失值和边界 | 80 | choice/json_equal | 对抗模板 + 人工抽检 |
| 合计 | 1,000 |  |  |

### 生成原则

- canonical seed 固定为 `20260919`，generator version 为 `motte-core-zh-generator-v1`。
- 先生成结构化事实和 gold，再渲染题面；不从自然语言反推答案。
- 每个模板族有独立 oracle 和 property tests。
- 不包含实时知识、个人数据、真实客户数据、公司机密和未经授权文本。
- 人名、订单、账号、IP、域名均为明确虚构或保留测试域。
- 分类题定义 mutually exclusive label policy，并包含拒绝/其他类。
- LLM 可用于提出候选模板，但候选不得直接入库；必须人工重写、oracle 校验和审查来源。
- 公开 generator 不等于隐藏测试。需要抗污染时由部署方用私有 seed/私有业务模板生成，结果不得与公共排行榜混称。

### 公开基线与私有 holdout 两轨

- 公开基线：canonical seed、模板和完整复现信息可提交，用于 CI、模型回归和跨团队可复核比较；
- 私有 holdout：由部署方在受控环境使用未入仓 seed 和私有模板生成，禁止把 seed/gold 写入公共日志、前端或代码仓；
- 私有 holdout 必须有访问日志、轮换周期和失密处置；一旦题目进入训练、提示调优或公开报告正文即轮换；
- 两轨分别报告，不把私有 holdout 分数并入公开基准排行榜，也不宣称公开生成器能够证明无污染。

### Profile

- smoke：每模板族至少 2 题，总计 80 至 100；
- regression：500 题，按题型和难度分层；
- full：1,000 题。

### 质量流程

1. 自动 schema/oracle 验证 100%；主生成器与独立 oracle 在至少 10,000 个随机样本上结果 100% 一致；
2. 分层人工复核至少 200 题，覆盖所有模板族、难度和 scorer；发现一个题意/gold 缺陷即修复并重新抽样；
3. 分类和对抗题至少双人复核，分歧必须解决；
4. 检测模板泄漏、题内/跨 profile 精确重复与近重复、答案分布倾斜和无效干扰字段；目标重复为 0；
5. 用至少三个能力梯度明显不同的参考模型做校准，只用于发现坏题，不用多数模型答案修改 gold；若总体出现无解释的低于 10% 地板或高于 95% 天花板，必须复核题目难度和 scorer；
6. 私有 holdout 不参与公开校准模型调参；
7. 修题必须升 generator 或 dataset version，保留修订说明。

### 输出

仓库提交 generator、模板、oracle、小型 fixtures 和统计快照；完整 1,000 题默认在本地生成到 `var/`，是否提交完整 JSONL 由代码仓库体积和内容审查另行决定。默认不提交大生成文件。

### 退出标准

- 数量和配额精确；
- 同 seed/version 产物 fingerprint 稳定；
- 所有 gold 可由 oracle 重算；
- 10,000 样本 oracle 一致性、200 题人工复核和三个参考模型校准均有记录；
- 公开与私有轨隔离，私有 seed 未入仓且有访问/轮换规则；
- 权属声明完成，无真实 PII/密钥/内部数据。

### 回滚

禁用 generator version；已发布 dataset 不改写，错误模板以新 version 修复。

### 估算

6 至 10 工程日，主要取决于人工审题。

## Phase 8：产品化、profile、API/CLI/Web 与统计

### 目标

让扩充题库能被操作者安全发现、导入、筛选、运行和比较，而不是只存在于脚本中。

### CLI

- `sources list/inspect/prepare/verify` 完整帮助和 JSON 输出；
- `direct-llm list` 显示 contract、fingerprint、source、revision、license status、profiles；
- `run --profile smoke|regression|full`；
- profile 与 `--case-ids/--random` 互斥；
- pending/restricted 需要显式 `--accept-license-risk`，并在 receipt 中记录；
- 增加成本估算 dry-run：题数、预计最大输入/输出 tokens，不调用模型。

### API

新增 typed 模型和端点：

- `GET /api/v1/benchmarks/direct-llm/sources`；
- `GET /api/v1/benchmarks/direct-llm/sources/{id}`；
- `GET /api/v1/benchmarks/direct-llm/datasets` 摘要目录；
- `GET /api/v1/benchmarks/direct-llm/cases` 增加 language/category/subject/difficulty/scorer/profile filters；
- run request 支持 profile；
- overview、sources、datasets、builtins、import、cases、runs 的 request/response 全部使用 Pydantic DTO，未知字段 fail closed；
- 移除或逐步替代 `client.ts` 中与 OpenAPI 重复的手写 Direct LLM 类型；
- OpenAPI 和 `schema.d.ts` 无漂移。

服务端远程 prepare：

- 默认不提供或返回 policy disabled；
- 不使用 FastAPI 临时 BackgroundTasks 冒充 durable job；
- 若产品必须 Web 一键下载，另建持久 `DatasetImportJob`、状态机、store/migration/worker 和恢复语义，作为 Phase 8 的独立子项目；
- 未完成 durable job 前，Web 只展示来源状态和已导入数据集，不承诺后台下载。

### Web

新增 `/direct-llm/sources` 或等价入口，实施前按 `apps/web/DESIGN.md` 和 `redesign-existing-projects` 审计：

- 来源表格展示题数、语言、能力、license status、revision、依赖、已导入版本；
- pending/restricted 使用文本和现有语义 token，不自造颜色；
- 数据集页支持 metadata filters 和 profile 预览；
- 操作页 profile 用 segmented/select，不把 profile 做成自由文本；
- 切换 dataset 时清理 selection，显示 fingerprint 短码；
- 结果/对比页展示维度统计和可比性差异；跨 protocol/dataset/selection 不生成伪共同错题；
- Result/Compare 对未知 outcome fail visibly，不把缺失 score 自动显示成“不通过”；
- 大结果集按 case ID map、分页或虚拟化渲染，避免 O(n²) 查找和一次性渲染全部长文本；
- expected 只在现有题目浏览/结果审计界面按既有权限展示，绝不进入 Provider request；若需要隐藏 gold 的角色权限，另立权限项目。

### 可观测性

- source prepare receipt；
- dataset fingerprint、题数和分布；
- run selection profile/IDs hash；
- scorer/parser failure 分布；
- 按 category/subject 的 accuracy/coverage；
- 费用估算与实际 token/cost；
- source/converter/scorer/prompt 版本在结果导出中可见。

### 退出标准

- CLI、API、Web 对同一 dataset/profile 创建的 case IDs 和 manifest 相同；
- 空字段、license gate、unknown source/profile 有统一错误；
- 50/500/full 三档可发起且比较页能判断是否可比；
- `pnpm --dir apps/web test` 和 build 全绿；
- UI 在桌面和移动宽度无文字重叠。

### 回滚

source catalog 和 profile UI 可 feature flag 隐藏；已导入 dataset 和历史 Run 保持可读。

### 估算

5 至 8 工程日；若加入 durable import job，另加 5 至 8 工程日和数据库迁移评审。

## Phase 9：C-Eval 与 CMMLU 受限适配器

### 目标

在非商业/署名/相同方式共享约束下提供可选中文学科评测，不进入默认商用目录。

### 规则

- 来源状态固定为 `restricted-public`；
- CLI 每次首次准备需要显式确认，receipt 保存确认的 license ID 和证据版本；
- Web 默认过滤 restricted，开启后仍明确显示“非商业等限制”；
- 不在仓库、wheel、容器镜像或发布 artifact 中再分发数据；
- 使用 `choice@1` 和版本化中文 prompt；
- 每个学科保留 metadata 和分层 profile；
- C-Eval 与 CMMLU 分开 dataset，不合并平均分；
- 官方 prompt/few-shot 与 Direct 变体分别命名，默认 `official_comparability=false`。

### 验收

- 许可证 gate 无法通过普通 `--yes` 或空字段绕过；
- 未确认时不下载、不落缓存；
- 每学科题数和 answer label 范围匹配固定 revision；
- attribution/citation 可导出。

### 估算

每个来源 2 至 4 工程日，加许可证审核。

## Phase 10：IFEval、LongBench v2 与高级能力

### 10.1 IFEval

- 不把 541 个 prompt 当作 `no_expectation` 后混入 accuracy；
- 实现独立 `instruction_rules` scorer/plugin，复用或等价实现官方规则；
- 每个 instruction rule 有版本、参数和 per-rule evidence；
- 同时报告 prompt-level strict/loose 和 instruction-level strict/loose，不能压成单一 Direct accuracy；
- 先验证官方 evaluator 的许可证、依赖和跨语言行为。

### 10.2 LongBench v2

- 503 道选择题可复用 `choice@1`，但需要 context length/token preflight；
- 导入保存原始 length/domain/subdomain/difficulty metadata，并对 503 题、6 大类和各长度/难度分布全量对账；
- 运行创建前用目标 Provider tokenizer 能力检查上下文；保守估算只能用于预警，不能支撑官方 full 资格声明；
- 超限题在创建期拒绝，禁止静默截断。若另建 eligible-only profile，必须单独命名且不得发布 LongBench v2 overall；
- 只有覆盖全部 503 题的 direct protocol 才能发布 overall，失败或不适配不得从分母静默消失；
- full/head-tail/no-context/CoT/RAG 都是不同 protocol，任何截断策略必须显式版本化并分榜；
- 展示最大、P50/P95 输入 tokens 和估计费用，运行前必须满足批准预算；
- 对相同预测，answer extractor 与 overall/难度/长度 aggregate 必须和 pinned upstream runner 一致；
- 数据卡 Apache、repo MIT 和长文档/书籍/代码的上游权利未核清前保持 pending。

### 10.3 继续后置

- HumanEval/MBPP：等待安全、资源受限、可审计的不可信代码沙箱；
- MT-Bench/AlpacaEval：等待版本化 judge model、rubric、bias 校准和成本治理；
- 多轮/工具任务：等待 case contract 支持消息序列、tool traces 和专用 execution backend。

### 退出标准

每类高级任务使用独立 plugin/metric，不伪装成现有 Direct LLM v2 accuracy。

## Phase 11：发布硬化、迁移与运维

### 目标

把 M1-M4 的能力以可升级、可回退、可审计方式发布。

### 工作包

1. 全量 contract/evaluator/sdk/storage/api/cli/runtime/web 测试。
2. 12K 合成 dataset 容量、分页、Run payload 和 rescore 性能测试。
3. source 下载故障、缓存损坏、schema drift、磁盘不足和中断恢复测试。
4. SQLite/PostgreSQL 原子发布一致性测试。
5. v1 历史 fixture 升级/读取/重评分兼容测试。
6. 生成 OpenAPI 和 TypeScript 类型，校验无漂移。
7. 更新 Direct LLM 操作指南、安装可选依赖、来源治理、备份恢复、回滚和已知限制。
8. 使用 fake provider 做所有新 scorer 和三个首批数据集的端到端测试。
9. 用小 profile 做有限 live smoke，显式记录模型、参数、费用、run IDs 和结果；不在 CI 执行。
10. 安全审查：URL allowlist、redirect、archive、路径、hash、secret、PII、日志脱敏。
11. 发布说明明确 Direct 变体不可直接对齐官方 leaderboard。

### 必跑门禁

```bash
uv run pytest -q -m "not live"
uv run ruff check .
uv run mypy packages/contracts
uv run python -m compileall -q packages apps
make openapi
git diff --exit-code -- api/openapi.json apps/web/src/api/schema.d.ts
pnpm --dir apps/web exec tsc --noEmit
pnpm --dir apps/web test
pnpm --dir apps/web build
docker compose -f infra/docker-compose.yml config -q
make check
```

若新增 optional dependency，还需在“最小安装”和“带 data-sources extra”两个环境测试清晰错误与正常转换。

### 发布退出标准

- 所有 P0/P1 阶段完成；
- stable 来源许可证已批准；
- pending/restricted 不会默认下载；
- v1/v2 兼容矩阵发布；
- 已知限制、恢复、回滚和禁用来源步骤可操作；
- live smoke 记录不含密钥和敏感正文。

## 11. 测试策略

### 11.1 测试金字塔

| 层级 | 内容 |
| --- | --- |
| Contract | v1/v2 schema、unknown keys、fingerprint、profiles、source spec |
| Evaluator | parser/compare/aggregate 的纯函数与 property tests |
| SDK | fetch/cache/convert/publish/resolve/score 编排 |
| Storage | 原子 dataset+scenario、并发冲突、SQLite/PostgreSQL 一致性 |
| API | typed boundary、错误码、分页/过滤、license policy |
| CLI | stdout JSON、stderr error、exit code、cache/prepare 幂等 |
| Runtime | prompt 不泄漏 gold、selected-only snapshot、retry/rescore |
| Web | 多 dataset selection、profile、许可状态、维度结果、响应式布局 |
| Integration | Source fixture -> dataset -> scenario -> Run -> Worker -> ScoringPass -> Report |
| Live | 小 profile、人工触发、费用受控，不进 CI |

### 11.2 固定 fixtures

仓库只提交小型、许可清晰的 fixtures：

- 每个公开来源 3 至 10 条最小字段 fixture；若许可证不允许再分发，使用同 schema 的合成 fixture；
- scorer 对抗输出 fixture；
- 12K 合成 metadata-only/cases generator，不提交大文件；
- v1 历史 dataset/run/scoring pass fixture；
- source manifest/hash mismatch fixture。

### 11.3 必须新增的回归测试文件

建议新增或扩展：

- `tests/contract/test_direct_llm_v2.py`
- `tests/contract/test_dataset_sources.py`
- `tests/evaluators/test_direct_llm_v2.py`
- `tests/sdk/test_direct_llm_sources.py`
- `tests/sdk/test_mmlu_pro_adapter.py`
- `tests/sdk/test_truthfulqa_adapter.py`
- `tests/sdk/test_core_zh_generator.py`
- `tests/storage/test_atomic_dataset_publish.py`
- `tests/api/test_direct_llm_sources_api.py`
- `tests/integration/test_direct_llm_v2_run.py`
- `tests/runtime/test_direct_llm_v2_smoke.py`
- `apps/web/tests/directLlmSelection.test.ts`
- `apps/web/tests/directLlmSources.test.tsx`

### 11.4 性能与容量断言

不使用易抖动的绝对耗时作为唯一 CI 门禁，使用结构性断言：

- selected-only snapshot case 数量等于 selected；
- Run list 不含 selected case 正文；
- catalog summary 不含 cases；
- 50 题 Run JSON 字节数设置合理上限并与 12K dataset 对比；
- repeated import 不新增版本；
- source cache 命中不发网络请求；
- case endpoint 始终尊重 limit 上限。

本地基准记录导入时间、峰值内存和数据库增长，作为 release evidence，不强绑不同 CI 机器的秒数。

## 12. 安全、隐私与供应链

1. 只允许 HTTPS，除测试 transport；redirect 最终域名必须满足 allowlist/policy。
2. URL、revision 和 cache path 不由未经校验的字符串直接拼出可逃逸路径。
3. 不加载 pickle、任意 Python、HF remote code 或数据集脚本。
4. artifact 和 conversion manifest 都有 hash；缓存命中仍核验 hash。
5. 下载、转换和发布分阶段，只有全部成功才原子发布。
6. source 文本不进入错误日志全文；日志只记 source ID、revision、hash 和受控错误摘要。
7. generated-internal 禁止真实客户数据；业务语料必须模板化或完成匿名化与授权审查。
8. expected 不进入 Provider request、trace message 或面向模型的 prompt。
9. Web/API 不接收 API key 等 secret 字段，沿用现有 `_reject_secret_fields`。
10. 大题面设置输入字节、case 数、单题长度和总转换输出上限，防止磁盘/内存耗尽。
11. regex v1 数据保持兼容；新公共来源不使用未受控 regex。
12. 数据源下架只阻止新 fetch/import；历史不可变证据按保留策略处理，不静默删除。

## 13. 风险登记

| 风险 | 严重度 | 触发信号 | 缓解 | 阻断阶段 |
| --- | --- | --- | --- | --- |
| 许可证声明冲突 | 高 | repo 与数据卡 license 不同 | 人工复核，pending 默认禁用 | Phase 5/10 stable |
| Run 快照爆炸 | 高 | 小 Run 携带完整 12K cases | v2 selected-only snapshot | Phase 5 前 |
| 上下文不适配或静默截断 | 高 | Provider 付费后拒绝/截断 | tokenizer-aware preflight，零调用前拒绝 | Phase 5 前；Phase 10 加强 |
| ground-truth-aware retry 污染分数 | 高 | 错答被自动重试 | 禁止 `retry_wrong`，仅按统一传输故障语义处理 | Phase 5 前 |
| scorer 误解析 | 高 | 正文出现 label 被当答案 | strict final marker + invalid_format | Phase 5 前 |
| 导入半发布 | 高 | dataset 存在、scenario 缺失 | storage 原子事务 | Phase 4 前 |
| 版本幂等错误 | 高 | 同 cases 不同 scorer 冲突 | dataset fingerprint | Phase 4 前 |
| 聚合口径漂移 | 高 | overview/report 不一致 | 单一 aggregate + cross-view tests | Phase 2 前 |
| 上游 schema 漂移 | 中高 | 题数/字段变化 | pinned revision + schema snapshot | 每个 adapter |
| 数据污染 | 中高 | 模型记忆公开答案 | 不宣称抗污染，保留私有业务集能力 | 发布说明 |
| 选择样本偏斜 | 中 | random 结果学科不均 | fixed stratified profiles | Phase 8 |
| 可选依赖过重 | 中 | pyarrow 增大安装 | optional extra + 清晰错误 | Phase 4 |
| Web 跨库混选 | 中高 | unknown IDs/错误 dataset | fingerprint-bound selection | Phase 1 |
| 长上下文高费用 | 高 | 超限/高 token | preflight、profile、成本估算 | Phase 10 |
| 自编题标签错误 | 高 | oracle/人工不一致 | structure-first + oracle + 双人复核 | Phase 7 |
| 官方分数误用 | 中高 | 用户与 leaderboard 直接比较 | `official_comparability=false` + UI 文案 | 每个来源 |
| 通用资源 API 大响应 | 中高 | `/datasets` 返回完整 cases | summary endpoint + Web 改用摘要 | Phase 2 |

## 14. 提交与 PR 组织

建议按可独立验证、可独立回滚的提交拆分：

1. `docs(plan): define direct llm dataset expansion roadmap`
2. `docs(data): define source governance and direct llm v2 adr`
3. `fix(direct-llm): align v1 aggregation and import identity`
4. `fix(storage): publish dataset and scenario atomically`
5. `fix(web): bind direct llm selections to datasets`
6. `feat(contracts): add direct llm v2 dataset contracts`
7. `feat(runtime): snapshot selected direct llm cases only`
8. `feat(eval): register deterministic direct llm v2 scorers`
9. `feat(data): add versioned source acquisition pipeline`
10. `feat(data): adapt mmlu pro direct variant`
11. `feat(data): adapt truthfulqa binary direct variant`
12. `feat(data): generate motte core zh benchmark pack`
13. `feat(api): expose typed dataset source and profile APIs`
14. `feat(web): manage direct llm sources and profiles`
15. `feat(report): aggregate direct llm dimensions and comparability`
16. `docs(ops): document dataset sources upgrades and rollback`

Phase 9/10 使用独立 PR，不与首批 GA 强行绑定。

每个 PR 必须包括：

- 行为变化；
- schema/version 影响；
- 数据/许可证影响；
- 测试证据；
- 回滚方式；
- 已知限制；
- 是否产生真实下载或模型费用。

## 15. 依赖与并行安排

### 15.1 不可并行关键路径

- Phase 1 必须先于 v2 和任何大型来源。
- Phase 2 必须先于 Phase 3-8。
- Phase 3 和 Phase 4 可在 Phase 2 contract 冻结后并行。
- Phase 5/6/7 必须依赖 Phase 3 和 Phase 4。
- Phase 8 依赖至少一个公开来源和 core-zh 的真实数据形状。
- Phase 11 最后执行。

### 15.2 可并行工作

- Phase 0 许可证复核可与 Phase 1 工程修复并行，但未批准来源不能 stable。
- Phase 5、6、7 的 converter/generator 可由不同负责人并行。
- Web source catalog 可在 API schema 冻结后与 adapter 开发并行。
- 文档、运维和 fixture 可随各阶段持续更新，不在最后一次性补齐。

### 15.3 角色建议

| 角色 | 责任 |
| --- | --- |
| Contract/Eval owner | v1/v2、scorer、metrics、compatibility |
| Data pipeline owner | source registry、fetch/cache、converter、fingerprint |
| Storage/API owner | 原子发布、typed endpoints、摘要/分页 |
| Web owner | source/profile/selection/结果维度 UI |
| Data QA owner | 题数、分布、抽检、oracle、污染记录 |
| License reviewer | 许可证证据、商业/再分发状态、下架决策 |

一人实施时按关键路径顺序推进，不用并行为理由跳过阶段门禁。

## 16. Definition of Done

首批扩充只有满足以下全部条件才算完成：

1. 本地 `main` 基线和计划中记录的版本一致，实施期间每阶段基线可追溯。
2. GSM8K 未被转换、替代或改口径。
3. 现有 23 题 builtins 和 Direct LLM v1 历史资源保持兼容。
4. Phase 1 的统计、fingerprint、原子发布、publication audit、CLI suite 隔离、Web 选择和 Compare 缺陷全部关闭。
5. Direct LLM v2 使用独立 plugin/version，所选题快照不包含未选题；context 不适配在零调用前拒绝且无静默截断。
6. `choice/numeric/json_equal` 有完整解析与对抗测试。
7. source pipeline 固定 revision/hash，CI 零网络，失败不半发布，任意报告可离线复核。
8. TruthfulQA Binary 和 `motte-core-zh` 达到 stable；MMLU-Pro 只有在许可证 approved 且 5-shot CoT 协议与 pinned upstream runner 对相同预测 100% 一致后才计入 stable，否则作为 experimental 不阻塞其余发布。
9. 每个数据集有 smoke/regression/full profile、metadata 分布和 dataset fingerprint。
10. CLI/API/Web 使用同一 SDK/contract，不各自实现转换或抽样。
11. 结果中显示 Direct 变体、来源、许可、协议、版本、selection hash 和 scorer evidence；不同协议不合并分榜。
12. 所有门禁通过，运维、升级和回滚文档完成。

## 17. 实施后目标题库规模

许可证批准条件满足时：

| 数据集 | 目标题数 | 稳定级别 |
| --- | ---: | --- |
| 现有 builtins | 23 | smoke only |
| MMLU-Pro 5-shot CoT Direct test | 12,032 | stable 或 experimental，取决于许可与协议 parity 结论 |
| TruthfulQA Binary Direct | 817 | stable |
| `motte-core-zh` | 1,000 | stable |
| 首批合计 | 13,872 | 其中至少 1,817 + 23 不依赖 MMLU-Pro 许可结论 |

C-Eval、CMMLU、IFEval、LongBench v2 不计入首批 Definition of Done，防止许可证、高级 evaluator 和长上下文成本拖累基础能力交付。

## 18. 下一步

计划获批后按以下顺序开始：

1. 完成 Phase 0 ADR 和来源准入登记；
2. 建立 Phase 1 的失败测试，先复现分母、版本身份、半发布、suite 隔离、跨库选择、Compare 和空字段问题；
3. 逐项修复并保持 v1 兼容；
4. 冻结 Direct LLM v2 contract 后再并行开发 scorer、context preflight 和 source pipeline；
5. 先用合成 12K dataset 做容量验收，再接触真实大型数据；
6. 首先完成 `motte-core-zh` 公开试点和私有 holdout 流程，再推进 TruthfulQA；MMLU-Pro 只在许可与 5-shot CoT parity 门禁通过后升 stable；
7. 每个 Phase 完成时更新 `docs/PROGRESS.md`，不要等到最终发布再补记录。

## 19. 上游参考入口

以下链接用于 Phase 0 固定具体 revision 和保存许可证证据；实现时不能把浮动页面本身当作不可变 artifact：

- MMLU-Pro GitHub：https://github.com/TIGER-AI-Lab/MMLU-Pro
- MMLU-Pro Hugging Face：https://huggingface.co/datasets/TIGER-Lab/MMLU-Pro
- TruthfulQA：https://github.com/sylinrl/TruthfulQA
- C-Eval：https://github.com/SJTU-LIT/ceval
- CMMLU：https://github.com/haonan-li/CMMLU
- IFEval evaluator：https://github.com/google-research/google-research/tree/master/instruction_following_eval
- IFEval dataset card：https://huggingface.co/datasets/google/IFEval
- LongBench v2 GitHub：https://github.com/THUDM/LongBench
- LongBench v2 dataset card：https://huggingface.co/datasets/THUDM/LongBench-v2

每个 SourceSpec 最终保存的是具体 commit/HF revision、artifact URL、SHA-256 和许可证快照，不依赖上述链接当前内容保持不变。
