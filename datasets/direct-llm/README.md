# Direct LLM 内置样例数据集

这些 JSONL 是仓库内置的**样例**评测集（不是权威基准，也不是官方数据）：用来在没有任何外部数据源、
不产生费用的前提下，把「导入 → 浏览题目 → 发起跑测 → 评分 → 对比」这条链路跑通和演示清楚。

## 文件格式

每行一个 JSON 对象：

| 键 | 必填 | 说明 |
| --- | --- | --- |
| `input` 或 `prompt` | 是（二选一，不能同时出现） | 题面，原样作为 user 消息发给模型，不加任何后缀 |
| `expected` | 否 | 期望答案；缺省时该题记为 `no_expectation`，不判对错也不进 accuracy 分母 |
| `scorer` | 否 | 覆盖数据集默认评分器，取值 `exact` / `contains` / `regex` |
| `case_id` | 否 | 缺省按序生成 `{数据集名}-0000`；必须全局唯一 |

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
