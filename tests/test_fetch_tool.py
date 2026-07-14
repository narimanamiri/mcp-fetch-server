"""Tests for fetch behavior, redirects, robots.txt, and chunking."""

from __future__ import annotations

import ipaddress
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from mcp_fetch_server.converters import chunk_content
from mcp_fetch_server.fetcher import FetchError, fetch_url_content
from mcp_fetch_server.security import SecurityError, robots_cache


@pytest.fixture(autouse=True)
def clear_robots_cache() -> None:
    robots_cache._cache.clear()


def test_chunk_content_paginates() -> None:
    content = "abcdefghijklmnopqrstuvwxyz"
    first = chunk_content(content, start_index=0, max_length=5)
    second = chunk_content(content, start_index=5, max_length=5)

    assert first.startswith("abcde")
    assert "start_index=5" in first
    assert second.startswith("fghij")


@respx.mock
@pytest.mark.asyncio
async def test_fetch_url_returns_markdown() -> None:
    html_page = (
        "<html><head><title>Test</title></head>"
        "<body><h1>Hello</h1><p>World</p></body></html>"
    )
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://example.com/").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=html_page,
        )
    )

    with patch(
        "mcp_fetch_server.fetcher.validate_url",
        new=AsyncMock(
            return_value=type(
                "ValidatedUrl",
                (),
                {
                    "url": "https://example.com/",
                    "scheme": "https",
                    "host": "example.com",
                    "path": "/",
                },
            )()
        ),
    ):
        result = await fetch_url_content(
            "https://example.com/",
            max_length=5000,
            start_index=0,
            raw=False,
            ignore_robots_txt=False,
        )

    assert "UNTRUSTED WEB CONTENT" in result.content
    assert result.status_code == 200


@respx.mock
@pytest.mark.asyncio
async def test_fetch_blocks_disallowed_robots() -> None:
    robots = "User-agent: *\nDisallow: /private"
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text=robots)
    )

    with patch(
        "mcp_fetch_server.fetcher.validate_url",
        new=AsyncMock(
            return_value=type(
                "ValidatedUrl",
                (),
                {
                    "url": "https://example.com/private",
                    "scheme": "https",
                    "host": "example.com",
                    "path": "/private",
                },
            )()
        ),
    ):
        with pytest.raises(SecurityError, match="robots.txt"):
            await fetch_url_content(
                "https://example.com/private",
                max_length=1000,
                start_index=0,
                raw=False,
                ignore_robots_txt=False,
            )


@respx.mock
@pytest.mark.asyncio
async def test_fetch_rejects_redirect_to_internal_ip() -> None:
    respx.get("https://example.com/redirect").mock(
        return_value=httpx.Response(
            302,
            headers={"location": "http://127.0.0.1/secret"},
        )
    )

    with patch(
        "mcp_fetch_server.security.resolve_host",
        new=AsyncMock(return_value=[ipaddress.ip_address("93.184.216.34")]),
    ):
        with pytest.raises(SecurityError, match="Blocked address"):
            await fetch_url_content(
                "https://example.com/redirect",
                max_length=1000,
                start_index=0,
                raw=False,
                ignore_robots_txt=True,
            )


@respx.mock
@pytest.mark.asyncio
async def test_fetch_rejects_oversized_response() -> None:
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(404)
    )

    async def oversized_stream() -> bytes:
        yield b"x" * 200

    route = respx.get("https://example.com/big")
    route.mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            stream=oversized_stream(),
        )
    )

    with patch(
        "mcp_fetch_server.fetcher.validate_url",
        new=AsyncMock(
            return_value=type(
                "ValidatedUrl",
                (),
                {
                    "url": "https://example.com/big",
                    "scheme": "https",
                    "host": "example.com",
                    "path": "/big",
                },
            )()
        ),
    ), patch("mcp_fetch_server.fetcher.settings.max_response_bytes", 50):
        with pytest.raises(FetchError, match="maximum allowed size"):
            await fetch_url_content(
                "https://example.com/big",
                max_length=1000,
                start_index=0,
                raw=True,
                ignore_robots_txt=True,
            )
