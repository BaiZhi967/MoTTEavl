# Direct LLM 评测（通用直连）

Direct LLM 是「通用直连评测」：题面即 prompt，逐题直接调用模型，用确定性评分器判定是否通过。
它没有官方上游数据源，数据来自本地 JSONL（仓库也内置了几份样例），因此与 GSM8K 的差别只有
「数据集从哪来」，运行、评分口径与恢复语义与 GSM8K 共用同一条链路。

本文件是操作者指南；实现契约见 `packages/contracts/motte_contracts/direct_llm.py`。

## 数据集契约

一份数据集 = 整份 JSONL 逐行校验后落成的不可变 `数据集名@版本`。每行一个 JSON 对象：

| 键 | 必填 | 说明 |
| --- | --- | --- |
| `input` 或 `prompt` | 是（二选一，不能同时出现） | 题面。**原样**作为 user 消息上线，不加任何套件后缀（prompt 版本 `direct-llm-verbatim-v1`） |
| `expected` | 否 | 期望答案。缺省时该题记为 `no_expectation`，不判对错也不进 accuracy 分母 |
| `scorer` | 否 | 覆盖数据集默认评分器：`exact` / `contains` / `regex` |
| `case_id` | 否 | 缺省按序生成 `{数据集名}-0000`；必须唯一 |

整份文件逐行校验：未知键、空题面、非法正则、重复 `case_id`、非 UTF-8、空文件都会**整体拒绝**，
不会落半个数据集。`source_line_count` 记的是非空行数，每题的 `metadata.source_line` 记的是物理行号
（空行被跳过，因此两者不一定相等）。

### 评分器（`direct-llm-answer-v1`）

| 评分器 | 判定 | 适用 |
| --- | --- | --- |
| `exact` | 两侧 `strip()` 后完全相等（大小写敏感） | 答案唯一的单值问答 |
| `contains` | 期望串是输出的子串（大小写敏感） | 标签、格式标记等容忍前后文的场景 |
| `regex` | `re.search(期望串, 输出)` | 结构化输出里抽取某个字段并校验 |

正则在**导入期**就编译校验，因此运行期不会因为坏正则失败。评分只看输出正文：`finish_reason=length`
截断的输出照样按正文判定。

### 分母口径：判定题数 judged

`summary.judged = correct + wrong_answer + call_failed`，`accuracy = correct / judged`：

- 没有 `expected` 的题（`no_expectation`）不判对错，**不进分母**；
- 有期望但**调用失败**的题仍然进分母（与 GSM8K 一致：失败不该被静默移出分母）；
- 从未发起的题（`not_attempted`）不进分母；
- 全部题目都没有期望时 `accuracy` 为 `null`，控制台显示「—」。

因此报告里的 `pass_rate` 与 `selected` 不是一回事：`selected` 是本次运行选中的题数，
`judged` 才是通过率的分母，`summary.denominator` 字段明确写出 `judged_cases`（GSM8K 是 `selected_cases`）。
对比页拿各运行自己的 `judged` 做分母，因此两列判定题数不同时会提示不可直接横比。

## 内置样例

`datasets/direct-llm/` 下有三份仓库自带的小型样例（该目录 README 有逐份说明）：

| 数据集 | 评分器 | 题数 | 考察点 |
| --- | --- | --- | --- |
| `direct-llm-exact-answer` | `exact` | 8 | 单值问答：只回答答案本身 |
| `direct-llm-classify` | `contains` | 8 | 四分类打标：容忍多余文字 |
| `direct-llm-json-extract` | `regex` | 7 | JSON 键值形状、缺失字段填 `null`、句末格式标签（含一题 `contains` 覆盖） |

样例由本仓库维护，`license` 记为 `internal-sample`，`source` 记为 `builtin:<id>`。它们不是权威基准，
用于在没有任何外部数据、不产生费用的前提下跑通链路与演示评分口径。目录可用 `MOTTE_BUILTIN_DATASET_DIR`
覆盖（非 editable 安装时指向样例目录）；目录缺失时清单接口返回该项 `importable: false`，
CLI/API 导入返回结构化错误 `BUILTIN_UNAVAILABLE`。

## Web 控制台

`make dev` 后打开 `/direct-llm`：

- **操作页**（六卡）：数据集（pinned，展示题数、数据集级评分器、来源、prompt/scorer 版本）、
  题目（全部 / 随机 N 题 + 种子 / 指定题目）、模型多选（勾选支持推理的模型后出现该模型自己的思考强度）、
  跑测参数（temperature、max_output_tokens）、内置样例（一键导入）、导入本地 JSONL。
- **题目页** `/direct-llm/cases`：按数据集浏览全部题目（题面、期望答案、生效评分器），支持搜索与分页，
  可逐题勾选、全选本页、粘贴 case id（只在客户端去重，未知 id 由服务端在发起时拒绝），
  再用「用所选 N 题发起评测」把选择经 sessionStorage 带回操作页。
- **过程页** `/direct-llm/monitor?runs=a,b`：批次行 + 逐题格子 + 运行时间线；格子把 `no_expectation`
  也显示为灰色的「未判定」。
- **结果页** `/direct-llm/runs/{id}/result`：通过率（分母＝判定题数）、判定题数（选中·无判定）、tokens、
  成本（含 price_table 版本）、模型五张指标卡 + 逐题钻取（题面、模型输出、失败原因、期望、生效评分器）。
- **对比页** `/direct-llm/compare?runs=a,b`：模型列 × 指标行（通过率、判定题数、tokens、成本）+ 共同不通过题 +
  逐题矩阵下钻。

本地 JSONL 由浏览器读成文本后放进 JSON body（不引入 multipart）；也可以直接用「选择 .jsonl 文件」按钮。

## CLI

```bash
export MOTTE_DB_PATH=./var/direct-llm.db          # PowerShell: $env:MOTTE_DB_PATH = './var/direct-llm.db'
uv run python -m motte_cli direct-llm builtins     # 列出内置样例（含题数、可导入性）
uv run python -m motte_cli direct-llm import --builtin direct-llm-classify
uv run python -m motte_cli direct-llm import --file ./my-set.jsonl --name my-set --scorer regex
uv run python -m motte_cli direct-llm list         # 列出已导入的数据集
uv run python -m motte_cli direct-llm run --scenario direct-llm-classify@1 --model YOUR_MODEL_PROFILE_ID
uv run python -m apps.worker.motte_worker --once
```

导入回执（stdout 单行 JSON）：

```json
{"imported": "direct-llm-classify@1", "scenario": "direct-llm-classify@1", "suite": "direct-llm",
 "scorer": "contains", "cases": 8, "source": "builtin:direct-llm-classify",
 "source_sha256": "...", "cases_sha256": "..."}
```

- **场景名与数据集名一致**（Direct LLM 没有 GSM8K 那样的 scope 维度），版本号也一一对应。
- 省略 `--version` 时自动选版本：**同内容已存在则复用其版本**（重复导入幂等），否则取下一个数字空号；
  显式钉住已存在且内容不同的 `name@version` 返回 409 `RESOURCE_CONFLICT`。
- 可选字段「显式传入就必须合法」：`--name` / `--license` / `--scorer` / `--source` 传空串会以
  422 `CONTRACT_INVALID` 拒绝，而不是静默回落到默认值；只有 `--version` 的「留空＝自动」是有语义的。
- 失败时退出码 2，stderr 是错误 JSON：`BUILTIN_UNAVAILABLE`（未知样例或样例目录缺失）、
  `SOURCE_UNAVAILABLE`（本地文件读不到）、`CONTRACT_INVALID`（逐行校验失败）、
  `RESOURCE_CONFLICT`（版本冲突）、`SCENARIO_NOT_FOUND`、`MODEL_NOT_FOUND`、
  `RUN_CONFIG_INVALID`（未知 case id、非法子集、非正数输出上限）、`MODEL_CONFIG_INVALID`（超模型上限）。

子集与输出预算（与 GSM8K 同构）：

```bash
uv run python -m motte_cli direct-llm run --scenario direct-llm-classify@1 --model YOUR_MODEL \
  --case-ids direct-llm-classify-0001,direct-llm-classify-0004
uv run python -m motte_cli direct-llm run --scenario direct-llm-classify@1 --model YOUR_MODEL \
  --random 3 --seed deadbeef --temperature 0.2 --max-output-tokens 256
```

`case_selection` 的语义与 GSM8K 完全一致（`all` / `ids` / `random` + 可复现种子，选择与种子写进运行快照）。
`--max-output-tokens` 是 Direct LLM 与 GSM8K 的**唯一差别**：GSM8K 把预设值 1024 钉死，
Direct LLM 允许本次运行覆盖（省略即数据集预设 1024），且仍然受模型档案声明的上限约束
（超上限在创建期就以 `MODEL_CONFIG_INVALID` 拒绝，不产生付费调用）。

## API

| 端点 | 用途 |
| --- | --- |
| `GET /api/v1/benchmarks/direct-llm` | 数据集概览（`scenario` / `dataset` / `eval` / `provenance` / `cases` / 最近运行的 accuracy） |
| `GET /api/v1/benchmarks/direct-llm/builtins` | 内置样例清单（含 `cases` 与 `importable`） |
| `POST /api/v1/benchmarks/direct-llm/import` | body `{content}` 或 `{builtin}`（二选一）+ `name?`/`version?`/`license?`/`scorer?`/`source?` |
| `GET /api/v1/benchmarks/direct-llm/cases` | `dataset=<name@version>&offset=&limit=&query=` 分页只读浏览题目（含每题生效 `scorer`） |
| `POST /api/v1/benchmarks/direct-llm/runs` | body `{model, scenario?, parameters?, reasoning_level?, case_selection?}` |

未知场景返回 422 `SCENARIO_NOT_FOUND`（不会静默退化成无题的通用 run）；未知题目 / 越界题数 / 未知等级 /
非正数输出上限同样在创建期 422。凭据字段一律拒绝（密钥只走凭据文件或 `api_key_env`）。

## 运行、失败与恢复

与 GSM8K 共用执行链路，语义完全相同：

- 暂时性错误（`rate_limit` / `server` / `timeout` / `network`）继续跑下一题且不重试；
  认证、客户端（端点/模型写错）、配置、协议与未知错误停跑，剩余题目记为 `not_attempted`。
- 任一题失败会让最终 run 落 `failed`，但已成功应答的题照常评分。取消在 Worker 前生效则零调用。
- `GET /api/v1/runs/{id}/report` 对终态运行也给结论（分母＝本次选中题数中的 `judged`），
  成本保留 price_table 版本与已知/未知题数；`rescore` 不调用模型，而是追加新的 ScoringPass。
- 重启跳过已持久化的题并尊重持久化的系统性停跑标记。若 CaseAttempt 停在 `dispatching`，远端结果
  不确定，恢复会转为 `indeterminate` 并把 Run 标为 `needs_review`，不会自动重复付费调用。
  `retry` 是显式操作，用同一份不可变快照创建子 Run，可能产生新费用。

## 离线验证

```bash
uv run pytest -q tests/contract/test_direct_llm.py tests/evaluators/test_direct_llm.py \
  tests/sdk/test_direct_llm.py tests/api/test_direct_llm_api.py \
  tests/cli/test_direct_llm_cli.py tests/runtime/test_direct_llm_smoke.py
make check
pnpm --dir apps/web test    # 控制台：操作/题目/过程/结果/对比五页
```

测试全部使用合成题目与内置样例，零网络零费用；`tests/conftest.py` 把 `MOTTE_DB_PATH`、
`MOTTE_DATASET_DIR`、`ARTIFACT_ROOT` 指向临时目录，API 测试一律注入内存 store，
因此跑测试不会污染开发者的 `var/runs.db` 或已导入的数据集。真实模型调用与费用由操作者自行发起。
