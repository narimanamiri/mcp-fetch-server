"""Shared HTTP request headers."""

from __future__ import annotations

from mcp_fetch_server.config import settings


def request_headers() -> dict[str, str]:
    return {
        "User-Agent": settings.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
