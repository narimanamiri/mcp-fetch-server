#!/usr/bin/env bash
#
# Install the corpus watcher as a systemd **user** unit inside WSL.
#
# The unit supervises the *Windows* console script through WSL interop rather
# than running a Linux copy of the project. That is deliberate: the catalog is
# SQLite, the MCP server that reads it runs on Windows, and SQLite accessed
# concurrently by a Windows process and a Linux process over drvfs has no
# shared lock manager. Keeping every writer on the Windows side avoids that
# entirely, and costs nothing, since systemd is only acting as a supervisor.
#
# A user unit is used so no root is required. Lingering is enabled so the
# watcher starts with the WSL distro instead of waiting for a login shell.
#
# Usage (from inside WSL):
#   ./scripts/install-watch-unit.sh [WATCHED_FOLDER]
#
# Re-running is safe: it rewrites the unit, keeps any existing settings file,
# and restarts the service.

set -euo pipefail

UNIT_NAME="mcp-corpus-watch.service"
UNIT_DIR="${HOME}/.config/systemd/user"
ENV_FILE="${HOME}/.config/mcp-corpus-watch.env"
WRAPPER="${HOME}/.config/mcp-corpus-watch-run.sh"

die() { printf 'error: %s\n' "$1" >&2; exit 1; }

# -- sanity ------------------------------------------------------------------

grep -qi microsoft /proc/version 2>/dev/null || die "this script is meant to run inside WSL"

[ -d /run/systemd/system ] || die \
  "systemd is not running in this distro. Add the following to /etc/wsl.conf and run 'wsl --shutdown':

  [boot]
  systemd=true"

# -- locate the project ------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
EXE="${PROJECT_DIR}/.venv/Scripts/mcp-fetch-server.exe"

case "${PROJECT_DIR}" in
  /mnt/*) ;;
  *) die "the project must live on a Windows drive (/mnt/...), found: ${PROJECT_DIR}" ;;
esac

[ -x "${EXE}" ] || [ -f "${EXE}" ] || die \
  "console script not found: ${EXE}
Build it on the Windows side first:  uv sync --extra rag"

WATCH_PATH_DEFAULT="${PROJECT_DIR}/corpus"
WATCH_PATH="${1:-${WATCH_PATH_DEFAULT}}"
mkdir -p "${WATCH_PATH}"

# -- settings ----------------------------------------------------------------

if [ -f "${ENV_FILE}" ]; then
  printf 'keeping existing settings: %s\n' "${ENV_FILE}"
else
  mkdir -p "$(dirname "${ENV_FILE}")"
  cat > "${ENV_FILE}" <<EOF
# Settings for ${UNIT_NAME}. Edit, then:
#   systemctl --user restart ${UNIT_NAME}

# Folder to watch. Use a Linux path under /mnt/<drive>/...
WATCH_PATH=${WATCH_PATH}

# Seconds between scans.
WATCH_INTERVAL=60

# Extra flags. Add --prune to remove documents when their source file is
# deleted, or --no-embed to skip indexing.
WATCH_FLAGS=
EOF
  printf 'wrote settings: %s\n' "${ENV_FILE}"
fi

# -- wrapper -----------------------------------------------------------------

# WSL interop translates a process's working directory into its Windows
# equivalent, but it does NOT translate command-line arguments. Handing the
# Windows executable "/mnt/e/..." makes it look for a literal "\mnt\e\..."
# directory, which does not exist -- and the watcher then scans nothing while
# reporting healthy empty passes. So the path is converted here, at start,
# which also means the settings file can keep a natural Linux path.
cat > "${WRAPPER}" <<'WRAPPER_EOF'
#!/usr/bin/env bash
set -euo pipefail

: "${WATCH_PATH:?WATCH_PATH is not set}"
: "${WATCH_EXE:?WATCH_EXE is not set}"
WATCH_INTERVAL="${WATCH_INTERVAL:-60}"
WATCH_FLAGS="${WATCH_FLAGS:-}"

if [ ! -d "${WATCH_PATH}" ]; then
  printf 'watched folder does not exist: %s\n' "${WATCH_PATH}" >&2
  exit 1
fi

# The executable is a Windows binary, so it needs a Windows path.
WATCH_PATH_WIN="$(wslpath -w "${WATCH_PATH}")"

# shellcheck disable=SC2086  # WATCH_FLAGS is meant to word-split into flags
exec "${WATCH_EXE}" watch "${WATCH_PATH_WIN}" --interval "${WATCH_INTERVAL}" ${WATCH_FLAGS}
WRAPPER_EOF
chmod +x "${WRAPPER}"
printf 'wrote wrapper: %s\n' "${WRAPPER}"

# -- unit --------------------------------------------------------------------

mkdir -p "${UNIT_DIR}"
cat > "${UNIT_DIR}/${UNIT_NAME}" <<EOF
[Unit]
Description=MCP fetch server corpus watcher
Documentation=https://github.com/narimanamiri/mcp-fetch-server
After=default.target
# Give up rather than hot-looping on a broken configuration.
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
EnvironmentFile=${ENV_FILE}
# Quoted: systemd splits Environment= on whitespace, and these paths contain
# spaces.
Environment="WATCH_EXE=${EXE}"
WorkingDirectory=${PROJECT_DIR}
ExecStart=${WRAPPER}
Restart=always
RestartSec=15
SyslogIdentifier=mcp-corpus-watch

[Install]
WantedBy=default.target
EOF
printf 'wrote unit: %s\n' "${UNIT_DIR}/${UNIT_NAME}"

# -- enable ------------------------------------------------------------------

# Without lingering the unit would only run while a login shell is open, and
# the WSL distro shuts down when idle.
if [ "$(loginctl show-user "${USER}" -p Linger --value 2>/dev/null || echo no)" != "yes" ]; then
  loginctl enable-linger "${USER}" 2>/dev/null \
    || printf 'note: could not enable lingering; run: sudo loginctl enable-linger %s\n' "${USER}"
fi

systemctl --user daemon-reload
systemctl --user enable "${UNIT_NAME}" >/dev/null
systemctl --user restart "${UNIT_NAME}"

sleep 3
printf '\n'
systemctl --user --no-pager --lines=0 status "${UNIT_NAME}" || true
printf '\nwatching: %s\n' "${WATCH_PATH}"
printf 'logs:     journalctl --user -u %s -f\n' "${UNIT_NAME}"
printf 'stop:     systemctl --user stop %s\n' "${UNIT_NAME}"
