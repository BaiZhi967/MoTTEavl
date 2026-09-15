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

RUN pip install --no-cache-dir "uv>=0.11.6,<0.12" \
    && uv sync --frozen --no-dev

COPY . .

EXPOSE 8000
