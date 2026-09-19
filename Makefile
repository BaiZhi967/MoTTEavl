.PHONY: install test lint audit replay worker web-build web-test compose-config check dev clean

# 统一开发入口；与 CI 使用完全相同的命令（见 .github/workflows/ci.yml）

install:
	uv sync
	pnpm install --frozen-lockfile

test:
	uv run pytest -q -m "not live"

lint:
	uv run ruff check .
	uv run mypy packages/contracts
	uv run python -m compileall -q packages apps

audit:
	uv run pip-audit --skip-editable
	pnpm --dir apps/web audit --audit-level high

replay:
	uv run pytest -m replay -q

worker:
	uv run python -m apps.worker.motte_worker

web-build:
	pnpm --dir apps/web build

# 导出 OpenAPI 规范并重新生成 TypeScript 类型（CI 校验无漂移）
openapi:
	uv run python -c "import json; from apps.api.app.main import app; open('api/openapi.json', 'w', encoding='utf-8').write(json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + '\n')"
	pnpm --dir apps/web gen:api

# 发布契约一致性（review R2-12）：重新生成 openapi.json 与提交产物 diff，
# 漂移即失败——公共接口演进不能只更新 Python 路由。
openapi-check:
	uv run python scripts/check_openapi.py

web-test:
	pnpm -r test

compose-config:
	docker compose -f infra/docker-compose.yml config -q

check: lint test web-build web-test compose-config openapi-check

# API 健康检查通过后启动 Web；监督子进程，Ctrl-C 仅清理本次启动的进程树
dev:
	uv run python -m apps.dev

clean:
	rm -rf .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -not -path './node_modules/*' -not -path './.venv/*' -exec rm -rf {} +
