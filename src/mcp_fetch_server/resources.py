"""MCP resource registrations: server config, fetch history, and cached pages."""

from __future__ import annotations

import json
from urllib.parse import unquote

from mcp.server.fastmcp import FastMCP

from mcp_fetch_server.config_snapshot import public_settings
from mcp_fetch_server.history import history


def register_resources(mcp: FastMCP) -> None:
    @mcp.resource(
        "config://settings",
        name="server-settings",
        title="Server configuration",
        description="Current (secret-redacted) server configuration and version.",
        mime_type="application/json",
    )
    def read_settings() -> str:
        return json.dumps(public_settings(), indent=2)

    @mcp.resource(
        "history://recent",
        name="fetch-history",
        title="Recent fetch history",
        description=(
            "The most recent URLs fetched by this server instance, most recent first. "
            "Successfully fetched pages may also be readable via the fetch-cache:// "
            "resource template."
        ),
        mime_type="application/json",
    )
    def read_history() -> str:
        entries = [
            {
                "url": entry.url,
                "status_code": entry.status_code,
                "content_type": entry.content_type,
                "content_length": entry.content_length,
                "fetched_at": entry.fetched_at,
                "error": entry.error,
                "cached": history.get_cached_content(entry.url) is not None,
            }
            for entry in history.recent()
        ]
        return json.dumps(entries, indent=2)

    @mcp.resource(
        "fetch-cache://{encoded_url}",
        name="cached-page",
        title="Cached page content",
        description=(
            "Previously fetched page content, read from the in-memory cache instead of "
            "re-fetching over the network. The URI parameter must be the target URL, "
            "percent-encoded (e.g. fetch-cache://https%3A%2F%2Fexample.com%2F). See "
            "history://recent for which URLs are currently cached."
        ),
        mime_type="text/markdown",
    )
    def read_cached_page(encoded_url: str) -> str:
        url = unquote(encoded_url)
        content = history.get_cached_content(url)
        if content is None:
            return f"[Not cached] No cached content for {url}. Fetch it first with fetch_url."
        return content
