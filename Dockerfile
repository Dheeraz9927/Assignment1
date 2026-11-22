FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
# dependencies first (cached layer), then the project itself
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --no-dev --frozen --no-install-project
COPY docassist ./docassist
RUN uv sync --no-dev --frozen
COPY ui ./ui
COPY eval ./eval
EXPOSE 8000 8501
