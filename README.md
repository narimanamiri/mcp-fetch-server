# MCP Web Fetch Server

An all-in-one Python MCP server for web research: fetch pages, search the web, batch-fetch, extract links, summarize via client-side sampling, and (optionally) read/write local files — all for LLM agents like Cursor. Supports local **stdio** and **Streamable HTTP** for remote access, and exercises essentially every MCP protocol capability (Tools, Resources, Prompts, Completions, Sampling, Elicitation, Roots, Progress, Logging).

**Documentation:**
- [User Manual](docs/USER_MANUAL.md) — installation, Cursor setup, troubleshooting
- [Project Documentation](docs/PROJECT_DOCUMENTATION.md) — architecture, modules, security, API

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

### Security
- SSRF protection with resolve-then-check and redirect re-validation
- `robots.txt` compliance (override with `ignore_robots_txt=true`)
- Optional domain allowlist
- Local file tools sandboxed to one configured directory, path-traversal safe
- Bearer token auth + rate limiting for HTTP mode
- `/health` endpoint
- Windows `.exe` build (no Python required for end users)

## Quick Start

### Option A: Windows executable (recommended)

```powershell
cd "E:\my python projects\MCP\mcp-fetch-server"
.\dist\mcp-fetch-server.exe --transport stdio
```

Build the exe yourself: `.\scripts\build_exe.ps1` → outputs `dist\mcp-fetch-server.exe`

### Option B: Python + uv (development)

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

## Optional: SearXNG search fallback

`web_search` uses DuckDuckGo by default. Since that's an unofficial HTML scrape, you can
optionally run a local [SearXNG](https://docs.searxng.org/) instance as a fallback that
`web_search` automatically uses if DuckDuckGo's scrape ever fails:

```powershell
docker compose up -d           # starts SearXNG at http://localhost:8080 (JSON API enabled)
```

No further configuration is needed — `FETCH_SEARXNG_URL` already defaults to
`http://localhost:8080` in `.env.example`/`.env`. Set it to an empty string to disable the
fallback, or point it at any other SearXNG instance you run. See `docker-compose.yml` and
`searxng/settings.yml`.

## Remote HTTP Mode

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
├── tests/                      # 77 pytest tests
├── docs/                       # User manual + technical docs
├── scripts/build_exe.ps1       # Build script
├── docker-compose.yml          # Optional local SearXNG instance (web_search fallback)
├── searxng/settings.yml        # SearXNG config (JSON API enabled)
└── .cursor/mcp.json            # Cursor config
```

## Development

```powershell
uv run pytest
uv run ruff check .
.\scripts\build_exe.ps1
```

## License

MIT
