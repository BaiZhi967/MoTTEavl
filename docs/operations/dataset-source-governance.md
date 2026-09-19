# 数据集来源治理

本文规定 Direct LLM 受管来源从登记、人工复核、固定上游版本、获取、转换、发布到下架的操作边界。它是工程准入政策，不构成法律意见；许可证结论不明确时必须保持阻断并交由有授权的人工复核者判断。

静态来源登记位于 [`datasets/direct-llm/sources/`](../../datasets/direct-llm/sources/)。架构边界见 [Direct LLM Dataset v2 ADR](../superpowers/specs/adr/2026-09-19-direct-llm-dataset-v2.md)。

## 状态与动作

公开来源以 `approved`、`restricted`、`pending` 为基础状态。项目自有来源可使用范围限定的 `approved-internal`，其含义是内部权属/隐私范围已确认，不代表取得第三方公开再分发权。

| 状态 | 含义 | 默认可见性 | 获取/转换 | 发布与新 Run |
| --- | --- | --- | --- | --- |
| `approved` | 人工已按固定 revision 审完数据许可、代码许可和用途 | 可见 | CLI 可按登记执行 | 仅在其他技术门禁通过后允许 |
| `approved-internal` | 项目自有生成器或数据仅获内部范围批准 | 内部可见 | 只允许登记范围内本地生成/读取 | 只允许登记范围；不得推导公开分发许可 |
| `restricted` | 非商业、署名/相同方式共享、访问条款或其他用途限制存在 | 默认隐藏或标记受限 | 默认拒绝；显式用途批准后方可执行 | 默认拒绝；不得进入通用 stable 目录 |
| `pending` | 证据缺失、声明冲突、尚未复核或技术边界未确认 | 可在审计目录显示 | 拒绝 | 拒绝 |

状态只描述治理结论，不代表转换器已经可用。`approved` 来源仍必须通过 revision、artifact hash、schema、converter、publication audit 和协议可比性门禁。未知状态、未知来源或不完整登记一律按 `pending` 处理。

## 人工复核职责

来源/许可证复核者负责：

1. 针对即将固定的具体 upstream revision 阅读数据卡、仓库许可证、数据文件声明、下载页条款和必要的论文说明。
2. 区分数据许可证、代码许可证、论文许可和网站服务条款，不用仓库根许可证自动替代数据许可。
3. 记录商业使用、再分发、署名、相同方式共享、访问鉴权、地域/用途和衍生数据限制。
4. 处理不同证据间的冲突；无法裁定时维持 `pending`，不得填写推测的 SPDX 标识。
5. 核对来源中潜在 PII、敏感内容、canary、训练数据污染声明及项目自编数据的权属/隐私清单。
6. 填写 reviewer、`reviewed_at`、证据 URL、结论和适用范围；证据变更或 revision 变化后重新复核。
7. 对 `restricted` 来源逐次确认具体用途和操作者授权；工程 override 不能代替本职责。

工程维护者负责固定 revision、计算 artifact SHA-256/字节数、验证 schema、维护确定性 converter 和生成 receipt。发布批准者核对人工许可结论与工程证据均完整后，才允许原子发布 dataset、scenario 和 publication event。三类职责可以由同一人承担，但每次审计记录必须明确其所执行的角色和结论。

## SourceSpec 证据

所有 `sources/*.json` 使用一致字段，并至少记录：

- 来源 id、名称、描述、tier、治理状态和允许的发布范围；
- homepage、repository、dataset card、citation URL；未知值明确写 `null`；
- revision 类型、resolver 和不可变 revision 值；禁止把 `main`、`latest` 或浮动 tag 作为落库值；
- 每个 artifact 的逻辑名称、固定 revision URL、格式、SHA-256、字节数和是否必需；
- 数据与代码的上游许可证声明、已验证 SPDX 结论、证据 URL 及人工说明；
- 商业使用、再分发、署名和相同方式共享结论；
- converter/prompt/scorer/profile 版本、split、依赖和 official comparability；
- 安全策略，包括仅 CLI 联网、禁止远程代码和禁止在线 rows 回退；
- 阻断项。草案允许 `null`，但任何阻断项都会使来源 fail-closed。

许可证声明与审核结论必须分开存放。`declared_ids` 可以逐字记录上游相互冲突的声明；只有人工完成证据核对后才能填写 `verified_spdx`。外部来源的 data 与 code license 必须分别提供匹配各自 `declared_ids` 的受支持 SPDX 结论和独立 evidence URL；只验证 data license 不能发布。不得为了让 JSON 看起来完整而猜许可证、revision、hash、字节数或 reviewer。

## Revision 与 artifact 核验

1. 操作者在 CLI 显式请求来源和 revision；resolver 可以把一次浮动引用解析成不可变 commit，但 receipt 和缓存只能保存解析后的值。
2. 下载 URL 必须绑定该 revision。只允许登记的 HTTPS 来源和明确的本地内部生成器。
3. 下载到同目录临时文件，设置连接/读取超时和最大字节数，并在流式写入时计算 SHA-256。
4. 同时核对状态码、声明 Content-Length（若有）、实际字节数和登记 hash；任一不符即删除临时文件并失败。
5. 校验通过后 fsync 并原子 rename。已存在缓存只有 hash 相同才可复用。fetch 和离线 convert 共用 cache path safety：配置 root、root 的全部祖先和目标后代都不能是 symlink 或 Windows junction，读取时仍要复核全局大小、artifact `max_bytes`、实际字节数和 hash。
6. 转换前再次校验 hash；转换器必须固定 id/version/config/seed，并对 schema、split、题数和答案范围 fail-closed。converter 自报的 source、revision、homepage、artifact、license、synthetic 和 converter identity 不是权威事实；发布数据集的 provenance 必须由当前 SourceSpec 和已验证 cache manifest 重建，非登记 converter alias 一律拒绝。
7. receipt 记录 source/revision、artifact URL/hash/bytes、许可状态、override 证据、converter/config hash、输入输出题数、拒绝原因、dataset fingerprint 和发布引用。publication audit 仅保存移除本机路径、正文和 secret 字段后的 portable receipt，并在存储边界重算 hash 和 identity。

原始 artifact 的 hash 不能用转换后文件 hash 代替。上游换 revision、artifact 内容、converter、prompt、scorer、配置或许可结论时，必须产生新 receipt 和新不可变资源版本。

## 禁止远程代码

任何来源都禁止设置或等价启用 Hugging Face `trust_remote_code`。同样禁止：

- 在数据获取/解析阶段执行上游 Python、shell、notebook、setup hook 或动态插件；
- 加载 pickle 或来源提供的可执行序列化对象；
- 因本地依赖缺失而静默回退在线 rows API；
- 在 API、Web、Worker、Run 准备、执行、重试或重评分路径隐式下载数据；
- 自动解压未实现路径穿越、symlink、文件数和展开大小限制的 archive。

JSON/JSONL/CSV 使用受控解析器：拒绝重复键、非有限数、超深嵌套和超限行列。Parquet 采用显式安装的 `pyarrow` optional dependency，读取前先校验 metadata 中的行、列和解压后大小；缺少依赖或超过上限时返回结构化错误并停止，不改变获取路径。

## 默认联网边界

CLI 是首批唯一默认联网入口，且网络动作必须由操作者显式发起。服务端远程获取默认关闭；API/Web 可以展示静态目录和操作已发布资源，但不能触发隐式 fetch。CI 使用 fake transport 和小型 fixtures，保持零网络、零真实模型费用。

`pending` 不允许 override。`restricted` 的审批证据必须绑定 source、允许动作、操作者、用途、批准人、签发时间、过期时间和工单，并由注入的可信 verifier 验证后才能生效；调用方自报 `override_verified` 不构成批准。当前 CLI 没有接入可信审批验证器，因此 restricted fetch/import/publish 保持 `SOURCE_APPROVAL_REQUIRED`。未来接入时，证据必须写入 receipt/publication audit，且只对本次动作生效；它不能改写 SourceSpec 状态，也不能跳过 revision、hash、schema、远程代码和原子发布门禁。

## 下架流程

许可证变化、上游撤回、hash 异常、数据污染、PII/敏感内容、schema 漂移或治理投诉均可触发下架：

1. 立即停止该来源的新 fetch、convert、import、publish 和新 Run；将目录状态改为 `pending`（待调查）或 `restricted`（已确认限制），并在 blockers 中记录原因和证据。
2. 保存触发时间、报告人、受影响 revision/artifact hash、已发布 dataset/scenario refs、Run 范围和处置负责人；通知技术负责人和来源/许可证负责人。
3. 隔离未发布缓存。若法律或政策要求删除原始数据，删除 artifact 正文但保留允许保留的 hash、大小、revision、receipt 和处置审计。
4. 不静默改写、重命名或复用已发布的不可变资源。将受影响资源标为不支持新运行；历史 Run、快照和评分证据按适用义务保留或执行受审计删除。
5. 评估报告、导出物和下游副本，记录通知、撤回或重新生成动作。restricted 数据不得继续出现在默认 stable 目录。
6. 只有新的人工复核结论、完整证据和新的 SourceSpec/资源版本通过批准后才能恢复；不得通过删除 blocker 或复用旧 receipt 恢复。

紧急下架可以先阻断动作后补齐调查，但不能先删除审计证据。发现 hash 与已登记值不一致时按供应链事件处理，不得自动接受新 hash。

## Phase 0 登记状态

| 来源 | 状态 | 当前边界 |
| --- | --- | --- |
| MMLU-Pro | `pending` | 数据卡与仓库许可证声明存在差异，未固定 revision/hash |
| TruthfulQA | `pending` | 上游许可证声明尚待针对固定 revision 人工复核 |
| `motte-core-zh` | `pending` | 生成器可做隔离 PoC；缺少真实 ownership/privacy reviewer、日期与证据，发布和新 Run 阻断 |
| C-Eval | `restricted` | 上游声明包含非商业/相同方式共享限制 |
| CMMLU | `restricted` | 上游声明包含非商业/相同方式共享限制 |
| IFEval | `pending` | 许可证和 evaluator/数据边界尚待固定 revision 复核 |
| LongBench v2 | `pending` | context preflight/no-truncation 已实现；数据卡、长文档、书籍与代码的上游权利仍待复核 |

这些 JSON 是可审计草案，不是可执行下载清单。只有所有 blocker 清除且状态/范围允许时，后续 CLI 才能使用。
