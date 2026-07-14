FROM python:3.12-slim AS builder

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN pip install --no-cache-dir uv \
    && uv pip install --system .

FROM python:3.12-slim

LABEL org.opencontainers.image.title="mcp-fetch-server" \
      org.opencontainers.image.description="MCP web fetch server with admin GUI and SearXNG fallback support"

RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /workspace \
    && chown appuser:appuser /workspace

WORKDIR /app
USER appuser

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/mcp-fetch-server /usr/local/bin/mcp-fetch-server

ENV PYTHONUNBUFFERED=1 \
    FETCH_LOCAL_FILES_ROOT=/workspace

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

CMD ["mcp-fetch-server", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
