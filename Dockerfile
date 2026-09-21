FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/workspace/.venv/bin:${PATH}"

WORKDIR /workspace

COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY apps ./apps
COPY bridges ./bridges
COPY migrations ./migrations
COPY alembic.ini ./

RUN pip install --no-cache-dir "uv>=0.11.6,<0.13" \
    && uv sync --frozen --no-dev

COPY . .

EXPOSE 8000

# compose/编排探活：镜像自带健康检查，指向 API 的 /health（loopback）。
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
