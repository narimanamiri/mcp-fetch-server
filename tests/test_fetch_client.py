"""Tests for HTTP client behavior: retries, HEAD fallback, and error messages."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from mcp_fetch_server.converters import html_to_markdown
from mcp_fetch_server.fetcher import FetchError, fetch_metadata, fetch_url_content


def test_html_to_markdown_handles_empty_document() -> None:
    result = html_to_markdown("<html><head></head><body></body></html>")
    assert "UNTRUSTED WEB CONTENT" in result
    assert "Empty" in result or "No readable" in result


def test_html_to_markdown_fallback_on_malformed_html() -> None:
    result = html_to_markdown("<p>Hello <b>world</b></p>")
    assert "Hello" in result
    assert "world" in result


def test_html_to_markdown_ignores_hidden_modal() -> None:
    # readability scores by text density and doesn't know about CSS
    # visibility, so a dense hidden modal can otherwise outscore the real
    # visible article content (regression: arxiv abstract pages).
    html = """
    <html><body>
        <article>
            <h1>Real Article Title</h1>
            <p>This is the real, visible article body that a reader cares
            about and should be extracted as the main content of the page.</p>
        </article>
        <div hidden="true">
            <p>Citation text citation text citation text citation text
            citation text citation text citation text citation text.</p>
        </div>
        <div style="display: none">
            <p>More hidden boilerplate that should never be shown either
            because it is not visible on the rendered page at all.</p>
        </div>
    </body></html>
    """
    result = html_to_markdown(html)
    assert "Real Article Title" in result
    assert "real, visible article body" in result
    assert "Citation text" not in result
    assert "hidden boilerplate" not in result


@respx.mock
@pytest.mark.asyncio
async def test_fetch_metadata_head_fallback_to_get() -> None:
    respx.head("https://example.com/page").mock(
        return_value=httpx.Response(405, headers={"content-type": "text/html"})
    )
    respx.get("https://example.com/page").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html", "content-length": "1234"},
            text="<html><body>ok</body></html>",
        )
    )
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))

    with patch(
        "mcp_fetch_server.fetcher.validate_url",
        new=AsyncMock(
            return_value=type(
                "ValidatedUrl",
                (),
                {
                    "url": "https://example.com/page",
                    "scheme": "https",
                    "host": "example.com",
                    "path": "/page",
                },
            )()
        ),
    ):
        meta = await fetch_metadata("https://example.com/page")

    assert meta.status_code == 200
    assert meta.content_length == 1234


@respx.mock
@pytest.mark.asyncio
async def test_fetch_http_error_includes_snippet() -> None:
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/missing").mock(
        return_value=httpx.Response(404, text="Page not found on this server")
    )

    with patch(
        "mcp_fetch_server.fetcher.validate_url",
        new=AsyncMock(
            return_value=type(
                "ValidatedUrl",
                (),
                {
                    "url": "https://example.com/missing",
                    "scheme": "https",
                    "host": "example.com",
                    "path": "/missing",
                },
            )()
        ),
    ):
        with pytest.raises(FetchError, match="HTTP 404") as exc_info:
            await fetch_url_content(
                "https://example.com/missing",
                max_length=1000,
                start_index=0,
                raw=False,
                ignore_robots_txt=True,
            )

    assert "Page not found" in str(exc_info.value)


@respx.mock
@pytest.mark.asyncio
async def test_fetch_retries_on_connect_error() -> None:
    route = respx.get("https://example.com/robots.txt")
    route.mock(return_value=httpx.Response(404))

    page_route = respx.get("https://example.com/")
    page_route.side_effect = [
        httpx.ConnectError("connection reset"),
        httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><body>ok</body></html>",
        ),
    ]

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
    ), patch("mcp_fetch_server.fetcher.settings.request_retries", 2), patch(
        "mcp_fetch_server.fetcher.settings.retry_backoff_seconds", 0
    ):
        result = await fetch_url_content(
            "https://example.com/",
            max_length=1000,
            start_index=0,
            raw=False,
            ignore_robots_txt=True,
        )

    assert "ok" in result.content.lower()
    assert page_route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_connect_error_has_readable_message() -> None:
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/").mock(side_effect=httpx.ConnectError(""))

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
    ), patch("mcp_fetch_server.fetcher.settings.request_retries", 1):
        with pytest.raises(FetchError, match="Network error fetching") as exc_info:
            await fetch_url_content(
                "https://example.com/",
                max_length=100,
                start_index=0,
                raw=False,
                ignore_robots_txt=True,
            )

    assert "ConnectError" in str(exc_info.value)
