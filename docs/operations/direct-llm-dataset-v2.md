# Direct LLM Dataset v2 主操作指南

本文是 Direct LLM Dataset v2 的操作员指南，描述当前代码、CLI、API 和 Web 的实际边界。它不是许可证批准、官方榜单成绩或数据再分发授权。来源治理结论以 [数据集来源治理](dataset-source-governance.md) 为准。

## 先看结论

- **GSM8K 永久独立**：GSM8K 保留自己的 suite、官方来源、prompt、Decimal scorer、统计口径和版本治理；不得转换成 Direct LLM 数据集，也不能用 Direct LLM v2 的扩容工作替代 GSM8K 治理。
- **v1 与 v2 并存**：历史 `direct-llm@1` 保持可读、可运行、可重评分；v2 使用显式 `contract_version=2`、`plugin_version="2"` 和独立 schema。未知显式版本不会回落到另一版本。
- **当前没有可发布的受管来源**：`motte direct-llm sources list` 当前返回 7 个来源，其中 MMLU-Pro、TruthfulQA、MoTTE Core ZH、IFEval、LongBench v2 为 `pending`，C-Eval、CMMLU 为 `restricted`；没有 `approved` 项，因此当前不存在能成功完成受管 `import`/`prepare` 的来源。
- **受管来源的网络入口是 CLI**：API、Web、Worker、retry、rescore 和 CI 不隐式下载来源。`fetch` 是显式联网命令；`import` 只读 verified cache；`prepare` 才是显式 fetch 后转换并发布的组合操作。
- **v2 Run 是 selected-only snapshot**：创建 Run 时先解析 profile 或抽样为固定 case ID 顺序，只把选中的完整 cases 冻结到 snapshot。12K 规模的全量数据也不会把未选题复制进 Run。

## v1/v2 兼容矩阵

| 维度 | Direct LLM v1 | Direct LLM v2 |
| --- | --- | --- |
| 资源识别 | `eval.suite=direct-llm`、`id=direct-llm-prompts`、`version=1`；历史缺少 `plugin_version` 的场景按 v1 解释 | `contract_version=2`、`eval.version=2`、`plugin_version="2"`；scenario 必须显式声明 v2 |
| prompt | `direct-llm-verbatim-v1`；题面原样作为 user prompt，不追加套件指令 | 由数据集 `eval.prompt_version` 钉住；转换器必须把 prompt 版本写入 provenance/config |
| scorer | `direct-llm-answer-v1`：`exact`、`contains`、`regex`；数据集默认评分器，可由单题覆盖 | scorer 是带 `id`、`version`、`config`、`config_sha256` 的对象；单题 metadata 可覆盖，但 hash 必须匹配 |
| snapshot | v1 保留完整 dataset 快照以维持历史兼容 | 只冻结 dataset identity/fingerprint/cases hash、selection 和 selected cases；provider projection 只含执行所需 prompt |
| 选择 | `all`、`ids`、`random` 与可复现 seed | 另支持固定 profile；profile 保存有序 case IDs、数量和 `case_ids_sha256`，不能在运行时重新抽样 |
| 分母 | `judged = correct + wrong_answer + call_failed`；无 `expected` 的 `no_expectation` 不进分母；有 expected 但调用失败仍进分母 | 聚合使用 `judged_cases`；`correct`、`wrong_answer`、`invalid_format` 进入分母，`call_failed` 和 `not_attempted` 不进分母，同时报告 coverage |
| 版本行为 | 已发布资源 insert-only，历史 Run/ScoringPass 保持原语义 | v2 资源、prompt、scorer、来源证据或转换配置变化必须产生新的不可变版本，不覆盖旧资源 |

`GSM8K` 不在这张 Direct LLM v1/v2 矩阵中。它是独立 suite，详见 [GSM8K 基准操作指南](gsm8k-smoke.md)。

## v2 数据集和 profiles

一个合法的 v2 dataset 至少包含：

- `contract_version=2`、`eval.suite=direct-llm`、`eval.id=direct-llm-prompts`、`eval.version=2`；
- `eval.prompt_version`、带配置 hash 的 scorer、`max_output_tokens` 和 `max_retries`；
- 全部 cases 的 `cases_sha256` 与 dataset fingerprint；
- `provenance.source_id`、固定 upstream revision、artifact manifest hash、许可证快照和 converter/config hash；
- 一个或多个 profile。每个 profile 是固定 case ID 列表，不是运行时按当前数据集重新计算的条件。

当前受管 SourceSpec 通常登记 `smoke`、`regression`、`full`，但登记 profile 不代表来源已批准，也不代表已经存在可发布 dataset。当前七个来源都仍被治理或技术 blocker 阻断。

运行 v2 profile 时，解析阶段会把 profile 解析成固定顺序的 case IDs，并将 selected cases 写入 snapshot。即使 dataset 有 12,000 题，选择一个小 profile 或随机子集时，Run 只存所选题；未选题不会进入 provider manifest，也不会在重试或重评分时从可变来源重新读取。

CLI 示例：

```bash
uv run python -m motte_cli direct-llm run \
  --scenario <published-v2-dataset>@<version> \
  --model <published-model-profile> \
  --profile smoke
```

这是一个**仅在 dataset/scenario 和 model profile 已发布且可用时**的示例。不要把当前 pending/restricted 来源名替换进去期待成功。

## 受管来源登记和状态

当前 `motte direct-llm sources list` 的治理快照如下：

| source id | 当前状态 | 主要阻断 | 当前能力 |
| --- | --- | --- | --- |
| `mmlu-pro` | `pending` | revision、artifact、converter 版本、许可证和官方协议 parity 未完成 | 不能受管 fetch/import/publish；仅可在未来满足本地 fixture/代码条件后做隔离实验 |
| `truthfulqa` | `pending` | 固定 commit、artifact hash/大小、许可证复核和 converter 版本未完成 | 不能受管 fetch/import/publish |
| `motte-core-zh` | `pending` | ownership/privacy 人工复核、双人 review 和模型校准未完成 | 生成器 PoC 不等于 cached source 可发布能力 |
| `ifeval` | `pending` | evaluator/data revision、规则 scorer、profiles 和官方 parity 未完成 | 不能受管 fetch/import/publish |
| `longbench-v2` | `pending` | 长文档权利、revision、tokenizer eligibility、预算和 runner parity 未完成 | 只能在固定本地快照上做受限研究，不能正式发布 |
| `ceval` | `restricted` | 非商业/share-alike 限制，且 revision、artifact 和人工审批未完成 | 默认拒绝受管 fetch/import/publish；默认 Web 隐藏 |
| `cmmlu` | `restricted` | 非商业/share-alike 限制，且 revision、artifact 和人工审批未完成 | 默认拒绝受管 fetch/import/publish；默认 Web 隐藏 |

来源目录中的 `revision.value`、artifact URL/SHA-256/bytes/max_bytes、converter version、prompt/scorer/profile 和 blocker 都是准入证据。登记一份 JSON 不等于批准该来源。

## CLI 命令边界

所有命令统一入口：

```bash
uv run python -m motte_cli direct-llm sources <command> ...
```

| 命令 | 网络 | 读取 | 写入 | 说明 |
| --- | --- | --- | --- | --- |
| `list` | 否 | 本地 SourceSpec registry | 无 | 输出静态来源登记和 blocker |
| `inspect` | 否 | 本地指定 SourceSpec | 无 | 查看单来源完整登记；`--source <id>` |
| `fetch` | 是，仅显式 CLI | registry 和上游 HTTPS artifact | cache 根目录 | 按 required artifact 下载、限制大小、校验 Content-Length/bytes/SHA-256，验证后原子写入 cache |
| `verify` | 否 | registry 和 cache | 无 | 重新读取 required artifact，校验大小和 SHA-256 |
| `convert` | 否 | verified cache、registry | `--output` 指定的 JSON 文件 | 转换为 v2 dataset，并原子写入 `{dataset, receipt}`；不写 resource store |
| `import` | 否 | verified cache、registry | dataset、scenario、publication audit resource store | 重新转换并原子发布；不触网 |
| `prepare` | 是，仅显式 CLI | registry、上游 artifact 和 cache | cache、dataset、scenario、publication audit | 执行 fetch、convert 和原子发布；只适用于所有门禁通过的 approved 来源 |

默认 cache 根目录是 `var/datasets/direct-llm`，也可通过 `--cache-dir` 或 `MOTTE_DIRECT_LLM_CACHE_DIR` 指定。默认 registry 是 `datasets/direct-llm/sources`，也可通过 `--registry-dir` 或 `MOTTE_DIRECT_LLM_SOURCE_REGISTRY` 覆盖。cache 路径按 source、revision、完整 artifact SHA-256 和 logical artifact name 隔离；不要手工把不同 revision 或 hash 的文件放在同一命名空间。

### list 和 inspect

```bash
uv run python -m motte_cli direct-llm sources list
uv run python -m motte_cli direct-llm sources inspect --source mmlu-pro
```

两者只读本地 JSON，不联网，也不下载或发布。当前输出中没有可发布来源。

### fetch 和 verify

```bash
# 当前 pending 示例：预期失败，不是成功下载示例
uv run python -m motte_cli direct-llm sources fetch \
  --source mmlu-pro \
  --revision <40-hex-revision>
# 预期治理错误：SOURCE_LICENSE_BLOCKED

uv run python -m motte_cli direct-llm sources verify \
  --source <approved-source-id> \
  --revision <40-hex-revision>
```

`fetch` 只能由显式 CLI 发起网络请求。下载不允许远程重定向、非 HTTPS、credential-bearing URL、浮动 revision 或超限 artifact；只有完整校验通过才会保留 cache。`verify` 不发请求，也不修改 cache；缺失或篡改 cache 会返回结构化 `SOURCE_CACHE_MISS` 或 `SOURCE_CACHE_CORRUPT` 等错误。

`restricted` 来源即使带上 `--override-actor`、`--override-purpose` 和 `--override-ticket`，当前 CLI 也不会注入 SDK 的 `ApprovalVerifier`；这些参数只是未验证的请求属性，不能解除 gate。CLI 的 restricted fetch/verify 当前失败码为 `SOURCE_APPROVAL_REQUIRED`。

### convert

`convert` 必须显式传 `--revision`、`--config` 和 `--output`：

```bash
uv run python -m motte_cli direct-llm sources convert \
  --source <approved-source-id> \
  --revision <40-hex-revision> \
  --config @var/source-config.json \
  --output var/converted/<source-id>-<revision>.json
```

`--config` 可以是内联 JSON，也可以是 `@path/to/config.json`。CLI 只接受白名单配置键，并拒绝 credential 字段；`@file` 不是绕过校验的方式。输出文件内容是：

```json
{
  "dataset": {"contract_version": 2, "...": "..."},
  "receipt": {
    "source_id": "...",
    "revision": "...",
    "artifact_manifest_sha256": "...",
    "dataset_fingerprint": "...",
    "publishable": true
  }
}
```

当前 pending/restricted 来源若加 `--allow-experimental`，该开关确实允许**本地转换尝试**；它不会改变 license 状态，不解除 `import`/`publish` 门禁，转换 receipt 的 `publishable` 仍为 `false`。当前七个登记均存在 blocker；实际最先出现的错误可能是 `SOURCE_REVISION_INVALID`、`SOURCE_NOT_READY`、`CONVERTER_UNAVAILABLE` 或转换器自己的结构化错误。不要把此命令当作当前来源成功入口。

### import 和 prepare

```bash
# 当前 pending/restricted 示例都预期失败；不是可执行成功示例
uv run python -m motte_cli direct-llm sources import \
  --source <pending-or-restricted-source-id> \
  --revision <40-hex-revision> \
  --config @var/source-config.json \
  --actor <operator>
```

CLI 当前实际治理错误为：

- `pending` 的 `import`/`prepare`：`SOURCE_LICENSE_BLOCKED`；
- `restricted` 的 `import`/`prepare`：`SOURCE_APPROVAL_REQUIRED`；当前 CLI 不注入 `ApprovalVerifier`，因此不能由 CLI 参数解除；
- 未固定 revision 或证据不全的来源：通过治理门后可能返回 `SOURCE_REVISION_INVALID`、`SOURCE_NOT_READY` 等结构化错误。

未来只有在来源变成 approved、人工复核和所有技术证据完成后，才使用下面的占位示例：

```bash
# 先决条件：SourceSpec=approved；revision/artifact/converter/prompt/scorer/profile
# 已固定；许可证与 protocol parity 已人工确认；cache 已通过 verify。
uv run python -m motte_cli direct-llm sources import \
  --source <approved-source-id> \
  --revision <40-hex-revision> \
  --config @var/source-config.json \
  --actor <release-operator>

# prepare 会显式联网 fetch，再转换并原子发布；只有同样的 approved 先决条件才可执行。
uv run python -m motte_cli direct-llm sources prepare \
  --source <approved-source-id> \
  --revision <40-hex-revision> \
  --config @var/source-config.json \
  --actor <release-operator>
```

发布会一次性写入 dataset、scenario 和 publication audit。dataset/scenario 是 insert-only，不覆盖同名版本；重复相同内容的发布会复用相同 publication identity。publication audit 绑定 dataset fingerprint、receipt/source evidence、actor、entrypoint 和时间，供后续审计查询。

## Approved 的未来发布流程

只有以下条件全部满足，才可把占位符替换为真实值：

1. SourceSpec 状态为 `approved`，reviewer/reviewed_at、数据与代码许可证证据、用途结论和 blockers 完整。
2. upstream revision 是不可变值，artifact URL 绑定该 revision，format/SHA-256/bytes/max_bytes 全部固定。
3. converter id/version、prompt version、scorer id/version/config hash、profile strategy/seed 和输出协议已固定。
4. converter 在离线 verified cache 上通过 schema、题数、profile hash、artifact manifest 和 dataset fingerprint 校验；无 row drop。
5. `verify` 成功后由同一受控 CLI 操作者运行 `import` 或 `prepare`；API/Web/Worker 不替代这一发布入口。
6. 原子发布产生 dataset、scenario 和 publication audit；发布后只允许创建新版本，不原地修改资源。

失败时应检查 receipt 和 publication audit，而不是手工改写 dataset。来源下架时，先停止新 fetch、convert、import、publish 和新 Run，隔离未发布 cache，记录受影响 revision/hash/资源引用；不要静默删除历史审计或复用旧 receipt 恢复。

## API 操作面

API 只操作本地 registry、已发布资源和 Run，不触发受管来源 fetch/convert/import/prepare。

### Source API

```text
GET /api/v1/benchmarks/direct-llm/sources
GET /api/v1/benchmarks/direct-llm/sources/{source_id}
```

前者返回 `DatasetSourceSummary` 列表：状态、distribution scope、stable eligibility、revision、声明 license IDs、profiles、official comparability 和 blocker 数；后者返回完整 SourceSpec。当前 API 不提供受管来源的远程准备接口。

### Dataset 和 publication audit API

```text
GET /api/v1/datasets
GET /api/v1/datasets/{name}/{version}
GET /api/v1/resource-publications
GET /api/v1/benchmarks/direct-llm
GET /api/v1/benchmarks/direct-llm/cases?dataset=<name>@<version>&offset=0&limit=50
```

`GET /api/v1/datasets` 返回 `DatasetSummary` 列表，包含 `contract_version`、fingerprint、题数、cases hash、suite、scorer、source、revision、license status 和 profiles。`resource-publications` 是 publication audit 的只读清单；它不是来源下载日志。每条 audit 保存去除本地 `artifact_paths`、正文和 secret 字段后的 portable receipt，并在写入边界重算 receipt hash、publication ID 和规范 UTC 时间。Direct LLM v2 dataset/scenario 不能通过通用单资源 POST 独立发布，必须与匹配 audit 在同一事务中提交。

### Run 和 dry-run API

```text
POST /api/v1/benchmarks/direct-llm/runs
POST /api/v1/benchmarks/direct-llm/dry-run
GET  /api/v1/runs
GET  /api/v1/runs/{run_id}
GET  /api/v1/runs/{run_id}/report
GET  /api/v1/runs/{run_id}/scoring-passes
POST /api/v1/runs/{run_id}/retry
POST /api/v1/runs/{run_id}/rescore
```

Run body 使用 `scenario`、`model`、`parameters`、`reasoning_level` 和 `case_selection`；case selection 可以是 `all`、`ids`、`random+seed` 或 v2 `profile`。创建期会解析资源和 provider/model 快照，API 只排队，不在请求中调用模型。v2 创建前还会要求匹配 publication audit，并重新检查当前 SourceSpec 状态与已发布 provenance；来源缺失、转为 pending/restricted、证据或 artifact/converter identity 漂移时，新 Run fail closed。Provider adapter 只接收 allowlist execution manifest，不会收到 benchmark snapshot、expected、scorer 或 provenance。

`dry-run` 走同一套选择和 manifest preparation，但不创建 Run。它返回 selected count、case ID hash、单题输入上界、输出 reserve、总量上界、context window、估算方法、费用上界和 price table version。缺少可用上下文或价格表时相应字段可为 `null`/“未提供”，不能把缺失当作零成本。

## Web 控制台边界

`/direct-llm` 是已发布 Direct LLM 数据集和 Run 的操作页：

- v2 数据集存在 profiles 时，页面显示 profile 选择，并以 profile count 计算 selected 题数；
- “运行前估算”调用 dry-run，显示上下文和费用的保守上界；它不会创建 Run；
- `restricted` 来源默认从来源选择列表隐藏，可手动打开“显示受限来源”查看静态登记；页面不提供隐式 fetch、convert、import 或 prepare；
- Web 的来源目录可以展示 `pending`，但展示不代表可运行或可发布；当前七个来源没有可发布项。

内置样例和本地 JSONL 导入是另一个本地数据集入口，不应与受管来源状态混淆。内置样例用于零网络、零真实模型调用地验证链路，见 [Direct LLM 评测操作指南](direct-llm.md)。

## Context、截断和费用

v2 运行创建时，如果已发布 model profile 固定了 `context_window`，系统会执行 context preflight：

- 使用 UTF-8 bytes 加固定 request/message structure 估算，不依赖某一个 tokenizer；方法版本为 `utf8-bytes-plus-structure-v1`；
- 将输出 `max_output_tokens` 作为 reserve 加入总量上界；任一 selected case 的总量上界超过 context window，创建期返回 `CONTEXT_WINDOW_EXCEEDED`；
- **不做静默截断**。完整 prompt 不适合上下文窗口时必须失败或由操作者修改选择/预算；不能拿截断后的题面冒充 full-verbatim 评测；
- 费用只有在 price table 提供有效 input/output rates 且存在输入上界时才会计算，结果是按 selected count 的保守上界，不是供应商最终账单。多模型运行应分别估算，不能把一个模型的 price table 套到另一个模型。

LongBench v2 另外要求 full-verbatim、tokenizer-aware eligibility 和官方 runner parity；当前这些证据仍 pending，见 [Direct LLM 高级基准运维与限制](direct-llm-advanced-benchmarks.md)。

## Retry、rescore 和离线 snapshot

- v2 `rescore` 只读持久化的 selected-only snapshot 和已有模型结果，不重新调用 subject model，也不读取可变 source/cache；每次 rescore 追加新的 `ScoringPass`/score set。
- 普通 `retry` 使用原 Run 的不可变快照，可能产生新的模型调用和费用；它不会重新从来源抽题，但在创建 child Run 前会重新检查当前 SourceSpec、publication audit 和存储资源 fingerprint。来源已下架或证据失效时 retry 拒绝，父 Run 和历史报告不变。
- `profile_stale` retry 才会重新解析 requested manifest，并按规则刷新过期的 profile/resource snapshot；只回放 `mode/profile` 请求字段，不把持久化的 count、seed 或 hash 当作用户输入。
- Worker 重启遇到 `dispatching` 时会标记 `indeterminate`/`needs_review`，不会自动重复可能已被 Provider 接收的付费请求。核对外部证据后再显式 retry。

## 备份、恢复、迁移和回滚

升级或发布前先停止 API、Worker/Celery consumer，并备份数据库与 artifact/cache。SQLite 示例：

```bash
uv run python -m motte_cli backup \
  --target var/backups \
  --artifacts-root var/artifacts

uv run python -m motte_cli restore \
  --source var/backups \
  --artifacts-root var/artifacts
```

默认受管来源 cache 是 `var/datasets/direct-llm`，它不等同于 `var/artifacts`。需要恢复受管 cache 时单独备份其目录，并核对 source/revision/artifact hash；不要只恢复数据库而认为 cache 已恢复。

PostgreSQL：

```bash
pg_dump -Fc -h <host> -U motteavl -d motteavl -f motteavl-<date>.dump
pg_restore -h <host> -U motteavl -d motteavl --clean --if-exists <dump>
```

当前数据库 head 是 `0003_resource_publications`：`0002_platform_integrity` 引入 Run revision、CaseAttempt、ScoringPass/ScoreSet 和 RunCommand；`0003_resource_publications` 引入 append-only dataset/scenario publication audit。PostgreSQL 升级：

```bash
DATABASE_URL=... uv run alembic upgrade head
```

SQLite 在打开数据库时执行幂等 schema upgrade，没有安全的就地 downgrade 路径。回滚顺序是先备份、再回应用代码、最后评估 schema：

```bash
DATABASE_URL=... uv run alembic current
DATABASE_URL=... uv run alembic downgrade -1
```

从 `0003` 回退会删除 publication audit 表；继续回退会删除 `0002` 的执行审计结构。任何 downgrade 都必须先确认备份可恢复；SQLite 应恢复升级前数据库备份。优先用 `git revert` 保留审计历史，不要直接删除不可变 dataset/scenario/publication 记录。

来源下架或 cache 污染时，停止新动作并隔离 cache；保留政策允许的 revision、hash、大小、receipt 和 publication audit。不要接受未知 hash、改名覆盖旧资源，或用恢复后的旧 cache 绕过新的 SourceSpec blocker。

## Optional dependency 和 Parquet

Parquet 解析依赖显式安装的 optional `pyarrow`。本仓库的受控 parser 会先读取 metadata 并检查行数、列数和解压后大小；缺少依赖时返回结构化错误：

```json
{
  "error": {
    "code": "SOURCE_DEPENDENCY_MISSING",
    "details": {"dependency": "pyarrow"}
  }
}
```

当前指南不声称 `pyarrow` 已安装，也不把缺失依赖静默回退到在线 rows API。依赖缺失、Parquet 超限或 metadata 不可信都应停止转换。

## 已知限制

- 当前自动化验证以 fake transport、合成 fixture、离线 adapter 和本地 resource store 为主；没有 live Provider、Docker 部署或 PostgreSQL 生产证据。
- 七个受管来源仍有许可证/人工复核 blocker；MoTTE Core ZH 的 ownership/privacy review、双人 review 和模型校准也仍 pending。
- 官方 prompt、scorer、runner、tokenizer eligibility、抽取和聚合 parity 尚未对当前来源建立；`official_comparability` 不得写成已成立。
- SDK 的 `SafeFetcher` 构造参数和 `read_cached_artifact(..., approval_verifier=...)` 支持注入 `ApprovalVerifier`，由 verifier 校验外部审批证据；当前 CLI 没有配置或注入该 verifier，所以 restricted 的 CLI fetch/verify/import/prepare 仍保持阻断。actor、ticket 或 adapter 转换成功都不是批准。
- 受管来源的 API/Web fetch、自动发布和 Worker 下载仍未开放；当前不能用命令示例伪造 approved 成功路径。
- 费用估算是保守上界；它不包含供应商计费差异、重试之外的服务费或最终账单校正，也不构成预算批准。

## 关联文档

- [数据集来源治理](dataset-source-governance.md)：状态、人工复核、许可证、artifact、下架和 trusted verifier 边界。
- [Direct LLM Dataset v2 与受管来源边界 ADR](../superpowers/specs/adr/2026-09-19-direct-llm-dataset-v2.md)：GSM8K 独立、v1/v2 并存、CLI 网络边界和 selected-only 决策。
- [Direct LLM 高级基准运维与限制](direct-llm-advanced-benchmarks.md)：C-Eval、CMMLU、IFEval、LongBench v2 的专项限制。
- [升级](upgrade.md)：停服、备份、Alembic head、SQLite schema upgrade 和 downgrade 风险。
- [备份与恢复](backup-restore.md)：SQLite/PostgreSQL 备份、artifact 备份和 restore 命令。
- [版本回退](rollback.md)：应用代码、迁移和验证顺序。
- [Direct LLM 评测操作指南](direct-llm.md)：v1 JSONL、评分、内置样例、普通 Direct LLM CLI/API/Web。
