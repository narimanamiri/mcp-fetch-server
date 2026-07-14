FROM python:3.12-slim AS builder

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir uv \
    && uv pip install --system .

FROM python:3.12-slim

RUN useradd --create-home --shell /bin/bash appuser
WORKDIR /app
USER appuser

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/mcp-fetch-server /usr/local/bin/mcp-fetch-server

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

CMD ["mcp-fetch-server", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
