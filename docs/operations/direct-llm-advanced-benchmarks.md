# Direct LLM 高级基准运维与限制

本文说明 C-Eval、CMMLU、IFEval 和 LongBench v2 的当前实现边界。它是运维限制说明，不是许可证批准、官方成绩声明或数据再分发授权。通用来源状态、人工复核、可信 override、缓存隔离和下架要求以[数据集来源治理](dataset-source-governance.md)为准。

## 当前状态总览

| 来源 | SourceSpec 状态 | 当前可用能力 | 当前禁止能力 | 官方可比性 |
| --- | --- | --- | --- | --- |
| C-Eval | `restricted` | 对操作者已合法取得并保存在本地的行做离线实验转换 | 受管 fetch、import、publish；默认 Web 展示；数据再分发 | `false` |
| CMMLU | `restricted` | 对操作者已合法取得并保存在本地的行做离线实验转换 | 受管 fetch、import、publish；默认 Web 展示；数据再分发 | `false` |
| IFEval | `pending` | 使用项目自研 preview 规则和四项独立指标做离线研究 | 官方 ready、Direct plugin 注册、`accuracy` 指标、对外发布 | 未建立 |
| LongBench v2 | `pending` | 对已固定的本地 503 题快照做 full-verbatim 转换、完整性聚合和保守 preflight | 静默截断、以子集冒充 overall、正式发布或官方可比性声明 | `pending` |

所有来源的网络入口、发布入口和 Web 可见性都由治理状态控制。adapter 或 evaluator 能在本地运行，不表示来源已经获准获取、导入、发布或用于新 Run。

## C-Eval 与 CMMLU

实现位于 [`restricted_chinese.py`](../../packages/sdk-python/motte_sdk/adapters/restricted_chinese.py)，分别暴露 `convert_ceval_direct` 和 `convert_cmmlu_direct`。两者是纯转换函数：只读取调用方传入的 `subject -> rows` 映射、固定 revision、artifact 元数据和许可证据，不执行网络请求、缓存导入或资源发布，也不改变 SourceSpec 状态。

### 允许的本地实验

调用方必须先在项目外按适用条款合法取得数据，并确保数据只留在获准的本地环境。转换时必须提供：

- 显式 `schema_version=1`；每行字段严格为 `id`、`question`、`A`、`B`、`C`、`D`、`answer`；
- 全局唯一的非空 `id`，非空题面和四个选项，以及大写 `A` 到 `D` 的答案；
- 40 位小写十六进制 revision；绑定该 revision 的 credential-free HTTPS artifact URL、原始 SHA-256 和字节数；
- `status=restricted` 的许可证据快照，包括非空 declared IDs、证据版本、证据 URL、citation 和 attribution。

输出使用中文单轮选择题 prompt，只允许模型返回 `[ANSWER:X]`；评分器为 `choice@1`，`allow_bare_final_label=false`，`max_retries=0`。case metadata 保留 `subject`、原始 `source_id`、`language=zh`、split、category、tags 和独立 template family。

### 必须保持隔离

C-Eval 和 CMMLU 使用不同的 dataset name、source ID、prompt version、converter ID、case ID 命名空间和 dataset fingerprint。不得合并两者的 cases、profiles 或报告后再标成任一官方结果；需要对照时，只能并列展示两个独立报告。两者的 converter config 都固定记录：

- `governance.status=restricted`；
- `distribution_scope=restricted`；
- `publishable=false`、`stable_eligible=false`；
- `data_redistribution_allowed=false`；
- `official_comparability=false`。

本地输出仍包含转换后的题目，因此不得提交到仓库、上传到共享 artifact、放入通用 stable 目录或向未获授权者再分发。可以保存不含题目正文且政策允许保留的 hash、计数和审计证据。

当前 CLI/SDK 没有接入 restricted 来源的 trusted approval verifier。调用方自报 actor、ticket、`override_verified` 或 adapter 成功均不构成批准；受管 fetch、import 和 publish 必须保持阻断。Web 的只读来源目录默认隐藏 `restricted` 项，操作者可手动选择“显示受限来源”查看静态登记，但页面不提供获取或准备操作；来源目录 API 可继续用于审计。

来源登记和上游链接：

- [C-Eval SourceSpec](../../datasets/direct-llm/sources/ceval.json)：[仓库/主页](https://github.com/SJTU-LIT/ceval)；
- [CMMLU SourceSpec](../../datasets/direct-llm/sources/cmmlu.json)：[仓库/主页](https://github.com/haonan-li/CMMLU)。

### Citation 与 attribution 导出

不得从仓库根许可证、数据卡标签或 adapter 默认值推导 citation/attribution。导出时优先使用与实际 revision 对应且已经复核的证据：

| 载体 | Citation 路径 | Attribution 路径 | 辅助证据 |
| --- | --- | --- | --- |
| SourceSpec | `links.citation` | `license.attribution` | `license.data.declared_ids`、`license.data.evidence_urls`、`license.review_notes` |
| 转换 dataset | `provenance.converter.config.license_evidence.citation` | `provenance.converter.config.license_evidence.attribution` | 同一快照的 `declared_ids`、`evidence_version`、`evidence_urls` |
| managed receipt | `dataset.provenance.converter.config.license_evidence.citation` | `dataset.provenance.converter.config.license_evidence.attribution` | receipt 的 source、revision、artifact manifest hash 和 dataset fingerprint |

当前两个 SourceSpec 的 `links.citation` 都是 `null`，不能补写猜测值。若调用链把转换 dataset 包入 receipt，应从 receipt 内嵌的不可变 dataset 读取上述字段，并同时导出 source ID、revision 和 fingerprint，不能只复制一段 citation 文本。

## IFEval Preview

实现位于 [`ifeval.py`](../../packages/evaluators/motte_eval/ifeval.py)。它不导入上游 IFEval 代码或数据，不注册 Direct LLM plugin，只提供项目自研的封闭 preview 规则 registry：`contains@1`、`starts_with@1`、`ends_with@1`、`json_object@1` 和 `max_words@1`。这些规则用于验证本地报告结构和聚合语义，不代表与官方 evaluator 等价。

报告固定为 `suite=ifeval-preview`、`official=false`、`source_status=pending`，并输出四个相互独立的指标：

1. `prompt_level_strict`；
2. `prompt_level_loose`；
3. `instruction_level_strict`；
4. `instruction_level_loose`。

每项分别记录 selected、judged、passed、rate 和 coverage。不得把任一项改名或折算成 `accuracy`；缺失规则证据必须保留为 `not_attempted`，不能移出分母记录。

Preview 可以处理子集，但 `IFEVAL_FULL_CASE_COUNT` 固定为 541；任何声称 full 的报告必须覆盖全部 541 个 case。即使 541 题和全部规则都完成，报告仍保持 preview/pending，不能据此取得官方 ready。

`require_official_ifeval_ready` 只有在受信 composition root 注入 trusted verifier，并由其明确验证以下全部证据后，才会返回绑定本次 evidence payload 的 ready descriptor。这个边界不宣称抵抗同一 Python 解释器内的任意代码执行、monkeypatch 或反射篡改；需要抵抗该攻击模型时，必须改用外部私钥签名的 attestation，而不是进程内 bool verifier：

- `source_id=ifeval`；
- 40 位固定 source revision；
- 许可证批准证据 hash；
- 40 位固定 evaluator revision；
- evaluator parity 证据 hash。

当前运行环境没有接入该 trusted verifier。[IFEval SourceSpec](../../datasets/direct-llm/sources/ifeval.json)中的数据/evaluator 许可证、固定 revision、artifact 和 parity blocker 仍未解除，因此不得标记官方 ready、不得发布，也不得在 Direct LLM 报告中伪装成普通 accuracy。上游证据入口仅供复核：[数据卡](https://huggingface.co/datasets/google/IFEval)与[官方 evaluator 仓库](https://github.com/google-research/google-research/tree/master/instruction_following_eval)。

## LongBench v2

实现位于 [`longbench_v2.py`](../../packages/sdk-python/motte_sdk/adapters/longbench_v2.py)。当前协议是 `longbench-v2-full-direct-v1`，只接受官方格式的 12 个字段：

```text
_id, domain, sub_domain, difficulty, length, context,
question, choice_A, choice_B, choice_C, choice_D, answer
```

`context` 是必填字段。`full-verbatim-v1` 会把完整 context 原样放入 prompt，随后放置 question 和源顺序的 A-D 选项；不得截断、head-tail、移除 context、改成 RAG，或在同名协议下创建 eligible-only 变体。

### 完整集合与报告

full 集合必须正好有 503 个唯一 case，并覆盖 6 个 domain。profile 计数固定为 smoke 50、regression 200、full 503，三者使用稳定 domain round-robin 前缀；调用方不能用自定义行数或 domain 数绕过。聚合器只接受 exact 503-case full profile，并输出：

- overall selected、judged、correct、not_attempted、accuracy 和 coverage；
- `by_domain`、`by_length`、`by_difficulty`；
- 每题 outcome 和明确的 `selected_cases` 分母。

只有 selected=503、judged=503 且 not_attempted=0 时，聚合结果的 `overall_publishable` 才为 `true`。该字段只表示“503 题结果完整，可形成 overall 报告”，不表示数据来源获准发布，也不表示官方可比。SourceSpec 和 dataset provenance 仍是 `pending`，`official_comparability.status=pending`；治理发布门禁必须继续阻断。

### Context preflight 与费用估算

模型档案固定 `context_window` 时，运行准备阶段使用 `utf8-bytes-plus-structure-v1`：以 UTF-8 字节数加请求/消息结构余量构造 tokenizer-independent 保守上界，再预留输出 token。若上界超过模型档案的 `context_window`，在 provider 构造和付费执行之前返回 `CONTEXT_WINDOW_EXCEEDED`；禁止让供应商隐式截断。未固定 `context_window` 时不会生成这项 preflight，不能把缺少拒绝误解为已经通过。

Direct LLM dry-run 可在上述 preflight 和有效 price table 同时存在时，用所选题数、最大输入上界和输出预留量计算 `estimated_cost_upper_bound`。它是保守预算提示，不是实际 tokenizer 计数、精确账单或官方模型资格证明。当前 converter config 明确记录 `official_tokenizer_parity=false`；要获得官方资格，仍需针对具体模型/版本固定 tokenizer、context window、输出预留、官方 runner/extraction parity，并逐题证明完整 prompt 可进入上下文。

许可证同样未 ready。[LongBench v2 SourceSpec](../../datasets/direct-llm/sources/longbench-v2.json)记录的数据卡 Apache-2.0 声明不能替代 context 中长文档、书籍、代码仓库和其他组成内容的上游权利审查；仓库代码许可证也不能自动覆盖数据。状态必须保持 `pending`。复核入口：

- [LongBench v2 数据卡](https://huggingface.co/datasets/THUDM/LongBench-v2)；
- [LongBench 仓库](https://github.com/THUDM/LongBench)；
- [官方 LongBench README](https://github.com/THUDM/LongBench/blob/main/README.md)。

## 明确后置的能力

以下能力不属于当前 Direct LLM 高级基准可用范围，也没有隐含上线日期：

- **HumanEval/MBPP**：需要可审计、资源受限、默认断网的不可信代码执行沙箱，以及依赖固定、超时、进程、文件系统和结果证据治理；当前禁止通过普通 Direct adapter 执行提交代码。
- **Judge benchmark**：需要固定 judge model/profile、prompt、rubric、采样策略、bias/一致性校准、成本预算和独立证据留存；在这些门禁完成前不使用 LLM-as-a-Judge 生成正式指标。
- **多轮/工具任务**：需要 case contract 表达消息序列、tool schema、tool result 和轨迹，需要专用 execution backend、权限策略、sandbox 与失败恢复；不得把多轮或工具轨迹扁平化成单轮 prompt 后冒充等价评测。

## 如何禁用或下架

发现许可证变化、上游撤回、hash 异常、数据污染、PII/敏感内容、schema 漂移或协议错误时，立即停止该来源的新转换和新 Run，并按[数据集来源治理：下架流程](dataset-source-governance.md#下架流程)执行。最低动作包括：

1. 将 SourceSpec 保持或改为 `pending`/`restricted`，记录 blocker 和证据；
2. 阻断 fetch、convert、import、publish 和新 Run，隔离未发布缓存；
3. 记录受影响 revision、artifact hash、dataset/scenario refs、报告和下游副本；
4. 不静默改写或复用不可变资源，不删除允许保留的审计证据；
5. 只有新的人工结论、完整固定证据和可信批准通过后，才以新资源版本恢复。

紧急情况下先阻断动作，再补齐调查。任何 adapter 成功、preview 指标、full coverage、preflight 通过或费用可估算，都不能替代下架与恢复审批。
