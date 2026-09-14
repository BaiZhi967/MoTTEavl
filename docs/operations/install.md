# 安装

## 前置版本

仓库已钉住工具链版本：Python 3.12（`.python-version`）、Node.js 24（`.nvmrc`）、pnpm 9.15.0（`package.json` 的 `packageManager`）、uv（`pyproject.toml` 的 `[tool.uv] required-version`）。Windows 环境按运行安全 ADR 走 Docker/WSL2。

## 步骤

```
cp .env.example .env
make install        # = uv sync + pnpm install --frozen-lockfile
make test           # 全量测试（零网络、零花费）
make check          # 与 CI 相同的门禁
```

可选服务（PostgreSQL / Redis / OTel collector）：`docker compose -f infra/docker-compose.yml up -d`。

## 日常启动

- API：`uv run uvicorn apps.api.app.main:app --reload --port 8000`
- Web：`pnpm --dir apps/web dev`（5173，`/api` 代理到 8000）
- 一键：`make dev`

各环境变量的消费状态见 `.env.example` 内注释。
