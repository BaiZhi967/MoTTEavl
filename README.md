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
| `make dev` | API + Web dev servers together (Ctrl-C stops both) |

## Running locally

- **API**: `uv run uvicorn apps.api.app.main:app --reload --port 8000` (or just `make dev`). Reads `MOTTE_DB_PATH` (default `./var/runs.db`, SQLite).
- **Web**: `pnpm --dir apps/web dev` — Vite on `http://localhost:5173`, `/api` is proxied to the API on port 8000.
- **Worker**: not yet a standalone process; it currently runs in-process through tests and the RunService entry points. A durable worker entrypoint (`python -m motte_worker`) lands in Phase 1 of the task plan.
- **Optional services** (PostgreSQL, Redis, OTel collector): `docker compose -f infra/docker-compose.yml up -d`.

## Environment variables

Copy `.env.example` to `.env`. Variable status is documented there; the ones actually read today are `MOTTE_DB_PATH` (SQLite location) and `ARTIFACT_ROOT` (artifact store). The rest are reserved for later phases.

## CI

`.github/workflows/ci.yml` runs exactly the same commands as `make check` plus the frozen pnpm install; keep the two in sync when changing gates.
