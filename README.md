# MCP Web Fetch Server

An all-in-one Python MCP server for web research: fetch pages, search the web, batch-fetch, extract links, summarize via client-side sampling, and (optionally) read/write local files — all for LLM agents like Cursor. Supports local **stdio** and **Streamable HTTP** for remote access, and exercises essentially every MCP protocol capability (Tools, Resources, Prompts, Completions, Sampling, Elicitation, Roots, Progress, Logging).

**Documentation:**

| Document | What's inside |
|----------|---------------|
| [User Manual](docs/USER_MANUAL.md) | Full end-user guide: install (Docker/Linux/Windows), Cursor setup, all tools, admin GUI, config, troubleshooting |
| [Project Documentation](docs/PROJECT_DOCUMENTATION.md) | Full technical reference: architecture, modules, MCP APIs, admin API, security, Docker stack, testing |

## Features

### Tools
- `fetch_url` — page content as markdown, with chunked reading (`start_index`, `max_length`)
- `fetch_metadata_tool` — HEAD request metadata
- `batch_fetch` — fetch multiple URLs concurrently, with per-URL error isolation and progress
- `web_search` — DuckDuckGo web search (no API key), with automatic fallback to a local SearXNG instance if DuckDuckGo's scrape fails
- `extract_links` — structured link/image extraction from a page
- `summarize_url` — asks the connected client's LLM to summarize a page (MCP sampling)
- `read_file` / `write_file` / `list_dir` — sandboxed local file access (opt-in, disabled by default)

### Other MCP capabilities
- **Resources**: `config://settings`, `history://recent`, `fetch-cache://{encoded_url}`
- **Prompts**: `fetch`, `research_topic`, `summarize_page`, `extract_key_facts`, `compare_sources`
- **Completions**: URL/depth autocomplete for prompt and resource arguments
- **Elicitation**: `write_file` confirms before overwriting an existing file
- **Roots**: local file tools honor client-exposed directories in addition to `FETCH_LOCAL_FILES_ROOT`
- **Progress notifications**: `batch_fetch` and `summarize_url` report progress as they run
- **Management GUI**: web dashboard at `/admin` for status, config, history, cache, and tools

### Security
- SSRF protection with resolve-then-check and redirect re-validation
- `robots.txt` compliance (override with `ignore_robots_txt=true`)
- Optional domain allowlist
- Local file tools sandboxed to one configured directory, path-traversal safe
- Bearer token auth + rate limiting for HTTP mode
- `/health` endpoint
- Windows `.exe` build (no Python required for end users)

## Quick Start

### Option A: Docker — full stack (recommended for server deployment)

Runs the MCP server, admin GUI, and SearXNG together.

**Linux / macOS:**

```bash
cd mcp-fetch-server
cp .env.docker.example .env          # edit MCP_AUTH_TOKEN
chmod +x scripts/docker-up.sh
./scripts/docker-up.sh
# or: docker compose up -d --build
```

**Windows (PowerShell):**

```powershell
cd "E:\my python projects\MCP\mcp-fetch-server"
copy .env.docker.example .env
.\scripts\docker-up.ps1
```

| Service | URL |
|---------|-----|
| MCP protocol | `http://127.0.0.1:8000/mcp` |
| Admin GUI | `http://127.0.0.1:8000/admin` |
| Health | `http://127.0.0.1:8000/health` |
| SearXNG (search fallback) | `http://127.0.0.1:8080` |

Connect Cursor over HTTP (Bearer token required):

```json
{
  "mcpServers": {
    "web-fetch": {
      "url": "http://127.0.0.1:8000/mcp",
      "headers": { "Authorization": "Bearer your-token-from-env" }
    }
  }
}
```

Local files for `read_file`/`write_file` map to the `./workspace` folder on your host.

### Option B: Windows executable (local / Cursor stdio)

```powershell
cd "E:\my python projects\MCP\mcp-fetch-server"
.\dist\mcp-fetch-server.exe --transport stdio
```

Build the exe yourself: `.\scripts\build_exe.ps1` → outputs `dist\mcp-fetch-server.exe`

### Option C: Python + uv (development)

```powershell
cd "E:\my python projects\MCP\mcp-fetch-server"
uv sync --dev
copy .env.example .env
uv run mcp-fetch-server --transport stdio
```

## Connect to Cursor

See the [User Manual — Connect to Cursor](docs/USER_MANUAL.md#4-connect-to-cursor) for step-by-step instructions.

Minimal `.cursor/mcp.json` using the executable:

```json
{
  "mcpServers": {
    "web-fetch": {
      "command": "E:/my python projects/MCP/mcp-fetch-server/dist/mcp-fetch-server.exe",
      "args": ["--transport", "stdio"],
      "env": { "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

## Management GUI

A built-in web dashboard lets you monitor and manage the server without using Cursor:

| Mode | URL |
|------|-----|
| **stdio** (Cursor default) | `http://127.0.0.1:8001/admin` |
| **streamable-http** | `http://127.0.0.1:8000/admin` (same port as MCP) |

The dashboard shows uptime, registered tools, redacted configuration, recent fetch
history, cached page previews, and a button to clear history/cache. It auto-refreshes
every 30 seconds.

If `MCP_AUTH_TOKEN` is set, enter it in the dashboard's auth bar (stored in your
browser session only). Disable the GUI with `FETCH_ADMIN_ENABLED=false`.

## Optional: SearXNG search fallback

`web_search` uses DuckDuckGo by default and falls back to SearXNG when that scrape fails.
When you use **Docker** (`docker compose up`), SearXNG is started automatically and the MCP
container is preconfigured to reach it at `http://searxng:8080`.

For **local** (non-Docker) use, you can still run only SearXNG:

```powershell
docker compose up -d searxng
```

Set `FETCH_SEARXNG_URL=http://localhost:8080` in `.env`. See `searxng/settings.yml`.

## Remote HTTP Mode (without full Docker stack)

```powershell
$env:MCP_AUTH_TOKEN = "your-long-random-token"
.\dist\mcp-fetch-server.exe --transport streamable-http --host 127.0.0.1 --port 8000
```

- MCP endpoint: `http://127.0.0.1:8000/mcp`
- Health check: `http://127.0.0.1:8000/health`

## Project Structure

```
mcp-fetch-server/
├── dist/mcp-fetch-server.exe   # Windows executable
├── src/mcp_fetch_server/       # Source code (tools, resources, prompts, security, ...)
├── tests/                      # 84 pytest tests
├── docs/                       # User manual + technical docs
├── scripts/docker-up.sh        # Linux/macOS stack startup
├── scripts/docker-up.ps1       # Windows stack startup
├── docker-compose.yml          # Full stack: MCP server + SearXNG
├── .env.docker.example         # Environment template for Docker Compose
├── Dockerfile                  # MCP server image
├── workspace/                  # Host folder mounted for local file tools (Docker)
├── searxng/settings.yml        # SearXNG config (JSON API enabled)
├── src/mcp_fetch_server/admin.py  # Management web GUI
└── .cursor/mcp.json            # Cursor config
```

## Development

```bash
uv run pytest
uv run ruff check .
./scripts/docker-up.sh          # Linux / macOS
docker compose down
```

Windows only:

```powershell
.\scripts\build_exe.ps1
.\scripts\docker-up.ps1
```

## License

MIT
