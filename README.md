# MoTTEavl

MoTTEavl is a single-user platform for evaluating LLMs, agents, and harnesses with a versioned evidence contract.

See the [design specification](docs/superpowers/specs/2026-09-14-llm-agent-harness-evaluation-platform-design.md), the [implementation plan](docs/superpowers/plans/2026-09-14-motteavl-platform-implementation-plan.md), and the [next-phase task plan](docs/superpowers/plans/2026-09-15-next-phase-task-plan.md).

## Prerequisites

Versions are pinned in the repo: Python 3.12 (`.python-version`), Node.js 24 (`.nvmrc`), pnpm 9.15.0 (`package.json` → `packageManager`), uv (`pyproject.toml` → `[tool.uv] required-version`). Windows is supported through Docker/WSL2 (see the [runtime safety ADR](docs/superpowers/specs/adr/2026-09-14-runtime-safety-and-execution-policy.md)).

## Setup

```
make install        # uv sync + pnpm install --frozen-lockfile
```

## Daily commands

| Command | What it runs |
|---|---|
| `make test` | `uv run pytest -q -m "not live"` (full suite, zero network/cost) |
| `make lint` | ruff + compileall |
| `make replay` | deterministic replay tests only |
| `make web-build` | `pnpm --dir apps/web build` |
| `make check` | lint + test + web build + compose config (same gates as CI) |
| `make dev` | Supervised API startup, then Web after API health is ready (Ctrl-C stops both) |

## Running locally

- **API**: `uv run uvicorn apps.api.app.main:app --reload --port 8000` (or just `make dev`). Reads `MOTTE_DB_PATH` (default `./var/runs.db`, SQLite).
- **Web**: `pnpm --dir apps/web dev` — Vite on `http://localhost:5173`, `/api` is proxied to the API on port 8000.
- **Worker**: `uv run python -m apps.worker.motte_worker` (or `make worker`) polls the durable queue, atomically claims queued Runs, and owns execution through `RunDispatcher`. Use `--once` to drain the queue and exit. Progress is emitted as one-line JSON objects to **stderr**; stdout stays available for scripts. Use `--quiet` to suppress progress output. Ctrl-C stops cleanly with exit status **130**. On restart, work interrupted before an external dispatch can be requeued; a persisted `dispatching` attempt has an uncertain outcome, so its Run becomes `needs_review` and is never called again automatically. SQLite uses a database-adjacent OS lock and PostgreSQL an advisory session lock; a second Worker fails fast instead of recovering a healthy executor's Runs. Logs omit prompts, gold answers, response content, provider payloads, and credentials. Celery/Redis dispatch is available in eager-tested form (`apps/worker/motte_worker/celery_app.py`); the default local mode needs no broker. See the [integrity operations guide](docs/operations/platform-integrity.md).
- **Optional services** (PostgreSQL, Redis, OTel collector): `docker compose -f infra/docker-compose.yml up -d`. Compose builds the `motteavl:local` image before starting API, migration, and Worker services.

`make dev` (equivalently `uv run python -m apps.dev`) uses a Python standard-library
supervisor on POSIX and native Windows; it does not use shell background jobs or
`kill 0`. Both loopback ports **8000 and 5173 must be free** before startup. Existing
listeners are never reused or stopped, and Vite cannot silently switch ports.
The reloading API starts first; Web starts only after `/health` returns `{"status":"ok"}`
with this launch's identity. Readiness has a **60-second deadline** with local-only
HTTP probes (ignoring proxy environment variables). Startup errors/timeouts or either
supervised child exiting stop the other server and return a nonzero status, with
child output and diagnostics left visible. Ctrl-C stops only this launch's process
trees (POSIX process groups / Windows Job Objects); Windows cleanup terminates those
processes rather than promising graceful application shutdown. API reloads after
initial startup may still briefly interrupt requests. No Worker or model calls are
started by this command. The combined launcher binds Web to `http://127.0.0.1:5173`.

## GSM8K benchmark (smoke / full)

Two scopes come from the same pinned official `test.jsonl`: **smoke** takes the first 20 rows (preset
`gsm8k-20`), **full** takes every row of the test split (preset `gsm8k-full`, row count pinned into the
dataset at import). Download the latest full dataset in one command (the commit is resolved to a fixed sha
and recorded; `--revision`/`--scope`/`--version` refine it):

```bash
uv run python -m motte_cli benchmark download --license MIT
uv run python -m motte_cli benchmark run --scenario gsm8k-test-full@1 --model YOUR_MODEL_PROFILE_ID
uv run python -m apps.worker.motte_worker --once
```

```bash
uv run python -m motte_cli benchmark import --file ./local-data/test.jsonl \
  --revision YOUR_FULL_40_CHARACTER_COMMIT_HASH --license MIT --scope full
```

The console (`/gsm8k`) has the same one-click download button plus an 高级设置 disclosure for scope, pinned
commit, dataset name/version and license; the server stores the raw source under `var/datasets/gsm8k/` before
creating an immutable dataset plus scenario (`gsm8k-test-smoke@1` / `gsm8k-test-full@1`). An omitted version
reuses the version holding the same cases (repeat downloads stay idempotent) and otherwise takes the next free
number, so the two scopes coexist without conflicts. The run card lets you choose which dataset to run.

Each run can narrow the dataset to a subset and pick a reasoning strength: `--case-ids a,b`, `--random N
[--seed HEX]` (the seed is recorded, so a random subset is reproducible) and `--reasoning-level LEVEL`. The
console exposes the same controls, plus a 题目 page (`/gsm8k/cases`) to browse every question of a dataset,
search it, tick cases and send that selection into the next run. Reports always divide by the cases that run
actually selected; note the preset pins `max_output_tokens=1024`, so a high reasoning level can spend the whole
budget on thinking and leave an empty answer.

Download/import/preparation are offline; Worker execution can incur provider charges, and a full run is one
paid call per selected case per model in sequence. Both scopes use a versioned strict Decimal scorer, an
explicit output-token limit and zero retries—not a monetary cap. See the [operator guide](docs/operations/gsm8k-smoke.md)
for provenance, scopes, credentials, API parity, partial-failure reports and restart/rescore semantics.
Repository fixtures are synthetic only, and the test suite is isolated from `var/runs.db`.

## Direct LLM evaluation (generic direct calls)

Direct LLM is the generic counterpart of GSM8K: the case input *is* the prompt (sent verbatim, no suite
suffix), the answer is judged by a deterministic, versioned scorer, and the same run/report/restart
machinery applies. The only structural difference is where data comes from—there is no upstream, so
datasets are imported from local JSONL (three small samples also ship in `datasets/direct-llm/`):

```bash
uv run python -m motte_cli direct-llm builtins
uv run python -m motte_cli direct-llm import --builtin direct-llm-classify
uv run python -m motte_cli direct-llm import --file ./my-set.jsonl --name my-set --scorer regex
uv run python -m motte_cli direct-llm run --scenario direct-llm-classify@1 --model YOUR_MODEL_PROFILE_ID
uv run python -m apps.worker.motte_worker --once
```

Each JSONL line is `{"input"|"prompt", "expected"?, "scorer"?, "case_id"?}`; the whole file is validated at
import and frozen into an immutable `name@version` (scenario name equals dataset name). Scoring is
`exact` / `contains` / `regex`, overridable per case. **Cases without an `expected` answer are recorded as
`no_expectation` and stay out of the accuracy denominator**, so the report always states its denominator
(`judged_cases` here, `selected_cases` for GSM8K). Unlike the GSM8K presets, the 1024-token output budget is
a run-level default here: `--max-output-tokens` can lower it, never above the model profile's ceiling.

The console (`/direct-llm`) mirrors the GSM8K suite with five pages: operate (dataset / cases / models /
parameters / builtin samples / local JSONL import), a 题目 page (`/direct-llm/cases`), the batch monitor, a
result page and a multi-model compare page. See the
[operator guide](docs/operations/direct-llm.md) and the
[sample dataset README](datasets/direct-llm/README.md).

## Environment variables

Copy `.env.example` to `.env`. Variables actually read today: `MOTTE_STORAGE` (`sqlite` default, or `postgres` for production), `MOTTE_DB_PATH` (SQLite location), `MOTTE_PG_DSN`/`DATABASE_URL` (PostgreSQL DSN, `postgresql+asyncpg://` prefixes accepted), `ARTIFACT_ROOT` (artifact store). Migrations use Alembic: `uv run alembic upgrade head` / `uv run alembic downgrade -1`.

## Model configuration

Model resource POST/PUT validates and normalizes profiles before storage. `input_modalities`
defaults to `["text"]` and must include `text`; supported declarations include `image`, `video`,
and `pdf` (other nonempty legacy modality names are preserved). Capability keys
`structured_output`, `native_search`, and `system_messages` are booleans, default false;
other capability keys are preserved. `supports_tools` retains its existing behavior.
These are support declarations, not switches that enable search, schema injection, or
multimodal execution. Full multimodal execution is not implemented by this change.

New ModelProfiles start as `draft`. Drafts can be edited and smoke-tested but cannot create
formal Runs; publish with `POST /api/v1/models/{id}/publish` or the Web console. Published
profiles are immutable and pinned into new ResolvedManifest v2 snapshots. Deletion deprecates
the profile instead of removing historical evidence. ProviderConnection remains mutable and
increments `generation` on every update.

`context_window` and `max_output_tokens` are nullable positive integers. Non-null top-level
`max_output_tokens` is the canonical request default **and ceiling**; only when null/missing
is legacy `parameters.max_output_tokens` used. Clear both to remove an old limit. Canonical
wins when both exist. `manifest.parameters.max_output_tokens` and per-request
`ModelRequest.max_output_tokens` may lower the limit, but exceeding it is an error before
network I/O. Adapter-specific parameter ranges still apply; Responses currently requires
at least 16 output tokens. Context window is declared metadata, not a tokenizer/truncator.

Reasoning profile fields are `supported: bool`, `levels: string[]`, `control: string|null`,
and `default_level: string|null`. Level names are configurable, not hardcoded per provider.
A non-null default must be in the supported levels and requires a mapping. Optional
`manifest.reasoning_level` selects a supported level instead of the default. Without either,
no reasoning patch is applied. The profile and chosen level are copied into the resolved
provider snapshot so later profile edits do not change an existing run.

`control` is real CEL, evaluated by **cel-python**, with the sole variable `reasoningLevel`
(string). It must return a JSON object. Examples (choose the one matching your endpoint):

```cel
{"reasoning_effort": reasoningLevel}
{"reasoning": {"effort": reasoningLevel}}
reasoningLevel == "high" ? {"thinking": {"type": "enabled", "budget_tokens": 1024}} : {"thinking": {"type": "disabled"}}
```

Save validates every declared level and returns actionable HTTP 422 on invalid CEL/output;
runtime validates again immediately before HTTP send. Merge order is profile parameter
(defaults) → manifest parameters → request parameters → CEL patch. The last merge is
**nonrecursive, shallow, top-level replacement**: a `reasoning` object replaces the entire
existing `reasoning` object, not individual children. Tools retain their existing adapter
mapping. Structural fields (`model`, `messages`, `input`, `system`, `instructions`, `tools`,
`tool_choice`, `stream`), auth/credential/header keys and output-limit keys (including API
aliases) are forbidden anywhere in patches. CEL cannot bypass the token ceiling.

Mappings allow literals, maps/lists, comparisons, conditionals, indexing and arithmetic;
function/method calls and macros are deliberately disallowed to bound execution. Limits:
4096 source bytes, 1024 AST nodes/depth 96, 32 unique levels of up to 64 characters each,
JSON output depth 8/256 values/8192 bytes. No I/O functions are exposed. Old descriptive
controls `reasoning_effort`, `reasoning.effort`, `thinking` remain inert metadata when no
level is selected; selecting a level requires replacing them with CEL. Arbitrary unsupported
control strings are rejected, never silently executed. Endpoint-specific semantics (for
example a thinking budget's minimum) remain the endpoint's responsibility.

`POST /api/v1/models/{id}/test` uses the same profile resolution/factory and accepts optional
`reasoning_level` alongside `prompt`. It bounds output to `min(16, profile ceiling)`;
configuration errors are 422, and an incompatible endpoint minimum may reject the test.
This endpoint performs a real call only when explicitly invoked by an operator. Backend
tests capture requests with fake transports and make no paid calls.

## CI

`.github/workflows/ci.yml` runs exactly the same commands as `make check` plus the frozen pnpm install; keep the two in sync when changing gates.

The current execution adapters are explicitly tracked in the [compatibility matrix](docs/protocols/provider-compatibility.md). `direct-llm@1` and `replay@1` are available execution backends; `external-benchmark@1` is versioned but marked unavailable until an adapter is connected. OpenAI-compatible, Responses, and Anthropic Provider adapters have offline fixture coverage; live endpoint validation is operator-triggered. Pi and CLI Harness surfaces are protocol/probe integrations only and remain unavailable for Run execution until a backend consumer is connected.
