# Direct LLM 内置样例与来源登记

这些 JSONL 是仓库内置的**样例**评测集（不是权威基准，也不是官方数据）：用来在没有任何外部数据源、
不产生费用的前提下，把「导入 → 浏览题目 → 发起跑测 → 评分 → 对比」这条链路跑通和演示清楚。

## 受管来源登记

[`sources/`](sources/) 下的 JSON 是 Phase 0 静态 SourceSpec 草案，不包含第三方题目，也不是可直接执行的下载清单。
它们登记来源状态、上游 revision/artifact hash 占位、许可证声明与证据、转换协议和安全边界；任何 `pending`、
未获当前用途批准的 `restricted` 或缺少 revision/hash/人工复核的来源都必须 fail-closed。

| 来源 | 状态 | 登记 |
| --- | --- | --- |
| MMLU-Pro | `pending` | [mmlu-pro.json](sources/mmlu-pro.json) |
| TruthfulQA | `pending` | [truthfulqa.json](sources/truthfulqa.json) |
| `motte-core-zh` | `pending` | [motte-core-zh.json](sources/motte-core-zh.json) |
| C-Eval | `restricted` | [ceval.json](sources/ceval.json) |
| CMMLU | `restricted` | [cmmlu.json](sources/cmmlu.json) |
| IFEval | `pending` | [ifeval.json](sources/ifeval.json) |
| LongBench v2 | `pending` | [longbench-v2.json](sources/longbench-v2.json) |

完整状态语义、人工复核职责、证据和下架流程见 [数据集来源治理](../../docs/operations/dataset-source-governance.md)；
GSM8K 不转换、Direct LLM v1/v2 并存、v2 selected-only snapshot 和 CLI 默认联网入口见
[Direct LLM Dataset v2 ADR](../../docs/superpowers/specs/adr/2026-09-19-direct-llm-dataset-v2.md)。

服务端远程获取默认关闭；浏览、运行和评分路径不得隐式联网。后续 CLI 来源命令必须由操作者显式触发，且禁止
Hugging Face `trust_remote_code`、pickle 和在线 rows API 回退。当前三份 JSONL 仍是 Direct LLM v1 smoke fixture。

## 文件格式

每行一个 JSON 对象：

| 键 | 必填 | 说明 |
| --- | --- | --- |
| `input` 或 `prompt` | 是（二选一，不能同时出现） | 题面，原样作为 user 消息发给模型，不加任何后缀 |
| `expected` | 否 | 期望答案；缺省时该题记为 `no_expectation`，不判对错也不进 accuracy 分母 |
| `scorer` | 否 | 覆盖数据集默认评分器，取值 `exact` / `contains` / `regex` |
| `case_id` | 否 | 缺省按序生成 `{数据集名}-0000`；必须在当前数据集内唯一，跨数据集由 dataset fingerprint 定域 |

导入是整份校验：任何一行不合法（未知键、空题面、非法正则、重复 `case_id`）都会整体拒绝，
不会写进半个数据集。

## 评分器口径

| 评分器 | 判定 | 适用 |
| --- | --- | --- |
| `exact` | 两侧 `strip()` 后完全相等（大小写敏感） | 答案唯一的单值问答 |
| `contains` | 期望串是输出的子串（大小写敏感） | 标签、格式标记等容忍前后文的场景 |
| `regex` | `re.search(期望串, 输出)` | 结构化输出中抽取某个字段并校验 |

正则在**导入期**就已编译校验，因此运行期不会因为坏正则失败。

## 内置集合

| 数据集 | 评分器 | 题数 | 考察点 |
| --- | --- | --- | --- |
| `direct-llm-exact-answer` | `exact` | 8 | 单值问答：只回答答案本身，考验指令遵循与精确性 |
| `direct-llm-classify` | `contains` | 8 | 四分类打标：容忍标点、大小写以外的多余文字 |
| `direct-llm-json-extract` | `regex` | 7 | 结构化抽取：JSON 键值形状、缺失字段填 `null`、句末格式标签（含一题 `contains` 覆盖） |

## 导入方式

- Web：Direct LLM 操作页「内置样例 / 导入 JSONL」卡一键导入，或粘贴 / 选择本地文件。
- CLI：`uv run python -m motte_cli direct-llm import --builtin direct-llm-classify --license internal-sample`
  或 `--file path/to/custom.jsonl`。

内置数据由本仓库维护，`license` 建议写 `internal-sample`；导入后的记录用 `source: builtin:<数据集 id>`
标记来源，可追溯。
