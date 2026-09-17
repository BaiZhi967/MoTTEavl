# GSM8K-20 local smoke benchmark

This is a pipeline smoke check, not a representative full GSM8K leaderboard score. It selects the **first 20 test rows in source file order**, without shuffle or sampling. No dataset is fetched by these commands. This repository contains **only generated synthetic test fixtures**, not official questions or answers, and no real-model benchmark result is claimed.

## Obtain and pin the source (operator action)

Obtain `grade_school_math/data/test.jsonl` from the official `openai/grade-school-math` repository, at a specific full 40-character Git commit. Preserve the original bytes and upstream LICENSE (MIT in that repository; verify the license at your pinned revision and record it). Use the test split, not train or a reformatted mirror. Record the commit and your source-file SHA-256 independently. The importer records your declared revision/license; it cannot verify offline that a file actually came from that commit. A syntactically valid hash is not proof of authenticity.

Do not replace the local file with this document's synthetic fixtures. Tests mark synthetic provenance explicitly. `--synthetic` is for development only, and remains visible in the stored manifest/report.

Each nonblank source record must have exactly string `question` and `answer` fields. The answer must end in `#### <number>`. Blank lines, invalid UTF-8, bad answers, extra fields and malformed rows **anywhere in the file**, including after the selected subset, are rejected. A trailing newline is allowed. At least the preset selected count is required.

## Import and run (no hand-built cases)

### Web 控制台（推荐）

`make dev` 后打开 `/gsm8k` 操作页：数据集卡展示当前 pinned 数据集（场景名、题数、revision 前缀）；尚无数据集时页面即导入表单，填官方仓库 40 位 commit 与 license，点「下载并导入」——服务端从 `raw.githubusercontent.com` 拉 `grade_school_math/data/test.jsonl`，源文件存到 `var/datasets/gsm8k/`（已 gitignore），校验后创建 dataset 与 `gsm8k-test-smoke@1` 场景。同版本重复导入幂等，内容冲突返回 409。跑测参数卡为 preset 固定值只读展示（题数 / 输出上限 1024 / 零重试），不提供 `temperature`/`max_tokens` 配置。模型卡（ModelPicker）按 Provider 分组多选，发起后为每个模型创建一个 run（个别创建失败就地列示，不阻塞整批），自动进入 `/gsm8k/monitor` 批次过程页：每个 run 一行（ID、模型、状态、进度、取消/重试），展开可见 20 格逐题网格与 SSE 实时事件流。全部终态后出现「查看对比结果」入口（`/gsm8k/compare`，accuracy / tokens / 成本 / 答错题重合）；单 run 指标卡与逐题钻取在 `/gsm8k/runs/{id}/result`。对应接口：`GET/POST /api/v1/benchmarks/gsm8k`、`/import`、`/runs`。

### CLI（离线/已有本地文件时）

Run from the project directory. Use the same SQLite path/environment for CLI, API and Worker. Stop any Worker before preparing data if you do not want an already-running Worker to start a queued paid job.

```bash
export MOTTE_DB_PATH=./var/gsm8k.db
uv run python -m motte_cli benchmark import \
  --file ./local-data/test.jsonl --name gsm8k-test --version 1 \
  --revision YOUR_FULL_40_CHARACTER_COMMIT_HASH --license MIT
```

Import validates the entire local file, stores the selected dataset and creates `gsm8k-test-smoke@1`. Output includes both reference names, selected count, source SHA-256 and canonical selected-cases SHA-256. Identical re-import is idempotent. Different bytes/provenance/selection under the same version are a conflict; use a new version. GSM8K dataset/scenario versions cannot be overwritten or deleted via resource CRUD; unrelated resources retain upsert behavior. Dataset and scenario are separate durable writes: if scenario creation fails, the valid dataset may remain; fix the conflict with a new version or re-run an identical import.

Configure a provider connection/model profile using existing resource APIs, then enqueue:

```bash
uv run python -m motte_cli benchmark run --scenario gsm8k-test-smoke@1 --model YOUR_MODEL_PROFILE_ID
uv run python -m apps.worker.motte_worker --once
```

Alternatively use a complete provider configuration in a local JSON file (no plaintext key):

```json
{"kind":"openai_compatible","base_url":"http://localhost:8001/v1","model":"your-model","api_key_env":"OPENAI_API_KEY"}
```

```bash
uv run python -m motte_cli benchmark run --scenario gsm8k-test-smoke@1 --provider @provider.json
```

`--provider` takes a provider object, **not** `{"provider": {...}}`. It also accepts a provider connection resource name if that connection includes the legacy model field. Prefer `--model` for separate connection/profile resources. Credentials use the existing environment/profile chain, never enter the snapshot. `--db PATH` is supported for both benchmark subcommands. On PowerShell use `$env:MOTTE_DB_PATH = './var/gsm8k.db'` instead of `export`.

Enqueueing is offline; **a running Worker may immediately execute a paid provider**. No automatic model calls occur in import or preflight. Actual execution is the operator's choice.

The generic CLI `run --spec` and API `POST /api/v1/runs` accept the same preparation input:

```json
{"scenario_version":"gsm8k-test-smoke@1","manifest":{"model":"YOUR_MODEL_PROFILE_ID"}}
```

No case IDs or prompt/answer list is needed. Selected IDs, prompts, raw dataset snapshot, source provenance, scenario, provider parameters and price table are copied at creation. Worker/retry/rescore do not re-resolve resources. Client overrides of selected cases, tools or reserved snapshots are rejected. Gold numeric answers live only in the evaluation snapshot; provider-facing cases contain only prompts, and the original gold reasoning is not retained.

## Preset and scorer

Preset `gsm8k-20`, version 1: first 20, zero-shot prompt `gsm8k-zero-shot-v1`, scorer `gsm8k-final-decimal-v1`, explicit `max_output_tokens=1024`, transport `max_retries=0`. These two limits override provider/profile parameter defaults, within the model's supported ceiling. They do not lift the model profile's hard ceiling: the canonical `max_output_tokens` field (or its legacy `parameters` fallback) is still enforced, and a profile whose ceiling is below 1024 is rejected at creation (`RUN_CONFIG_INVALID: max_output_tokens exceeds model ceiling ...`) by CLI `benchmark run`, CLI `run --spec` and the API alike, before any paid call. Use a model profile with `max_output_tokens` >= 1024 (for example 2048) for this preset. Other supported parameters remain visible in the provider snapshot. The preset does **not** promise deterministic model output or a monetary cap. Output tokens bound response length, not total input charges or unknown pricing. Missing pricing stays unknown, not free.

The final nonblank output line must be exactly `#### <number>` (trailing horizontal whitespace allowed, no leading indentation). Signed integers/decimals and properly grouped thousands work: `#### +1,234.50`, `#### -.25`. Decimal comparison is exact, without float conversion. NaN/Infinity, exponents, currency, units, fractions, malformed commas and trailing commentary fail parsing. There is no last-number fallback. `finish_reason=length` is still scored from the returned content: no strict final answer means parse failure.

## Outcomes and errors

- `correct`, `wrong_answer`, `parse_failure`: a successful response was received and scored.
- `call_failed`: the case invocation failed; its existing evidence envelope is retained, including metering/usage/cost when provided.
- `not_attempted`: no invocation was made (systemic stop, cancellation, preflight rejection, or currently pending).

Any call failure makes the final run `failed`, but successful responses before/after it are still scored. Rate-limit, server, timeout and network errors continue with the next case, without retry. Auth, client (bad endpoint/model/request), configuration, protocol and unknown failures stop; remaining selected cases become not attempted. Worker construction failures use existing `unsupported` status, with all cases not attempted. Cancellation before Worker emits the terminal cancellation event and makes no calls.

## Report, rescore, restart

`GET /api/v1/runs/{id}/report` returns accuracy/pass_rate = correct / **selected count**, even on failed/cancelled/unsupported runs. `attempted` includes failed calls; `responded` counts successful model envelopes (including wrong/parse failures). `completion` = responded / selected; `attempt_rate` = attempted / selected. Unknown outcomes never disappear from the denominator. Cost retains price-table versions, known partial total (zero remains zero), and known/unknown case counts; usage sums only existing token fields. Per-case envelopes remain available for metering and incomplete usage. A partial cost total is not the cost of a fully executed selection.

`POST /api/v1/runs/{id}/rescore` works for completed or terminal benchmark runs and never calls a provider. It uses the pinned snapshot/scorer version, replaces score rows, and does not change execution evidence/status. Legacy non-benchmark rescore still requires completed status.

Restart resumes interrupted runs, skips durably stored case results, and respects a durable systemic-stop marker even if the process died before persisting final scores/status. A process crash **after a remote call but before local case persistence may repeat that call**: this is not exactly-once billing. `retry` creates a new run with the same immutable snapshot and re-executes the selection; it may incur new charges. Do not run recovery concurrently with a healthy Worker.

## Offline verification

```bash
uv run pytest -q tests/contract/test_gsm8k.py tests/runtime/test_gsm8k_smoke.py
make check
```

Fixtures are generated from explicitly labelled synthetic questions, not official GSM8K content. Coverage includes strict decimals, entire-file validation, immutable versions, API/CLI parity, twenty-case Worker execution with fake HTTP transport, no gold in requests, partial/systemic failures, selected denominator, pre-Worker cancellation, restart and no-call rescore. Live provider verification and official source authenticity remain operator responsibilities.
