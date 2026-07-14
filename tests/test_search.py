"""Tests for the DuckDuckGo-backed web_search tool."""

from __future__ import annotations

import httpx
import pytest
import respx

from mcp_fetch_server import search as search_module
from mcp_fetch_server.search import (
    SEARCH_URL,
    SearchError,
    format_results,
    search_web,
    searxng_search,
    web_search,
)

_SAMPLE_HTML = """
<html><body>
<div class="result results_links results_links_deep web-result">
    <a class="result__a" href="https://example.com/one">Example One</a>
    <a class="result__snippet">This is the first snippet.</a>
</div>
<div class="result results_links results_links_deep web-result">
    <a class="result__a"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Ftwo&amp;rut=abc">
       Example Two
    </a>
    <div class="result__snippet">This is the second snippet.</div>
</div>
</body></html>
"""


@respx.mock
@pytest.mark.asyncio
async def test_web_search_parses_results_and_unwraps_redirect() -> None:
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, text=_SAMPLE_HTML))

    results = await web_search("test query", max_results=5)

    assert len(results) == 2
    assert results[0].title == "Example One"
    assert results[0].url == "https://example.com/one"
    assert results[0].snippet == "This is the first snippet."

    # The second result's href is wrapped in DDG's /l/?uddg= redirect and
    # should be unwrapped to the real target URL.
    assert results[1].url == "https://example.com/two"
    assert "Example Two" in results[1].title


@respx.mock
@pytest.mark.asyncio
async def test_web_search_respects_max_results() -> None:
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, text=_SAMPLE_HTML))

    results = await web_search("test query", max_results=1)
    assert len(results) == 1


@pytest.mark.asyncio
async def test_web_search_rejects_empty_query() -> None:
    with pytest.raises(SearchError, match="must not be empty"):
        await web_search("   ")


@respx.mock
@pytest.mark.asyncio
async def test_web_search_raises_on_http_error() -> None:
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(SearchError, match="HTTP 503"):
        await web_search("test query")


@respx.mock
@pytest.mark.asyncio
async def test_web_search_raises_when_no_results_found() -> None:
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, text="<html><body></body></html>"))

    with pytest.raises(SearchError, match="No results found"):
        await web_search("test query")


@respx.mock
@pytest.mark.asyncio
async def test_web_search_raises_on_network_error() -> None:
    respx.post(SEARCH_URL).mock(side_effect=httpx.ConnectError("boom"))

    with pytest.raises(SearchError, match="Search request failed"):
        await web_search("test query")


def test_format_results_empty() -> None:
    assert format_results([]) == "[No results]"


def test_format_results_notes_fallback_backend() -> None:
    from mcp_fetch_server.search import SearchResult

    output = format_results(
        [SearchResult(title="T", url="https://example.com", snippet="s")],
        backend="searxng",
    )
    assert output.startswith("(results via fallback backend: searxng)")


_SEARXNG_URL = "http://localhost:8080/search"

_SEARXNG_JSON = {
    "results": [
        {"title": "SearXNG One", "url": "https://example.org/one", "content": "First result."},
        {"title": "SearXNG Two", "url": "https://example.org/two", "content": "Second result."},
    ]
}


@respx.mock
@pytest.mark.asyncio
async def test_searxng_search_parses_json_results() -> None:
    respx.get(_SEARXNG_URL).mock(return_value=httpx.Response(200, json=_SEARXNG_JSON))

    results = await searxng_search("test query", max_results=5)

    assert len(results) == 2
    assert results[0].title == "SearXNG One"
    assert results[0].url == "https://example.org/one"


@respx.mock
@pytest.mark.asyncio
async def test_searxng_search_raises_on_non_json_response() -> None:
    respx.get(_SEARXNG_URL).mock(return_value=httpx.Response(200, text="<html>not json</html>"))

    with pytest.raises(SearchError, match="did not return JSON"):
        await searxng_search("test query")


@respx.mock
@pytest.mark.asyncio
async def test_searxng_search_raises_when_no_results() -> None:
    respx.get(_SEARXNG_URL).mock(return_value=httpx.Response(200, json={"results": []}))

    with pytest.raises(SearchError, match="no results"):
        await searxng_search("test query")


@pytest.mark.asyncio
async def test_searxng_search_raises_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_module.settings, "searxng_url", "")

    with pytest.raises(SearchError, match="not configured"):
        await searxng_search("test query")


@respx.mock
@pytest.mark.asyncio
async def test_search_web_uses_duckduckgo_when_it_succeeds() -> None:
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, text=_SAMPLE_HTML))

    results, backend = await search_web("test query", max_results=1)

    assert backend == "duckduckgo"
    assert len(results) == 1


@respx.mock
@pytest.mark.asyncio
async def test_search_web_falls_back_to_searxng_when_duckduckgo_fails() -> None:
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(503))
    respx.get(_SEARXNG_URL).mock(return_value=httpx.Response(200, json=_SEARXNG_JSON))

    results, backend = await search_web("test query")

    assert backend == "searxng"
    assert results[0].title == "SearXNG One"


@respx.mock
@pytest.mark.asyncio
async def test_search_web_raises_when_both_backends_fail() -> None:
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(503))
    respx.get(_SEARXNG_URL).mock(return_value=httpx.Response(500))

    with pytest.raises(SearchError, match="Both search backends failed"):
        await search_web("test query")


@respx.mock
@pytest.mark.asyncio
async def test_search_web_propagates_duckduckgo_error_when_searxng_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(search_module.settings, "searxng_url", "")
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(SearchError, match="HTTP 503"):
        await search_web("test query")
