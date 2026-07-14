"""Management web GUI for the MCP fetch server.

Served at ``/admin`` when running in streamable-http mode, or on a separate
background port (default ``127.0.0.1:8001``) when using stdio with Cursor.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from mcp_fetch_server import __version__
from mcp_fetch_server.config import settings
from mcp_fetch_server.config_snapshot import public_settings
from mcp_fetch_server.history import history

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MCP Fetch Server — Admin</title>
  <style>
    :root {
      --bg: #0f1419;
      --surface: #1a2332;
      --surface2: #243044;
      --border: #2d3a4f;
      --text: #e7ecf3;
      --muted: #8b9cb3;
      --accent: #3b82f6;
      --accent-hover: #2563eb;
      --ok: #22c55e;
      --warn: #f59e0b;
      --err: #ef4444;
      --radius: 10px;
      --font: "Segoe UI", system-ui, -apple-system, sans-serif;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: var(--font);
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      min-height: 100vh;
    }
    header {
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 1rem 1.5rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      flex-wrap: wrap;
    }
    header h1 { font-size: 1.15rem; font-weight: 600; }
    header .meta { color: var(--muted); font-size: 0.85rem; }
    .badge {
      display: inline-block;
      padding: 0.2rem 0.55rem;
      border-radius: 999px;
      font-size: 0.75rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .badge-ok { background: rgba(34,197,94,0.15); color: var(--ok); }
    .badge-warn { background: rgba(245,158,11,0.15); color: var(--warn); }
    main { max-width: 1200px; margin: 0 auto; padding: 1.5rem; }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 1rem;
      margin-bottom: 1.5rem;
    }
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 1rem 1.1rem;
    }
    .card .label { color: var(--muted); font-size: 0.8rem; margin-bottom: 0.25rem; }
    .card .value { font-size: 1.5rem; font-weight: 700; }
    section {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      margin-bottom: 1.25rem;
      overflow: hidden;
    }
    section h2 {
      font-size: 0.95rem;
      padding: 0.85rem 1.1rem;
      border-bottom: 1px solid var(--border);
      background: var(--surface2);
    }
    section .body { padding: 1rem 1.1rem; }
    table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
    th, td {
      text-align: left;
      padding: 0.55rem 0.65rem;
      border-bottom: 1px solid var(--border);
      vertical-align: top;
    }
    th { color: var(--muted); font-weight: 600; font-size: 0.75rem; text-transform: uppercase; }
    tr:last-child td { border-bottom: none; }
    a { color: var(--accent); text-decoration: none; word-break: break-all; }
    a:hover { text-decoration: underline; }
    .toolbar {
      display: flex;
      gap: 0.5rem;
      flex-wrap: wrap;
      margin-bottom: 1rem;
      align-items: center;
    }
    button, .btn {
      background: var(--accent);
      color: #fff;
      border: none;
      border-radius: 6px;
      padding: 0.45rem 0.85rem;
      font-size: 0.85rem;
      cursor: pointer;
      font-family: inherit;
    }
    button:hover { background: var(--accent-hover); }
    button.secondary {
      background: var(--surface2);
      border: 1px solid var(--border);
      color: var(--text);
    }
    button.danger { background: var(--err); }
    pre.config {
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 0.85rem;
      font-size: 0.78rem;
      overflow-x: auto;
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 320px;
      overflow-y: auto;
    }
    .preview {
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 0.85rem;
      font-size: 0.8rem;
      max-height: 280px;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: Consolas, "Courier New", monospace;
    }
    .empty { color: var(--muted); font-size: 0.9rem; padding: 0.5rem 0; }
    .status-ok { color: var(--ok); }
    .status-err { color: var(--err); }
    .auth-bar {
      display: flex;
      gap: 0.5rem;
      align-items: center;
      flex-wrap: wrap;
    }
    .auth-bar input {
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 6px;
      color: var(--text);
      padding: 0.4rem 0.65rem;
      font-size: 0.85rem;
      min-width: 220px;
    }
    #toast {
      position: fixed;
      bottom: 1.25rem;
      right: 1.25rem;
      background: var(--surface2);
      border: 1px solid var(--border);
      padding: 0.65rem 1rem;
      border-radius: 8px;
      font-size: 0.85rem;
      display: none;
      z-index: 100;
    }
    .pill { font-size: 0.72rem; padding: 0.15rem 0.45rem; border-radius: 4px; }
    .pill-cached { background: rgba(59,130,246,0.2); color: var(--accent); }
    .pill-error { background: rgba(239,68,68,0.15); color: var(--err); }
    @media (max-width: 640px) {
      main { padding: 1rem; }
      table { font-size: 0.78rem; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>MCP Fetch Server</h1>
      <div class="meta">Management dashboard</div>
    </div>
    <div style="display:flex;align-items:center;gap:0.75rem;flex-wrap:wrap;">
      <span id="status-badge" class="badge badge-ok">Online</span>
      <span id="version" class="meta"></span>
    </div>
  </header>
  <main>
    <div class="toolbar auth-bar" id="auth-bar" style="display:none;">
      <label for="token">Bearer token:</label>
      <input id="token" type="password" placeholder="MCP_AUTH_TOKEN value">
      <button type="button" onclick="saveToken()">Save</button>
    </div>
    <div class="grid" id="stats"></div>

    <section>
      <h2>Registered tools</h2>
      <div class="body" id="tools"><div class="empty">Loading…</div></div>
    </section>

    <section>
      <h2>Configuration</h2>
      <div class="body"><pre class="config" id="config">Loading…</pre></div>
    </section>

    <section>
      <h2>Recent fetches</h2>
      <div class="body">
        <div class="toolbar">
          <button type="button" class="secondary" onclick="refreshAll()">Refresh</button>
          <button type="button" class="danger" onclick="clearHistory()">Clear history &amp; cache</button>
        </div>
        <div id="history"><div class="empty">Loading…</div></div>
      </div>
    </section>

    <section>
      <h2>Cache preview</h2>
      <div class="body">
        <div id="cache-list" class="empty">Select a cached URL from history.</div>
        <div class="preview" id="cache-preview" style="display:none;margin-top:0.75rem;"></div>
      </div>
    </section>
  </main>
  <div id="toast"></div>
  <script>
    const API = "/admin/api";
    function getToken() { return sessionStorage.getItem("admin_token") || ""; }
    function saveToken() {
      sessionStorage.setItem("admin_token", document.getElementById("token").value.trim());
      toast("Token saved");
      refreshAll();
    }
    function headers() {
      const h = { "Accept": "application/json" };
      const t = getToken();
      if (t) h["Authorization"] = "Bearer " + t;
      return h;
    }
    async function api(path, opts = {}) {
      const res = await fetch(API + path, { ...opts, headers: { ...headers(), ...(opts.headers || {}) } });
      if (res.status === 401) throw new Error("Unauthorized — set MCP_AUTH_TOKEN in the auth bar.");
      if (!res.ok) {
        const body = await res.text();
        throw new Error(body || res.statusText);
      }
      return res.json();
    }
    function toast(msg) {
      const el = document.getElementById("toast");
      el.textContent = msg;
      el.style.display = "block";
      setTimeout(() => { el.style.display = "none"; }, 2800);
    }
    function fmtTime(ts) {
      if (!ts) return "—";
      return new Date(ts * 1000).toLocaleString();
    }
    function fmtBytes(n) {
      if (n < 1024) return n + " B";
      if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
      return (n / 1048576).toFixed(2) + " MB";
    }
    function fmtUptime(sec) {
      if (sec < 60) return Math.floor(sec) + "s";
      if (sec < 3600) return Math.floor(sec / 60) + "m " + Math.floor(sec % 60) + "s";
      const h = Math.floor(sec / 3600);
      const m = Math.floor((sec % 3600) / 60);
      return h + "h " + m + "m";
    }
    async function loadStatus() {
      const s = await api("/status");
      document.getElementById("version").textContent = "v" + s.version;
      document.getElementById("auth-bar").style.display = s.auth_required ? "flex" : "none";
      if (s.auth_required && getToken()) document.getElementById("token").value = getToken();
      document.getElementById("stats").innerHTML = [
        ["Uptime", fmtUptime(s.uptime_seconds)],
        ["History entries", s.history_count],
        ["Cached pages", s.cache_count],
        ["Cache size", fmtBytes(s.cache_bytes)],
        ["Transport", s.transport],
      ].map(([label, value]) =>
        `<div class="card"><div class="label">${label}</div><div class="value">${value}</div></div>`
      ).join("");
    }
    async function loadConfig() {
      const c = await api("/config");
      document.getElementById("config").textContent = JSON.stringify(c, null, 2);
    }
    async function loadTools() {
      const tools = await api("/tools");
      if (!tools.length) {
        document.getElementById("tools").innerHTML = '<div class="empty">No tools registered.</div>';
        return;
      }
      document.getElementById("tools").innerHTML =
        "<table><thead><tr><th>Name</th><th>Description</th></tr></thead><tbody>" +
        tools.map(t =>
          `<tr><td><strong>${escapeHtml(t.name)}</strong></td>` +
          `<td>${escapeHtml(t.description || "")}</td></tr>`
        ).join("") + "</tbody></table>";
    }
    async function loadHistory() {
      const entries = await api("/history");
      if (!entries.length) {
        document.getElementById("history").innerHTML = '<div class="empty">No fetches yet.</div>';
        return;
      }
      document.getElementById("history").innerHTML =
        "<table><thead><tr><th>URL</th><th>Status</th><th>Time</th><th>Notes</th></tr></thead><tbody>" +
        entries.map(e => {
          const status = e.error
            ? `<span class="status-err">${escapeHtml(e.error)}</span>`
            : `<span class="status-ok">${e.status_code}</span>`;
          const notes = [];
          if (e.cached) notes.push('<span class="pill pill-cached">cached</span>');
          if (e.error) notes.push('<span class="pill pill-error">error</span>');
          const cacheLink = e.cached
            ? ` <a href="#" onclick="previewCache('${encodeURIComponent(e.url)}');return false;">view</a>`
            : "";
          return `<tr>
            <td><a href="${escapeHtml(e.url)}" target="_blank" rel="noopener">${escapeHtml(e.url)}</a>${cacheLink}</td>
            <td>${status}</td>
            <td>${fmtTime(e.fetched_at)}</td>
            <td>${notes.join(" ") || "—"}</td>
          </tr>`;
        }).join("") + "</tbody></table>";
    }
    async function previewCache(encodedUrl) {
      const url = decodeURIComponent(encodedUrl);
      const data = await api("/cache/content?url=" + encodeURIComponent(url));
      document.getElementById("cache-preview").style.display = "block";
      document.getElementById("cache-preview").textContent = data.content;
      document.getElementById("cache-list").textContent = "Showing: " + data.url;
    }
    async function clearHistory() {
      if (!confirm("Clear all fetch history and cached content?")) return;
      await api("/history/clear", { method: "POST" });
      toast("History and cache cleared");
      await refreshAll();
    }
    function escapeHtml(s) {
      return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }
    async function refreshAll() {
      try {
        await Promise.all([loadStatus(), loadConfig(), loadTools(), loadHistory()]);
        document.getElementById("status-badge").className = "badge badge-ok";
        document.getElementById("status-badge").textContent = "Online";
      } catch (err) {
        document.getElementById("status-badge").className = "badge badge-warn";
        document.getElementById("status-badge").textContent = "Error";
        toast(err.message);
      }
    }
    refreshAll();
    setInterval(refreshAll, 30000);
  </script>
</body>
</html>"""


class AdminPanel:
    """HTTP handlers for the management dashboard and JSON API."""

    def __init__(
        self,
        *,
        mcp: FastMCP | None = None,
        transport: str = "stdio",
        started_at: float | None = None,
    ) -> None:
        self._mcp = mcp
        self._transport = transport
        self._started_at = started_at or time.time()

    def _authorized(self, request: Request) -> bool:
        if not settings.mcp_auth_token:
            return True
        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            return False
        return auth_header[7:].strip() == settings.mcp_auth_token

    def _unauthorized(self) -> JSONResponse:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    async def dashboard(self, request: Request) -> Response:
        return HTMLResponse(_DASHBOARD_HTML)

    async def api_status(self, request: Request) -> Response:
        if not self._authorized(request):
            return self._unauthorized()
        return JSONResponse(await self._status_payload())

    async def api_config(self, request: Request) -> Response:
        if not self._authorized(request):
            return self._unauthorized()
        return JSONResponse(public_settings())

    async def api_tools(self, request: Request) -> Response:
        if not self._authorized(request):
            return self._unauthorized()
        tools = await self._list_tools()
        return JSONResponse(tools)

    async def api_history(self, request: Request) -> Response:
        if not self._authorized(request):
            return self._unauthorized()
        return JSONResponse(self._history_payload())

    async def api_cache(self, request: Request) -> Response:
        if not self._authorized(request):
            return self._unauthorized()
        return JSONResponse(
            [{"url": url, "encoded": quote(url, safe="")} for url in history.cached_urls()]
        )

    async def api_cache_item(self, request: Request) -> Response:
        if not self._authorized(request):
            return self._unauthorized()
        url = request.query_params.get("url", "").strip()
        if not url:
            return JSONResponse({"error": "missing_url"}, status_code=400)
        content = history.get_cached_content(url)
        if content is None:
            return JSONResponse({"error": "not_cached", "url": url}, status_code=404)
        return JSONResponse({"url": url, "content": content})

    async def api_clear_history(self, request: Request) -> Response:
        if not self._authorized(request):
            return self._unauthorized()
        history.clear()
        return JSONResponse({"ok": True})

    async def root_redirect(self, request: Request) -> Response:
        return RedirectResponse("/admin", status_code=302)

    async def _status_payload(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "uptime_seconds": time.time() - self._started_at,
            "started_at": datetime.fromtimestamp(self._started_at, tz=UTC).isoformat(),
            "transport": self._transport,
            "history_count": history.entry_count,
            "cache_count": history.cache_count,
            "cache_bytes": history.cache_bytes_used,
            "auth_required": bool(settings.mcp_auth_token),
            "admin_host": settings.admin_host,
            "admin_port": settings.admin_port,
        }

    def _history_payload(self) -> list[dict[str, Any]]:
        return [
            {
                "url": entry.url,
                "status_code": entry.status_code,
                "content_type": entry.content_type,
                "content_length": entry.content_length,
                "fetched_at": entry.fetched_at,
                "error": entry.error,
                "cached": history.get_cached_content(entry.url) is not None,
            }
            for entry in history.recent(limit=settings.max_history_entries)
        ]

    async def _list_tools(self) -> list[dict[str, str | None]]:
        if self._mcp is None:
            return []
        tools = await self._mcp.list_tools()
        return [
            {"name": tool.name, "description": tool.description}
            for tool in tools
        ]

    def create_app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/", self.root_redirect, methods=["GET"]),
                Route("/admin", self.dashboard, methods=["GET"]),
                Route("/admin/api/status", self.api_status, methods=["GET"]),
                Route("/admin/api/config", self.api_config, methods=["GET"]),
                Route("/admin/api/tools", self.api_tools, methods=["GET"]),
                Route("/admin/api/history", self.api_history, methods=["GET"]),
                Route("/admin/api/cache", self.api_cache, methods=["GET"]),
                Route("/admin/api/cache/content", self.api_cache_item, methods=["GET"]),
                Route("/admin/api/history/clear", self.api_clear_history, methods=["POST"]),
            ]
        )

    def register_routes(self, mcp: FastMCP) -> None:
        """Attach admin routes to a FastMCP instance (streamable-http mode)."""
        self._mcp = mcp

        @mcp.custom_route("/admin", methods=["GET"])
        async def _dashboard(request: Request) -> Response:
            return await self.dashboard(request)

        @mcp.custom_route("/admin/api/status", methods=["GET"])
        async def _status(request: Request) -> Response:
            return await self.api_status(request)

        @mcp.custom_route("/admin/api/config", methods=["GET"])
        async def _config(request: Request) -> Response:
            return await self.api_config(request)

        @mcp.custom_route("/admin/api/tools", methods=["GET"])
        async def _tools(request: Request) -> Response:
            return await self.api_tools(request)

        @mcp.custom_route("/admin/api/history", methods=["GET"])
        async def _history(request: Request) -> Response:
            return await self.api_history(request)

        @mcp.custom_route("/admin/api/cache", methods=["GET"])
        async def _cache(request: Request) -> Response:
            return await self.api_cache(request)

        @mcp.custom_route("/admin/api/cache/content", methods=["GET"])
        async def _cache_item(request: Request) -> Response:
            return await self.api_cache_item(request)

        @mcp.custom_route("/admin/api/history/clear", methods=["POST"])
        async def _clear(request: Request) -> Response:
            return await self.api_clear_history(request)


def start_admin_background(
    *,
    mcp: FastMCP | None = None,
    transport: str = "stdio",
) -> None:
    """Run the admin GUI on a background thread (stdio mode)."""
    import threading

    import anyio
    import uvicorn

    panel = AdminPanel(mcp=mcp, transport=transport)
    config = uvicorn.Config(
        panel.create_app(),
        host=settings.admin_host,
        port=settings.admin_port,
        log_level="warning",
    )

    def _serve() -> None:
        anyio.run(uvicorn.Server(config).serve)

    thread = threading.Thread(target=_serve, name="admin-gui", daemon=True)
    thread.start()
