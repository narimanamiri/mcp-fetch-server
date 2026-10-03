#!/usr/bin/env bash
#
# One-command setup for the MCP fetch server and its offline corpus stack.
#
# Takes a bare machine to a working install: fetches the project if it is not
# already here, installs dependencies, writes a configuration file with a
# freshly generated auth token, checks the local model, brings up the vector
# store, and verifies the result.
#
# Run it from inside a checkout:
#
#   ./scripts/install.sh
#
# ...or standalone on a machine with nothing but git and curl, in which case it
# clones the project first:
#
#   curl -fsSL https://raw.githubusercontent.com/narimanamiri/mcp-fetch-server/master/scripts/install.sh -o install.sh
#   bash install.sh --yes
#
# Re-running is safe. Nothing already configured is overwritten: an existing
# .env is left alone, models already pulled are skipped, and a running vector
# store is reused.

set -euo pipefail

REPO_URL="${MCP_REPO_URL:-https://github.com/narimanamiri/mcp-fetch-server.git}"
REPO_REF="master"
UV_INSTALLER="https://astral.sh/uv/install.sh"

MODELS_CHAT="gemma3:4b"
MODELS_EMBED="bge-m3"

# Defaults
INSTALL_DIR=""
NET_MODE="hybrid"
QDRANT_MODE="auto"       # auto | server | embedded
WITH_RERANK="no"
PULL_MODELS="yes"
CORPUS_PATH=""
INSTALL_WATCHER="no"
ASSUME_YES="no"
ALLOW_UV_INSTALL="ask"   # ask | yes | no

# ---------------------------------------------------------------- output

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  B=$'\033[1m'; DIM=$'\033[2m'; R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; Z=$'\033[0m'
else
  B=""; DIM=""; R=""; G=""; Y=""; Z=""
fi

step()  { printf '\n%s==>%s %s%s%s\n' "${G}" "${Z}" "${B}" "$1" "${Z}"; }
info()  { printf '    %s\n' "$1"; }
warn()  { printf '    %s!%s %s\n' "${Y}" "${Z}" "$1"; }
die()   { printf '\n%serror:%s %s\n' "${R}" "${Z}" "$1" >&2; exit 1; }
have()  { command -v "$1" >/dev/null 2>&1; }

confirm() {
  # $1 = prompt. Non-interactive runs must be explicit rather than assumed.
  [ "${ASSUME_YES}" = yes ] && return 0
  if [ ! -t 0 ]; then
    warn "not interactive and --yes was not given, so skipping: $1"
    return 1
  fi
  printf '    %s [y/N] ' "$1"
  read -r reply
  case "${reply}" in [yY]*) return 0 ;; *) return 1 ;; esac
}

usage() {
  cat <<EOF
${B}mcp-fetch-server installer${Z}

Usage: install.sh [options]

  --dir PATH          Where to install (default: this checkout, else ./mcp-fetch-server)
  --ref REF           Git branch or tag to clone (default: ${REPO_REF})
  --mode MODE         Corpus mode: online | offline | hybrid (default: ${NET_MODE})
  --qdrant MODE       Vector store: auto | server | embedded (default: ${QDRANT_MODE})
                        server   = Qdrant in Docker
                        embedded = Qdrant on local disk, no Docker needed
  --rerank            Also install the cross-encoder reranker (large: onnxruntime)
  --no-models         Do not pull Ollama models (~4.5 GB)
  --corpus PATH       Ingest this folder once setup finishes
  --watch             Install the folder watcher as a background service
  --yes               Assume yes to prompts (needed for unattended runs)
  --no-uv-install     Fail instead of installing uv when it is missing
  -h, --help          Show this help

Examples:
  ./scripts/install.sh
  ./scripts/install.sh --yes --mode offline --qdrant embedded
  ./scripts/install.sh --yes --corpus ~/documents --watch
EOF
}

# ---------------------------------------------------------------- args

while [ $# -gt 0 ]; do
  case "$1" in
    --dir)            INSTALL_DIR="${2:?--dir needs a path}"; shift 2 ;;
    --ref)            REPO_REF="${2:?--ref needs a value}"; shift 2 ;;
    --mode)           NET_MODE="${2:?--mode needs a value}"; shift 2 ;;
    --qdrant)         QDRANT_MODE="${2:?--qdrant needs a value}"; shift 2 ;;
    --corpus)         CORPUS_PATH="${2:?--corpus needs a path}"; shift 2 ;;
    --rerank)         WITH_RERANK="yes"; shift ;;
    --no-models)      PULL_MODELS="no"; shift ;;
    --watch)          INSTALL_WATCHER="yes"; shift ;;
    # --yes only *defaults* the uv question to yes. It must not override an
    # explicit --no-uv-install, which would make the result depend on flag
    # order and silently download and run a remote installer.
    --yes|-y)         ASSUME_YES="yes"
                      [ "${ALLOW_UV_INSTALL}" = ask ] && ALLOW_UV_INSTALL="yes"
                      shift ;;
    --no-uv-install)  ALLOW_UV_INSTALL="no"; shift ;;
    -h|--help)        usage; exit 0 ;;
    *)                die "unknown option: $1  (try --help)" ;;
  esac
done

case "${NET_MODE}" in online|offline|hybrid) ;; *) die "--mode must be online, offline or hybrid" ;; esac
case "${QDRANT_MODE}" in auto|server|embedded) ;; *) die "--qdrant must be auto, server or embedded" ;; esac

# ---------------------------------------------------------------- platform

OS="$(uname -s 2>/dev/null || echo unknown)"
IS_WSL=no
grep -qi microsoft /proc/version 2>/dev/null && IS_WSL=yes

case "${OS}" in
  Linux)  PLATFORM="linux" ;;
  Darwin) PLATFORM="macos" ;;
  MINGW*|MSYS*|CYGWIN*)
    die "this is a Windows shell. Use the PowerShell installer instead:
    powershell -ExecutionPolicy Bypass -File scripts\\install.ps1" ;;
  *) PLATFORM="unknown" ;;
esac
[ "${IS_WSL}" = yes ] && PLATFORM="wsl"

printf '%s\n' "${B}MCP fetch server installer${Z}"
info "platform: ${PLATFORM}"

# ---------------------------------------------------------------- 1. project

step "Locating the project"

SCRIPT_PATH="${BASH_SOURCE[0]}"
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" 2>/dev/null && pwd || echo "")"
PROJECT_DIR=""

if [ -n "${INSTALL_DIR}" ]; then
  PROJECT_DIR="${INSTALL_DIR}"
elif [ -n "${SCRIPT_DIR}" ] && [ -f "${SCRIPT_DIR}/../pyproject.toml" ]; then
  # Running from inside a checkout.
  PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
elif [ -f "./pyproject.toml" ] && grep -q 'name = "mcp-fetch-server"' ./pyproject.toml 2>/dev/null; then
  PROJECT_DIR="$(pwd)"
else
  PROJECT_DIR="$(pwd)/mcp-fetch-server"
fi

if [ -f "${PROJECT_DIR}/pyproject.toml" ]; then
  info "using existing checkout: ${PROJECT_DIR}"
else
  have git || die "git is required to fetch the project. Install git and re-run."
  info "cloning ${REPO_URL} (${REPO_REF})"
  info "  into ${PROJECT_DIR}"
  git clone --depth 1 --branch "${REPO_REF}" "${REPO_URL}" "${PROJECT_DIR}" \
    || die "clone failed. Check the URL, the ref, and your network."
fi

cd "${PROJECT_DIR}"
PROJECT_DIR="$(pwd)"

# ---------------------------------------------------------------- 2. uv

step "Checking for uv"

if have uv; then
  info "found: $(uv --version)"
else
  # Installing uv means downloading and running a remote script, so it is not
  # done silently. --yes opts in; --no-uv-install refuses outright.
  case "${ALLOW_UV_INSTALL}" in
    no) die "uv is not installed and --no-uv-install was given.
Install it yourself:  curl -fsSL ${UV_INSTALLER} | sh" ;;
  esac
  warn "uv is not installed."
  info "The official installer at ${UV_INSTALLER} would be downloaded and run."
  if confirm "Install uv now?"; then
    have curl || die "curl is required to install uv."
    curl -fsSL "${UV_INSTALLER}" | sh || die "uv installation failed."
    # The installer drops uv in one of these without touching this shell's PATH.
    for candidate in "${HOME}/.local/bin" "${HOME}/.cargo/bin"; do
      [ -x "${candidate}/uv" ] && PATH="${candidate}:${PATH}"
    done
    export PATH
    have uv || die "uv was installed but is not on PATH. Open a new shell and re-run."
    info "installed: $(uv --version)"
  else
    die "uv is required. Install it and re-run:  curl -fsSL ${UV_INSTALLER} | sh"
  fi
fi

# ---------------------------------------------------------------- 3. deps

step "Installing dependencies"

EXTRAS=(--extra rag)
[ "${WITH_RERANK}" = yes ] && EXTRAS+=(--extra rerank)
info "extras: rag$([ "${WITH_RERANK}" = yes ] && printf ', rerank')"

# An installer gets run on whatever network the machine happens to have, and
# uv's 30s default is not enough for the larger wheels (numpy, onnxruntime) on
# a slow link. Retrying is worthwhile because completed downloads are cached,
# so each attempt starts further along.
export UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT:-180}"

SYNC_OK=no
for attempt in 1 2 3; do
  if uv sync "${EXTRAS[@]}"; then
    SYNC_OK=yes
    break
  fi
  if [ "${attempt}" -lt 3 ]; then
    warn "dependency download failed (attempt ${attempt}/3), retrying..."
    sleep 5
  fi
done
[ "${SYNC_OK}" = yes ] || die "dependency installation failed after 3 attempts.
If the network is slow, raise the timeout and try again:
    UV_HTTP_TIMEOUT=600 uv sync --extra rag"

# Everything below runs through the project's own environment.
RUN=(uv run --no-sync)

# ---------------------------------------------------------------- 4. config

step "Writing configuration"

if [ -f .env ]; then
  info ".env already exists, leaving it untouched"
else
  [ -f .env.example ] || die ".env.example is missing from the checkout."
  cp .env.example .env

  # A real random token: the example ships an obvious placeholder, and an HTTP
  # deployment that keeps it is unauthenticated in practice.
  TOKEN=""
  if have openssl; then
    TOKEN="$(openssl rand -hex 32)"
  else
    TOKEN="$("${RUN[@]}" python -c 'import secrets;print(secrets.token_urlsafe(32))' 2>/dev/null || true)"
  fi
  if [ -n "${TOKEN}" ]; then
    # Portable in-place edit: GNU and BSD sed disagree about -i.
    tmp="$(mktemp)"
    sed "s|^MCP_AUTH_TOKEN=.*|MCP_AUTH_TOKEN=${TOKEN}|" .env > "${tmp}" && mv "${tmp}" .env
    info "generated a random MCP_AUTH_TOKEN"
  else
    warn "could not generate a token; set MCP_AUTH_TOKEN in .env before using HTTP mode"
  fi
  info "wrote .env"
fi

set_env() {
  # set_env KEY VALUE -- replace the line if present, append it otherwise.
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  if grep -q "^${key}=" .env 2>/dev/null; then
    sed "s|^${key}=.*|${key}=${value}|" .env > "${tmp}" && mv "${tmp}" .env
  else
    rm -f "${tmp}"
    printf '%s=%s\n' "${key}" "${value}" >> .env
  fi
}

set_env FETCH_NET_MODE "${NET_MODE}"
info "corpus mode: ${NET_MODE}"

# ---------------------------------------------------------------- 5. model

step "Checking the local model"

OLLAMA_URL="$("${RUN[@]}" python -c 'from mcp_fetch_server.config import settings;print(settings.llm_base_url)' 2>/dev/null || echo "http://localhost:11434")"

ollama_up() { curl -fsS -m 3 "${OLLAMA_URL}/api/version" >/dev/null 2>&1; }

if ollama_up; then
  info "Ollama is reachable at ${OLLAMA_URL}"
elif have ollama; then
  warn "Ollama is installed but not responding; starting it in the background"
  (ollama serve >/dev/null 2>&1 &) || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do ollama_up && break; sleep 2; done
  ollama_up && info "Ollama is now reachable" || warn "Ollama still not responding"
else
  warn "Ollama was not found. The corpus needs it for embeddings and enrichment."
  info "Install it from https://ollama.com/download, then re-run this script."
fi

if ollama_up && [ "${PULL_MODELS}" = yes ]; then
  for model in "${MODELS_EMBED}" "${MODELS_CHAT}"; do
    if "${RUN[@]}" python - "${model}" <<'PY' 2>/dev/null
import sys, urllib.request, json
from mcp_fetch_server.config import settings
want = sys.argv[1].split(":")[0]
with urllib.request.urlopen(f"{settings.llm_base_url}/api/tags", timeout=5) as fh:
    have = [m.get("model", "") for m in json.load(fh).get("models", [])]
sys.exit(0 if any(h.split(":")[0] == want for h in have) else 1)
PY
    then
      info "${model} is already present"
    else
      info "pulling ${model} (this is a multi-gigabyte download)"
      if have ollama; then
        ollama pull "${model}" || warn "could not pull ${model}; pull it yourself later"
      else
        warn "${model} is missing and the ollama CLI is unavailable here."
        info "  pull it on the machine running Ollama:  ollama pull ${model}"
      fi
    fi
  done
elif [ "${PULL_MODELS}" = no ]; then
  info "skipping model downloads (--no-models)"
fi

# ---------------------------------------------------------------- 6. qdrant

step "Setting up the vector store"

DOCKER=""
for candidate in docker podman; do have "${candidate}" && { DOCKER="${candidate}"; break; }; done
docker_up() { [ -n "${DOCKER}" ] && "${DOCKER}" info >/dev/null 2>&1; }

RESOLVED_QDRANT="${QDRANT_MODE}"
if [ "${RESOLVED_QDRANT}" = auto ]; then
  if docker_up; then
    RESOLVED_QDRANT="server"
  else
    RESOLVED_QDRANT="embedded"
    info "no usable container runtime, so choosing embedded mode"
  fi
fi

if [ "${RESOLVED_QDRANT}" = embedded ]; then
  # An empty URL makes the client run Qdrant on local disk.
  set_env FETCH_QDRANT_URL ""
  info "Qdrant will run embedded on local disk (no Docker required)"
else
  docker_up || die "--qdrant server was requested but no container runtime is running.
Start Docker, or re-run with --qdrant embedded."
  info "starting Qdrant with ${DOCKER} compose"
  if "${DOCKER}" compose up -d qdrant >/dev/null 2>&1; then
    :
  elif have docker-compose && docker-compose up -d qdrant >/dev/null 2>&1; then
    :
  else
    die "could not start Qdrant. Try: ${DOCKER} compose up -d qdrant"
  fi
  set_env FETCH_QDRANT_URL "http://localhost:6333"
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    curl -fsS -m 3 http://localhost:6333/ >/dev/null 2>&1 && break
    sleep 2
  done
  curl -fsS -m 3 http://localhost:6333/ >/dev/null 2>&1 \
    && info "Qdrant is up at http://localhost:6333" \
    || warn "Qdrant did not answer yet; it may still be starting"
fi

# ---------------------------------------------------------------- 7. verify

step "Verifying the installation"

if "${RUN[@]}" mcp-fetch-server doctor; then
  info "all checks passed"
else
  warn "some checks failed; see above. The install is still usable for whatever passed."
fi

# ---------------------------------------------------------------- 8. corpus

if [ -n "${CORPUS_PATH}" ]; then
  step "Ingesting ${CORPUS_PATH}"
  if [ -d "${CORPUS_PATH}" ]; then
    "${RUN[@]}" mcp-fetch-server ingest "${CORPUS_PATH}" --quiet || warn "ingestion reported errors"
    "${RUN[@]}" mcp-fetch-server embed --quiet || warn "embedding reported errors"
  else
    warn "no such folder: ${CORPUS_PATH}"
  fi
fi

# ---------------------------------------------------------------- 9. watcher

if [ "${INSTALL_WATCHER}" = yes ]; then
  step "Installing the folder watcher"
  if [ -d /run/systemd/system ]; then
    bash "${PROJECT_DIR}/scripts/install-watch-unit.sh" "${CORPUS_PATH:-${PROJECT_DIR}/corpus}" \
      || warn "could not install the watcher unit"
  else
    warn "systemd is not available here, so the watcher was not installed as a service."
    info "run it in the foreground instead:"
    info "  uv run mcp-fetch-server watch ${CORPUS_PATH:-./corpus}"
  fi
fi

# ---------------------------------------------------------------- done

CORPUS_HINT="${CORPUS_PATH:-./corpus}"
cat <<EOF

${G}Done.${Z} Installed at ${B}${PROJECT_DIR}${Z}

Next steps:

  ${DIM}# add documents and make them searchable${Z}
  uv run mcp-fetch-server ingest ${CORPUS_HINT}
  uv run mcp-fetch-server enrich
  uv run mcp-fetch-server embed
  uv run mcp-fetch-server search "your question" --answer

  ${DIM}# run it for an MCP client such as Cursor${Z}
  uv run mcp-fetch-server serve --transport stdio

Config is in ${B}${PROJECT_DIR}/.env${Z}. Re-run this script any time; it will
not overwrite it.
EOF
