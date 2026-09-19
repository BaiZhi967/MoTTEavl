# ADR: Direct LLM Dataset v2 与受管来源边界

- 日期：2026-09-19
- 状态：已确认约束

## 背景

Direct LLM 需要从仓库内置的 23 道 smoke 样例扩展到可复现的公开题库和项目自编题库。现有 v1 契约、运行快照和评分器仍承载历史资源与 Run，不能通过原地升级破坏；大型来源还会放大快照体积、许可证、供应链和隐式联网风险。

本 ADR 只冻结 Phase 0 的边界。具体 v2 schema、转换器和 CLI 实现由后续阶段完成。

## 决策

### GSM8K 不转换

GSM8K 继续作为独立 suite，保留其官方来源身份、prompt、Decimal scorer、统计口径和报告语义。不得把 GSM8K 转换、复制或包装成 Direct LLM 数据集，也不得用 Direct LLM 的扩容工作替代 GSM8K 的版本与来源治理。

### Direct LLM v1 与 v2 并存

- `direct-llm@1` 保持可读、可运行和可重评分；既有 dataset、scenario、Run、ScoringPass 及内置 23 题不改写。
- `direct-llm@2` 使用独立契约、插件与评分器版本。未知显式版本必须拒绝，不得回落到 v1。
- 缺少 `plugin_version` 的历史 Direct LLM scenario 按 v1 解释；v2 scenario 必须显式声明版本。
- 修题、换 prompt、换 scorer、换来源证据或转换配置均产生新的不可变数据集版本，不能覆盖已发布资源。
- 第三方题库默认命名为 MoTTEavl 的 Direct 变体。除非逐项证明协议等价，不得宣称与官方排行榜同分或直接可比。

### v2 Run 只冻结所选题

v1 Run 继续保留完整 dataset 快照以维持历史兼容。v2 在创建 Run 时先把 profile 或抽样解析为确定的 case ID 顺序，然后仅冻结：

- dataset identity、fingerprint、cases hash、总题数及必要的 eval/provenance 摘要；
- selection 模式、profile/seed、所选 case ID 及其 hash；
- 本次执行所选的完整 cases，顺序与执行顺序一致。

v2 Run 不复制未选题。provider manifest 只投影执行需要的 prompt，不包含 expected、scorer config 或完整 provenance；重试和重评分复用同一不可变 selected-only snapshot，不重新抽样、不读取可变上游来源。

### 数据源默认拒绝

所有外部来源必须先登记静态 SourceSpec，并受来源治理文档约束。来源或登记存在以下任一情况时，fetch、convert、import、publish 和新 Run 默认拒绝：

- 来源未知、状态未知或状态为 `pending`；
- 状态为 `restricted` 但没有与当前用途匹配的显式批准；
- 未固定不可变 upstream revision；
- artifact URL、SHA-256、字节数或格式缺失/不匹配；
- 数据许可证与代码许可证边界、证据或人工复核结论缺失；
- converter、prompt、scorer 或配置版本未知；
- schema 漂移、依赖缺失或 official comparability 结论缺失。

`approved-internal` 只适用于项目自有来源的限定范围，不等同于第三方公开分发许可。任何 override 都必须是显式操作并写入 receipt/audit，不得修改全局审查状态或形成默认放行。

### CLI 是默认联网入口

首批受管来源的联网动作只能由操作者显式调用 CLI 发起。服务端远程下载默认关闭；Web/API 只提供只读来源目录或本地已发布资源操作，不能在列表、浏览、创建 Run、执行或评分时隐式联网。Worker、重试、重评分和 CI 始终不得下载题库。

CLI 获取链路必须固定 revision、校验 artifact SHA-256/字节数、限制协议/超时/大小，并写结构化 receipt。Parquet 只通过显式可选依赖（计划采用 `pyarrow`）解析；禁止 `trust_remote_code`、pickle 和在线 rows API 回退。

## 影响

- v1 兼容成本会保留，但不会让新契约改变历史评分或快照语义。
- v2 Run 存储量随所选题数增长，而不随完整数据集规模线性增长。
- SourceSpec 可以先以草案登记，但证据空缺会阻止执行；登记本身不是许可批准。
- pending/restricted 来源可以开发隔离的转换 PoC，但不能越过许可、revision、hash、协议和发布审计门禁进入 stable。
- 来源下架不会静默改写或删除历史不可变资源；新获取、新发布和新运行按治理流程关闭，历史证据继续可审计。

具体状态、人工复核职责、证据要求与下架流程见 [数据集来源治理](../../../operations/dataset-source-governance.md)。
