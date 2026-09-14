# MoTTEavl

MoTTEavl is a single-user platform for evaluating LLMs, agents, and harnesses with a versioned evidence contract.

See the [design specification](docs/superpowers/specs/2026-09-14-llm-agent-harness-evaluation-platform-design.md) and [implementation plan](docs/superpowers/plans/2026-09-14-motteavl-platform-implementation-plan.md).

## Local setup

Install Python 3.12+, `uv`, Node.js, and `pnpm`; run `uv sync` and `pnpm install`. Copy `.env.example` to `.env`, then start local services with `docker compose -f infra/docker-compose.yml up -d`.

Run the workspace health check with `uv run pytest tests/test_workspace_health.py -q`.
