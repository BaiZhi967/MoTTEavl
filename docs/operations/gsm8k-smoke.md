# GSM8K local benchmark（smoke / full）

This is a pipeline benchmark, not a leaderboard claim. Two scopes exist, both imported from the same
official `grade_school_math/data/test.jsonl` at a pinned commit:

- **smoke** — preset `gsm8k-20`, version 1: the **first 20 rows in file order**.
- **full** — preset `gsm8k-full`, version 1: **every row of the test split**. The row count is not a
  constant: it is pinned into the immutable dataset record at import time and becomes the run/scoring
  denominator. There is no shuffle or sampling in either scope.

The repository contains **only generated synthetic test fixtures**, not official questions or answers,
and no real-model benchmark result is claimed. A dataset imported from the synthetic fixtures is marked
`synthetic: true` and the console shows a warning on its card; never read such a run as a GSM8K score.

## Obtain and pin the source (operator action)

Obtain `grade_school_math/data/test.jsonl` from the official `openai/grade-school-math` repository, at a
specific full 40-character Git commit. Preserve the original bytes and upstream LICENSE (MIT in that
repository; verify the license at your pinned revision and record it). Use the test split, not train or a
reformatted mirror. Record the commit and your source-file SHA-256 independently. The importer records your
declared revision/license; it cannot verify offline that a file actually came from that commit. A
syntactically valid hash is not proof of authenticity.

`benchmark download` and the console import both fetch the file for you and store the raw bytes under
`var/datasets/gsm8k/test-<revision>.jsonl` (gitignored, `MOTTE_DATASET_DIR` override) so the exact question
text of a run can be checked afterwards. Do not replace a locally obtained file with this document's
synthetic fixtures. `--synthetic` is for development only, and remains visible in the stored
manifest/report.

Each nonblank source record must have exactly string `question` and `answer` fields. The answer must end in
`#### <number>`. Blank lines, invalid UTF-8, bad answers, extra fields and malformed rows **anywhere in the
file**, including after the selected subset, are rejected. A trailing newline is allowed. At least the
preset selected count is required (20 for smoke; at least one row for full).

## Download, import and run

默认动作是「下载官方最新全量数据集」：省略 commit 时先解析官方仓库中该数据文件的最新 commit（GitHub
commits API，未认证 60 次/小时；文件查询为空则退回默认分支 HEAD），再按解析出的固定 sha 下载，因此「最新」
只是一次解析动作，落库的 provenance 仍是可复核的 40 位 revision，而不是浮动分支。源不可达/被墙时失败并提示
改用显式 40 位 commit（不会静默换成别的东西）。省略版本号时自动选版本：**同内容已存在则复用其版本**（重复点
「下载最新」不会攒版本号），否则取下一个数字空号，所以全量与冒烟能自动分开占版本（`@1` 全量 / `@2` 冒烟）。

### Web 控制台（推荐）

`make dev` 后打开 `/gsm8k` 操作页，四张卡：数据集（pinned）、模型（多选对比）、跑测参数（preset 固定）、
下载并导入数据集。

- 数据集卡展示当前数据集（场景名、数据集名@版本、范围、题数、revision 前缀、synthetic 提示）。存在多个
  数据集时卡内出现「运行数据集」下拉（原生 select，与过滤控件同规格）：默认选中题数最多的数据集（全量优先，
  同题数时冒烟在前），选项与发起按钮都带题数，切到冒烟即改成 20 题的小跑。
- 下载卡默认只有一个按钮「下载最新全量数据集」，点了就是官方最新 commit + 整个 test split + 自动版本号。
  「高级设置」折叠区（默认收起）里可改：题目范围（全量 / 冒烟前 20 题）、官方仓库 commit（留空 = 自动解析
  最新）、数据集名、数据集版本（留空 = 自动）、License；按钮文案随 commit 是否填写变化。下载后服务端存源文件
  到 `var/datasets/gsm8k/`、校验整个 split，再创建不可变数据集与场景
  （`<name>-smoke@<version>` / `<name>-full@<version>`），并自动把该数据集选为当前运行数据集；回执里带本次真正
  使用的 revision 前缀。版本冲突等错误就地显示（409 提示换版本号）。
- 发起后为每个模型创建一个 run（个别创建失败就地列示，不阻塞整批），自动进入 `/gsm8k/monitor` 批次过程页：
  每个 run 一行（ID、模型、状态、进度、取消/重试），展开可见逐题网格与 SSE 实时事件流。全部终态后出现
  「查看对比结果」入口（`/gsm8k/compare`）；单 run 指标卡与逐题钻取在 `/gsm8k/runs/{id}/result`。

对应接口：`GET /api/v1/benchmarks/gsm8k`（items 含 `scope`、`cases`、`benchmark`、`provenance`）、
`POST /api/v1/benchmarks/gsm8k/import`（body `{license, revision?, scope?: "smoke"|"full", name?, version?}`，
省略 `revision` 即解析最新、省略 `version` 即自动版本；回执含 `revision`/`scope`/`cases`）、
`POST /api/v1/benchmarks/gsm8k/runs`（body `{model, scenario}`，省略 `scenario` 时默认 `<name>-full@1`；
场景不存在返回 422 `SCENARIO_NOT_FOUND`，不会静默退化成无题的通用 run）。同内容重复导入幂等，内容冲突返回 409。

### CLI

Run from the project directory. Use the same SQLite path/environment for CLI, API and Worker. Stop any Worker
before preparing data if you do not want an already-running Worker to start a queued paid job.

从官方仓库直接下载（默认：解析官方最新 commit + 全量题目 + 自动版本号）：

```bash
export MOTTE_DB_PATH=./var/gsm8k.db
uv run python -m motte_cli benchmark download --license MIT
```

输出（stdout 单行 JSON）：

```json
{"imported": "gsm8k-test@1", "scenario": "gsm8k-test-full@1", "scope": "full",
 "benchmark": "gsm8k-full", "cases": 1319, "revision": "5d0b5c9a...",
 "source_sha256": "...", "cases_sha256": "..."}
```

需要精细控制时用可选参数：`--revision <40 位 commit>`（钉住指定 revision，省略即解析官方最新）、
`--scope smoke`（只取前 20 题）、`--name`、`--version`（省略即自动：同内容复用，否则下一个空号）、
`--split`（契约当前仅 test）、`--source-dir`、`--db`。下载进度与源文件落盘路径写在 stderr，stdout 始终只有
一行 JSON。失败时退出码 2，stderr 最后一行是错误 JSON：`SOURCE_UNAVAILABLE`（源不可达/非 200/超时，
或最新 commit 解析不出来，消息里会提示改用显式 commit）、`CONTRACT_INVALID`（revision 不是 40 位十六进制、
文件校验失败、scope 未登记）、`RESOURCE_CONFLICT`（显式钉住的版本已存在不同内容，换 `--version` 或省略它）。

已有本地文件时用 `import`（离线，`--revision` 必填；默认同样是全量，`--scope smoke` 取前 20 题）：

```bash
uv run python -m motte_cli benchmark import \
  --file ./local-data/test.jsonl --revision YOUR_FULL_40_CHARACTER_COMMIT_HASH --license MIT
```

Both commands validate the entire file, store the selected dataset version and create the scenario. Identical
re-import is idempotent: with an explicit `--version` the same `name@version` must match byte-for-byte, while an
omitted version reuses whatever version already holds the same selected cases. The same `name@version` cannot
hold two scopes (`smoke@1` and `full@1` select different cases); with automatic versioning the second scope
simply lands on the next free number (`full@1` + `smoke@2`), and only an explicitly pinned duplicate version is
refused with 409. GSM8K dataset/scenario versions cannot be overwritten or deleted via resource CRUD; unrelated
resources retain upsert behavior. Dataset and scenario are separate durable writes: if scenario creation fails,
the valid dataset may remain; fix the conflict with a new version or re-run an identical import.

Configure a provider connection/model profile using existing resource APIs, then enqueue:

```bash
uv run python -m motte_cli benchmark run --scenario gsm8k-test-full@1 --model YOUR_MODEL_PROFILE_ID
uv run python -m apps.worker.motte_worker --once
```

Worker execution writes one JSON object per line to stderr, so a long run can be watched without exposing model content:

```bash
uv run python -m apps.worker.motte_worker --once 2>worker-progress.jsonl
# PowerShell: uv run python -m apps.worker.motte_worker --once 2>worker-progress.jsonl
```

Each line identifies the run and event; case progress includes `ordinal`, `total`, `duration_ms`, and, when
available, numeric token/cost/retry summaries. The stream deliberately excludes prompts, gold answers, response
content, canonical provider payloads, error messages, and credentials. Use `--quiet` for a silent worker. The
persisted event trace and full evidence remain available through the monitor page or
`GET /api/v1/runs/{id}/events`; stderr is an operational progress summary, not a replacement for that trace.

Alternatively use a complete provider configuration in a local JSON file (no plaintext key):

```json
{"kind":"openai_compatible","base_url":"http://localhost:8001/v1","model":"your-model","api_key_env":"OPENAI_API_KEY"}
```

```bash
uv run python -m motte_cli benchmark run --scenario gsm8k-test-full@1 --provider @provider.json
```

`--provider` takes a provider object, **not** `{"provider": {...}}`. It also accepts a provider connection
resource name if that connection includes the legacy model field. Prefer `--model` for separate
connection/profile resources. Credentials use the existing environment/profile chain, never enter the
snapshot. `--db PATH` is supported for both benchmark subcommands. On PowerShell use
`$env:MOTTE_DB_PATH = './var/gsm8k.db'` instead of `export`.

Enqueueing is offline; **a running Worker may immediately execute a paid provider**. No automatic model calls
occur in download, import or preflight. Actual execution is the operator's choice.

The generic CLI `run --spec` and API `POST /api/v1/runs` accept the same preparation input:

```json
{"scenario_version":"gsm8k-test-full@1","manifest":{"model":"YOUR_MODEL_PROFILE_ID"}}
```

No case IDs or prompt/answer list is needed. Selected IDs, prompts, raw dataset snapshot, source provenance,
scenario, provider parameters and price table are copied at creation. Worker/retry/rescore do not re-resolve
resources. Client overrides of selected cases, tools or reserved snapshots are rejected. Gold numeric answers
live only in the evaluation snapshot; provider-facing cases contain only prompts, and the original gold
reasoning is not retained.

### Cost and duration of a full run

A full run makes one paid call per selected case per model — with the official split that is thousands of
sequential calls, so plan for hours and for the token/cost total, not minutes. Cases are executed in order,
results are durable per case, and an interrupted run resumes without repeating persisted cases, so a long run
can be stopped and continued (`make worker` after a restart). Cancel from the console or `POST /api/v1/runs/{id}/cancel`
stops further calls; the report still scores the cases that already ran. The per-case grid and drill table
render every selected case, so a full result page is a large page by design.

##题目子集与思考强度（每次运行可选）

数据集版本不可变；**每次运行**可以再选一个子集，选择与随机种子都会写进运行快照：

| `case_selection.mode` | 含义 | 快照记录 |
|---|---|---|
| `all`（默认） | 整份数据集 | `{"mode": "all", "count": N, "seed": null}` |
| `ids` | 指定题目（必须都在数据集里，重复自动去重，按数据集顺序执行） | `{"mode": "ids", "count": N, "seed": null}` |
| `random` | 随机抽 N 题 | `{"mode": "random", "count": N, "seed": "<hex>"}` |

- 子集运行的 **report 分母是子集题数**（`summary.selected`、accuracy、completion 全部按子集算），表里同时
  保留数据集级 `selected_count`，两者不混用；`run_selection.case_ids_sha256` 与运行行里的 `case_ids` 互为凭据。
- `random` 用 `random.Random(int(seed, 16))` 抽样，**同一 seed + 同一数据集必得同一子集**：省略 seed 时服务端
  生成 8 字节 hex 并回填进快照，因此任何随机运行都可复现。想换一批题就换 seed。
- 子集只影响本次运行，不改数据集；不同子集的运行在对比页会提示「各列选中的题目数不同」，accuracy 不可直接横比。
- **思考强度**（`reasoning_level`）按模型档案声明的等级取值（`low`/`high` 等由档案定义，控制映射是档案里的
  CEL）；未声明支持推理的模型不接受该字段。注意本基准把 `max_output_tokens` 钉在 1024：高思考强度会把预算
  耗在思考上，正文可能为空并记为 `parse_failure`（deepseek 一类推理模型在 `max` 档实测 16/20 题如此），
  要出数就选非推理模型或较低等级。

### Web

- 操作页新增「题目」卡：范围 = 全部（默认）/ 随机 N 题（填题数 + 种子，可重新生成）/ 指定题目（读取「题目」
  页的勾选，显示已选题数，数据集不匹配时提示并禁用发起）；发起按钮显示的是**本次真实题数**（如 `1 个模型 × 100 题`）。
- 「题目」页（操作页 → 浏览 / 勾选题目，路由 `/gsm8k/cases`）：按数据集浏览全部题目（题面、期望答案、case id），
  支持关键词 / id 搜索、分页、逐题勾选、全选本页、清空、粘贴 case id（按 `gsm8k-test-####` 与数据集题数精确校验，
  越界与格式错误的 id 会提示并剔除）。勾选后用「用所选 N 题发起跑测」回到操作页，勾选经 sessionStorage 传递，
  避免把上千个 id 塞进 URL。
- 模型卡：勾选支持推理的模型后就地出现「思考强度」下拉（默认 = 档案默认等级），每个模型各自提交自己的
  `reasoning_level`；下方提示 1024 输出预算的截断风险。

### API / CLI

```bash
# 指定题目
uv run python -m motte_cli benchmark run --scenario gsm8k-test-full@1 --model YOUR_MODEL \
  --case-ids gsm8k-test-0001,gsm8k-test-0042
# 随机 100 题（记录种子，可复现；省略 --seed 则自动生成并写进快照）
uv run python -m motte_cli benchmark run --scenario gsm8k-test-full@1 --model YOUR_MODEL \
  --random 100 --seed deadbeef
# 思考强度（等级名来自模型档案）
uv run python -m motte_cli benchmark run --scenario gsm8k-test-full@1 --model YOUR_MODEL \
  --random 100 --reasoning-level low
```

`POST /api/v1/benchmarks/gsm8k/runs` 的 body 支持 `{"case_selection": {...}, "reasoning_level": "low"}`；
`GET /api/v1/benchmarks/gsm8k/cases?dataset=<name@version>&offset=0&limit=50&query=<关键词>` 分页只读浏览题目
（控制台「题目」页用的就是它）。`case_ids` 参数不再被 benchmark 运行接受（子集统一走 `case_selection`），
未知题目 / 越界题数 / 未知等级 / 未知模式都在创建期返回 422，不产生任何付费调用。

## Presets and scorer

Both presets are version 1, zero-shot prompt `gsm8k-zero-shot-v1`, scorer `gsm8k-final-decimal-v1`, explicit
`max_output_tokens=1024`, transport `max_retries=0`:

| scope | preset id | selection | selected count |
|---|---|---|---|
| smoke | `gsm8k-20` | `first-n-in-file-order` | 20 (fixed) |
| full | `gsm8k-full` | `all-rows-in-file-order` | source row count (pinned at import) |

These two limits override provider/profile parameter defaults, within the model's supported ceiling. They do
not lift the model profile's hard ceiling: the canonical `max_output_tokens` field (or its legacy `parameters`
fallback) is still enforced, and a profile whose ceiling is below 1024 is rejected at creation
(`RUN_CONFIG_INVALID: max_output_tokens exceeds model ceiling ...`) by CLI `benchmark run`, CLI `run --spec`
and the API alike, before any paid call. Use a model profile with `max_output_tokens` >= 1024 (for example
2048) for these presets. Reasoning models that spend the whole 1024-token output budget on reasoning return
empty content and are scored as parse failures; pick a non-reasoning model or a profile whose reasoning level
leaves output budget. Other supported parameters remain visible in the provider snapshot. The presets do
**not** promise deterministic model output or a monetary cap. Output tokens bound response length, not total
input charges or unknown pricing. Missing pricing stays unknown, not free.

The final nonblank output line must be exactly `#### <number>` (trailing horizontal whitespace allowed, no
leading indentation). Signed integers/decimals and properly grouped thousands work: `#### +1,234.50`,
`#### -.25`. Decimal comparison is exact, without float conversion. NaN/Infinity, exponents, currency, units,
fractions, malformed commas and trailing commentary fail parsing. There is no last-number fallback.
`finish_reason=length` is still scored from the returned content: no strict final answer means parse failure.

## Outcomes and errors

- `correct`, `wrong_answer`, `parse_failure`: a successful response was received and scored.
- `call_failed`: the case invocation failed; its existing evidence envelope is retained, including metering/usage/cost when provided.
- `not_attempted`: no invocation was made (systemic stop, cancellation, preflight rejection, or currently pending).

Any call failure makes the final run `failed`, but successful responses before/after it are still scored.
Rate-limit, server, timeout and network errors continue with the next case, without retry. Auth, client (bad
endpoint/model/request), configuration, protocol and unknown failures stop; remaining selected cases become
not attempted. Worker construction failures use existing `unsupported` status, with all cases not attempted.
Cancellation before Worker emits the terminal cancellation event and makes no calls.

## Report, rescore, restart

`GET /api/v1/runs/{id}/report` returns accuracy/pass_rate = correct / **selected count**, even on
failed/cancelled/unsupported runs. The denominator is the run's own selection (the `case_selection` subset when
one was used, otherwise the dataset's `selected_count`: 20 for smoke, the source row count for full), never a
global constant. `attempted` includes failed calls; `responded` counts
successful model envelopes (including wrong/parse failures). `completion` = responded / selected;
`attempt_rate` = attempted / selected. Unknown outcomes never disappear from the denominator. Cost retains
price-table versions, known partial total (zero remains zero), and known/unknown case counts; usage sums only
existing token fields. Per-case envelopes remain available for metering and incomplete usage. A partial cost
total is not the cost of a fully executed selection.

`POST /api/v1/runs/{id}/rescore` works for completed or terminal benchmark runs and never calls a provider. It
uses the pinned snapshot/scorer version, appends an immutable ScoringPass/score set, and moves only the Run's
`current_scoring_pass_id`; old passes remain selectable from the report API. Legacy non-benchmark rescore still
requires completed status.

Restart skips durably stored case results and respects a durable systemic-stop marker. Each external call is
persisted as a CaseAttempt before dispatch. If the process dies while an attempt is `dispatching`, the outcome
may already exist remotely; recovery marks the attempt `indeterminate` and the Run `needs_review` instead of
automatically repeating the paid call. `retry` is an explicit operator action that creates a child Run with the
same immutable snapshot and may incur new charges. The execution lock prevents concurrent recovery by a second
Worker.

## Offline verification

```bash
uv run pytest -q tests/contract/test_gsm8k.py tests/api/test_gsm8k_import.py \
  tests/cli/test_benchmark_download.py tests/sdk/test_gsm8k_source.py tests/runtime/test_gsm8k_smoke.py
make check
pnpm --dir apps/web test    # 控制台：题目页、题目选择、思考强度
```

Fixtures are generated from explicitly labelled synthetic questions, not official GSM8K content. Coverage
includes strict decimals, entire-file validation, immutable versions, both scopes (every-row selection and the
pinned full denominator), run-level subsets (`all`/`ids`/`random` with deterministic seeds, subset denominators,
rejected unknown ids and out-of-range counts), API/CLI parity, latest-commit resolution (file commit, HEAD
fallback, transport failure hint), automatic version reuse/next-free, explicit-version conflicts, mocked
`benchmark download` with source landing, no gold in requests, partial/systemic failures, selected denominator,
pre-Worker cancellation, restart and no-call rescore. `tests/conftest.py` points `MOTTE_DB_PATH`, `MOTTE_DATASET_DIR` and
`ARTIFACT_ROOT` at a temporary directory for the whole session, and the API tests build the app with in-memory
stores: running the test suite never writes the developer's `var/runs.db`, `var/datasets/` or artifacts. Live
provider verification and official source authenticity remain operator responsibilities.
