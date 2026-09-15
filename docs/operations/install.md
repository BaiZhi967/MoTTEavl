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

可选服务（PostgreSQL / Redis / OTel collector）：`docker compose -f infra/docker-compose.yml up -d`。Compose 会先构建 `motteavl:local`，再启动 migration、API 与 Worker；不再使用未安装项目依赖的裸 `python:3.12-slim`。

## 日常启动

- API：`uv run uvicorn apps.api.app.main:app --reload --port 8000`
- Web：`pnpm --dir apps/web dev`（5173，`/api` 代理到 8000）
- 一键：`make dev`

各环境变量的消费状态见 `.env.example` 内注释。

Windows 原生环境只通过 Docker Desktop 或 WSL2 运行容器与 CLI Harness；进程树终止、PTY 和 rootless 能力差异会写入运行诊断。
