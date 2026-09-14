.PHONY: install test lint replay worker web-build web-test compose-config check dev clean

# 统一开发入口；与 CI 使用完全相同的命令（见 .github/workflows/ci.yml）

install:
	uv sync
	pnpm install --frozen-lockfile

test:
	uv run pytest -q -m "not live"

lint:
	uv run ruff check .
	uv run python -m compileall -q packages apps

replay:
	uv run pytest -m replay -q

worker:
	uv run python -m apps.worker.motte_worker

web-build:
	pnpm --dir apps/web build

web-test:
	pnpm -r test

compose-config:
	docker compose -f infra/docker-compose.yml config -q

check: lint test web-build web-test compose-config

# 同时启动 API（:8000）与 Web（:5173，/api 代理到 8000）；Ctrl-C 一并停止
dev:
	@trap 'kill 0' EXIT; \
	uv run uvicorn apps.api.app.main:app --reload --port 8000 & \
	pnpm --dir apps/web dev & \
	wait

clean:
	rm -rf .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -not -path './node_modules/*' -not -path './.venv/*' -exec rm -rf {} +
