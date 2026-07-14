#!/usr/bin/env bash
# Start the full Docker stack (MCP server + SearXNG + admin GUI)
# Usage: ./scripts/docker-up.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    echo "docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    echo "docker-compose"
  else
    echo "ERROR: Neither 'docker compose' nor 'docker-compose' found." >&2
    echo "Install Docker Engine and the Compose plugin, or docker-compose." >&2
    exit 1
  fi
}

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is not installed or not on PATH." >&2
  echo "Install Docker Engine: https://docs.docker.com/engine/install/" >&2
  exit 1
fi

COMPOSE="$(compose_cmd)"

ENV_FILE="${PROJECT_ROOT}/.env"
ENV_EXAMPLE="${PROJECT_ROOT}/.env.docker.example"

if [[ ! -f "${ENV_FILE}" ]]; then
  if [[ -f "${ENV_EXAMPLE}" ]]; then
    cp "${ENV_EXAMPLE}" "${ENV_FILE}"
    echo "Created .env from .env.docker.example — edit MCP_AUTH_TOKEN before exposing this stack."
  else
    echo "ERROR: Missing .env file. Copy .env.docker.example to .env and set MCP_AUTH_TOKEN." >&2
    exit 1
  fi
fi

if grep -qE '^\s*MCP_AUTH_TOKEN=change-me' "${ENV_FILE}"; then
  echo "WARNING: MCP_AUTH_TOKEN is still the default placeholder — change it in .env before production use." >&2
fi

mkdir -p "${PROJECT_ROOT}/workspace"

echo "Building and starting containers..."
${COMPOSE} up -d --build

echo ""
echo "Stack is up:"
echo "  MCP endpoint : http://127.0.0.1:8000/mcp"
echo "  Admin GUI    : http://127.0.0.1:8000/admin"
echo "  Health check : http://127.0.0.1:8000/health"
echo "  SearXNG      : http://127.0.0.1:8080"
echo ""
echo "View logs: ${COMPOSE} logs -f"
echo "Stop stack: ${COMPOSE} down"
