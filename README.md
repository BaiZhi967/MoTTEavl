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
- **Worker**: `uv run python -m apps.worker.motte_worker` (or `make worker`) — polls the durable SQLite queue (`MOTTE_DB_PATH`), claims queued runs, and resumes interrupted runs after a restart. Use `--once` to drain the queue and exit. Celery/Redis dispatch is available in eager-tested form (`apps/worker/motte_worker/celery_app.py`); the default local mode needs no broker.
- **Optional services** (PostgreSQL, Redis, OTel collector): `docker compose -f infra/docker-compose.yml up -d`.

## Environment variables

Copy `.env.example` to `.env`. Variables actually read today: `MOTTE_STORAGE` (`sqlite` default, or `postgres` for production), `MOTTE_DB_PATH` (SQLite location), `MOTTE_PG_DSN`/`DATABASE_URL` (PostgreSQL DSN, `postgresql+asyncpg://` prefixes accepted), `ARTIFACT_ROOT` (artifact store). Migrations use Alembic: `uv run alembic upgrade head` / `uv run alembic downgrade -1`.

## CI

`.github/workflows/ci.yml` runs exactly the same commands as `make check` plus the frozen pnpm install; keep the two in sync when changing gates.
