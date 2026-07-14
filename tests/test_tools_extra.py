"""Tests for the extra tools' business logic (batch_fetch, web_search, extract_links,
local file tool wrappers), independent of any live FastMCP Context."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from mcp.server.fastmcp.exceptions import ToolError

from mcp_fetch_server.search import SEARCH_URL
from mcp_fetch_server.security import robots_cache
from mcp_fetch_server.tools_extra import (
    run_batch_fetch,
    run_extract_links,
    run_list_dir,
    run_read_file,
    run_web_search,
    run_write_file,
)


@pytest.fixture(autouse=True)
def clear_robots_cache() -> None:
    robots_cache._cache.clear()


def _validated_url(url: str, host: str, path: str) -> object:
    return type(
        "ValidatedUrl",
        (),
        {"url": url, "scheme": "https", "host": host, "path": path},
    )()


@respx.mock
@pytest.mark.asyncio
async def test_run_batch_fetch_mixes_success_and_error() -> None:
    respx.get("https://a.example/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://a.example/").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/html"}, text="<html><body>ok-a</body></html>"
        )
    )
    respx.get("https://b.example/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://b.example/").mock(return_value=httpx.Response(404, text="gone"))

    progress_calls: list[tuple[int, int]] = []

    async def _on_progress(done: int, total: int) -> None:
        progress_calls.append((done, total))

    def _fake_validate(url: str) -> object:
        if "a.example" in url:
            return _validated_url("https://a.example/", "a.example", "/")
        return _validated_url("https://b.example/", "b.example", "/")

    with patch(
        "mcp_fetch_server.fetcher.validate_url",
        new=AsyncMock(side_effect=_fake_validate),
    ):
        output = await run_batch_fetch(
            ["https://a.example/", "https://b.example/"],
            max_length=1000,
            ignore_robots_txt=True,
            on_progress=_on_progress,
        )

    assert "[1] https://a.example/" in output
    assert "ok-a" in output
    assert "[2] https://b.example/" in output
    assert "ERROR" in output
    assert len(progress_calls) == 2
    assert progress_calls[-1] == (2, 2)


@pytest.mark.asyncio
async def test_run_batch_fetch_rejects_empty_list() -> None:
    with pytest.raises(ToolError, match="at least one URL"):
        await run_batch_fetch([], max_length=1000, ignore_robots_txt=True)


@pytest.mark.asyncio
async def test_run_batch_fetch_rejects_too_many_urls() -> None:
    with patch("mcp_fetch_server.tools_extra.settings.max_batch_urls", 2):
        with pytest.raises(ToolError, match="Too many URLs"):
            await run_batch_fetch(
                ["https://a.example/", "https://b.example/", "https://c.example/"],
                max_length=1000,
                ignore_robots_txt=True,
            )


@respx.mock
@pytest.mark.asyncio
async def test_run_web_search_wraps_search_error_as_tool_error() -> None:
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(ToolError, match="HTTP 503"):
        await run_web_search("test query")


@respx.mock
@pytest.mark.asyncio
async def test_run_web_search_returns_formatted_results() -> None:
    html = """
    <div class="result web-result">
        <a class="result__a" href="https://example.com/">Example</a>
        <a class="result__snippet">A snippet</a>
    </div>
    """
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, text=html))

    output = await run_web_search("test query", max_results=1)
    assert "Example" in output
    assert "https://example.com/" in output


@respx.mock
@pytest.mark.asyncio
async def test_run_extract_links_returns_formatted_links() -> None:
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text='<html><body><a href="/about">About</a></body></html>',
        )
    )

    with patch(
        "mcp_fetch_server.fetcher.validate_url",
        new=AsyncMock(
            return_value=_validated_url("https://example.com/", "example.com", "/")
        ),
    ):
        output = await run_extract_links("https://example.com/")

    assert "[LINK]" in output
    assert "https://example.com/about" in output


@pytest.mark.asyncio
async def test_run_read_file_wraps_file_access_error(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="not found"):
        await run_read_file("missing.txt", [tmp_path])


@pytest.mark.asyncio
async def test_run_read_file_returns_content(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    content = await run_read_file("a.txt", [tmp_path])
    assert content == "hello"


@pytest.mark.asyncio
async def test_run_write_file_creates_file(tmp_path: Path) -> None:
    written = await run_write_file("nested/a.txt", "content", [tmp_path])
    assert written.read_text(encoding="utf-8") == "content"


@pytest.mark.asyncio
async def test_run_write_file_rejects_path_outside_root(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="outside all allowed"):
        await run_write_file("../outside.txt", "content", [tmp_path])


@pytest.mark.asyncio
async def test_run_list_dir_formats_entries(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()

    output = await run_list_dir("", [tmp_path])
    assert "[FILE] file.txt" in output
    assert "[DIR]  sub" in output
