FROM ghcr.io/astral-sh/uv:0.9.18 AS uv

FROM python:3.12-slim-bookworm

COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH=/app/.venv/bin:$PATH \
    CORTEX_HOST=0.0.0.0 \
    CORTEX_VAULT=/data/vault \
    CORTEX_AUTH_DB=/data/auth.sqlite \
    HOME=/data \
    FASTEMBED_CACHE_PATH=/data/cache/fastembed \
    HF_HOME=/data/cache/huggingface

EXPOSE 8765 8766
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8766/healthz', timeout=4)"]
CMD ["cortex", "serve"]
