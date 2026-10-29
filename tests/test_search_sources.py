"""Tests for explicit search-source selection.

The behaviour under test exists because of a sharp edge: in hybrid mode the
corpus was tried first and the live web only reached when the corpus returned
nothing, so once an archive held anything relevant the web was unreachable.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from mcp_fetch_server import search as search_module
from mcp_fetch_server.config import settings
from mcp_fetch_server.search import (
    SearchError,
    SearchResult,
    combined_search,
    format_results,
    online_search,
    search_web,
)
from mcp_fetch_server.tools_extra import run_web_search

DDG = "https://html.duckduckgo.com/html/"
SEARX = "http://localhost:8080/search"


def ddg_page(title: str = "Live Result") -> str:
    return (
        '<div class="result">'
        f'<a class="result__a" href="https://example.com/live">{title}</a>'
        '<a class="result__snippet">A snippet from the live web.</a>'
        "</div>"
    )


def corpus_rows() -> list[tuple[str, str, str]]:
    return [("Archived Doc", "https://local.archive/doc/a-1", "A local passage.")]


@pytest.fixture
def hybrid(monkeypatch):
    monkeypatch.setattr(settings, "net_mode", "hybrid")
    monkeypatch.setattr(settings, "searxng_url", "")
    return settings


@pytest.fixture
def online_only(monkeypatch):
    monkeypatch.setattr(settings, "net_mode", "online")
    monkeypatch.setattr(settings, "searxng_url", "")
    return settings


def fake_corpus(monkeypatch, rows=None, error: str | None = None):
    """Patch the corpus backend without needing an index or a model."""

    async def _search(query, max_results=None):
        if error is not None:
            raise SearchError(error)
        return [
            SearchResult(title=title, url=url, snippet=snippet)
            for title, url, snippet in (rows or corpus_rows())
        ]

    monkeypatch.setattr(search_module, "corpus_search", _search)


# ------------------------------------------------------------ source=web


@respx.mock
async def test_source_web_skips_a_corpus_that_would_have_answered(hybrid, monkeypatch):
    """The regression this exists for: a corpus with a relevant document used
    to make the live web unreachable."""
    fake_corpus(monkeypatch)
    respx.post(DDG).mock(return_value=httpx.Response(200, text=ddg_page()))

    results, backend = await search_web("anything", source="web")

    assert backend == "duckduckgo"
    assert [result.url for result in results] == ["https://example.com/live"]


@respx.mock
async def test_online_search_never_touches_the_corpus(hybrid, monkeypatch):
    called = False

    async def _should_not_run(query, max_results=None):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(search_module, "corpus_search", _should_not_run)
    respx.post(DDG).mock(return_value=httpx.Response(200, text=ddg_page()))

    await online_search("anything")
    assert called is False


@respx.mock
async def test_online_search_falls_back_to_searxng(monkeypatch):
    monkeypatch.setattr(settings, "searxng_url", "http://localhost:8080")
    respx.post(DDG).mock(return_value=httpx.Response(503, text="unavailable"))
    respx.get(SEARX).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"url": "https://example.org/x", "title": "Fallback", "content": "s"}
                ]
            },
        )
    )

    results, backend = await online_search("anything")
    assert backend == "searxng"
    assert results[0].title == "Fallback"


# --------------------------------------------------------- source=corpus


async def test_source_corpus_skips_the_web(hybrid, monkeypatch):
    fake_corpus(monkeypatch)
    results, backend = await search_web("anything", source="corpus")
    assert backend == "corpus"
    assert results[0].url.startswith("https://local.archive/")


async def test_source_corpus_without_a_corpus_explains_itself(online_only):
    with pytest.raises(SearchError, match="FETCH_NET_MODE"):
        await search_web("anything", source="corpus")


# ----------------------------------------------------------- source=both


@respx.mock
async def test_source_both_merges_with_corpus_first(hybrid, monkeypatch):
    fake_corpus(monkeypatch)
    respx.post(DDG).mock(return_value=httpx.Response(200, text=ddg_page()))

    results, backend = await search_web("anything", source="both")

    assert backend == "corpus+web"
    assert results[0].url.startswith("https://local.archive/")
    assert results[-1].url == "https://example.com/live"


@respx.mock
async def test_source_both_survives_the_web_failing(hybrid, monkeypatch):
    """One side failing must not lose the other."""
    fake_corpus(monkeypatch)
    respx.post(DDG).mock(return_value=httpx.Response(500, text="down"))

    results, backend = await search_web("anything", source="both")
    assert backend == "corpus"
    assert len(results) == 1


@respx.mock
async def test_source_both_survives_the_corpus_failing(hybrid, monkeypatch):
    fake_corpus(monkeypatch, error="nothing matched")
    respx.post(DDG).mock(return_value=httpx.Response(200, text=ddg_page()))

    results, backend = await search_web("anything", source="both")
    # The real web backend is reported, not a generic "web": a web-only
    # outcome has to render identically whether or not a corpus was consulted,
    # and the specific name is what carries a SearXNG fallback.
    assert backend == "duckduckgo"
    assert results[0].url == "https://example.com/live"


@respx.mock
async def test_source_both_raises_only_when_both_fail(hybrid, monkeypatch):
    fake_corpus(monkeypatch, error="nothing matched")
    respx.post(DDG).mock(return_value=httpx.Response(500, text="down"))

    with pytest.raises(SearchError) as caught:
        await search_web("anything", source="both")
    message = str(caught.value)
    assert "corpus:" in message and "web:" in message


@respx.mock
async def test_both_deduplicates_an_archived_url(hybrid, monkeypatch):
    fake_corpus(
        monkeypatch,
        rows=[("Mirrored", "https://example.com/live", "The archived copy.")],
    )
    respx.post(DDG).mock(return_value=httpx.Response(200, text=ddg_page()))

    results, _ = await combined_search("anything")
    assert len(results) == 1
    assert results[0].title == "Mirrored"


async def test_both_without_a_corpus_is_just_the_web(online_only, monkeypatch):
    async def _online(query, max_results=None):
        return [SearchResult(title="W", url="https://example.com/w", snippet="s")], "duckduckgo"

    monkeypatch.setattr(search_module, "online_search", _online)
    results, backend = await search_web("anything", source="both")
    assert backend == "duckduckgo"
    assert len(results) == 1


# ----------------------------------------------------------- offline mode


async def test_offline_mode_refuses_an_explicit_web_search(monkeypatch):
    """A deployment that declares itself air-gapped must not be talked out of
    it by a tool argument."""
    monkeypatch.setattr(settings, "net_mode", "offline")
    with pytest.raises(SearchError, match="offline mode"):
        await search_web("anything", source="web")


async def test_offline_mode_degrades_auto_and_both_to_the_corpus(monkeypatch):
    monkeypatch.setattr(settings, "net_mode", "offline")
    fake_corpus(monkeypatch)

    for source in ("auto", "both"):
        results, backend = await search_web("anything", source=source)
        assert backend == "corpus", source
        assert results


@respx.mock
async def test_offline_mode_never_issues_a_web_request(monkeypatch):
    monkeypatch.setattr(settings, "net_mode", "offline")
    fake_corpus(monkeypatch)
    route = respx.post(DDG).mock(return_value=httpx.Response(200, text=ddg_page()))

    await search_web("anything", source="both")
    assert route.call_count == 0


# ----------------------------------------------------------- full matrix


@respx.mock
@pytest.mark.parametrize("source", ["auto", "web", "corpus", "both"])
async def test_offline_mode_issues_no_web_request_for_any_source(monkeypatch, source):
    """The air-gap guarantee, stated once per source value.

    FETCH_NET_MODE=offline is a promise about network behaviour, so no tool
    argument may cause a request. Asserting the request count rather than the
    return value is the point: a refusal that still made the call would pass a
    looser test.
    """
    monkeypatch.setattr(settings, "net_mode", "offline")
    monkeypatch.setattr(settings, "searxng_url", "")
    fake_corpus(monkeypatch)
    route = respx.post(DDG).mock(return_value=httpx.Response(200, text=ddg_page()))

    try:
        await search_web("anything", source=source)
    except SearchError:
        pass  # source='web' is refused outright, which is also correct

    assert route.call_count == 0, f"offline mode reached the web for source={source}"


@respx.mock
@pytest.mark.parametrize(
    ("mode", "source", "expected_backend"),
    [
        ("online", "auto", "duckduckgo"),
        ("online", "web", "duckduckgo"),
        ("online", "both", "duckduckgo"),
        ("hybrid", "auto", "corpus"),
        ("hybrid", "web", "duckduckgo"),
        ("hybrid", "corpus", "corpus"),
        ("hybrid", "both", "corpus+web"),
        ("offline", "auto", "corpus"),
        ("offline", "corpus", "corpus"),
        ("offline", "both", "corpus"),
    ],
)
async def test_dispatch_matrix(monkeypatch, mode, source, expected_backend):
    monkeypatch.setattr(settings, "net_mode", mode)
    monkeypatch.setattr(settings, "searxng_url", "")
    fake_corpus(monkeypatch)
    respx.post(DDG).mock(return_value=httpx.Response(200, text=ddg_page()))

    _, backend = await search_web("anything", source=source)
    assert backend == expected_backend


async def test_corpus_source_is_refused_when_no_corpus_is_served(monkeypatch):
    monkeypatch.setattr(settings, "net_mode", "online")
    with pytest.raises(SearchError, match="No local corpus"):
        await search_web("anything", source="corpus")


# ------------------------------------------------------------------ auto


@respx.mock
async def test_auto_still_prefers_the_corpus(hybrid, monkeypatch):
    """The original behaviour has to survive, since it is right for an
    air-gapped deployment."""
    fake_corpus(monkeypatch)
    route = respx.post(DDG).mock(return_value=httpx.Response(200, text=ddg_page()))

    _, backend = await search_web("anything", source="auto")
    assert backend == "corpus"
    assert route.call_count == 0


@respx.mock
async def test_auto_falls_through_when_the_corpus_is_empty(hybrid, monkeypatch):
    fake_corpus(monkeypatch, error="nothing matched")
    respx.post(DDG).mock(return_value=httpx.Response(200, text=ddg_page()))

    _, backend = await search_web("anything", source="auto")
    assert backend == "duckduckgo"


# -------------------------------------------------------------- validation


async def test_an_unknown_source_is_rejected(hybrid):
    with pytest.raises(SearchError, match="Unknown search source"):
        await search_web("anything", source="internet")


async def test_the_tool_surfaces_an_unknown_source_as_a_tool_error(hybrid):
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="Unknown search source"):
        await run_web_search("anything", source="internet")


# ------------------------------------------------------------- labelling


def test_mixed_results_are_labelled():
    """Which results are local archive and which are live web changes how
    they should be weighed."""
    results = [SearchResult(title="t", url="u", snippet="s")]
    assert "local archive and the live web" in format_results(results, backend="corpus+web")
    assert "live web" in format_results(results, backend="web")


def test_corpus_only_results_are_not_labelled_as_a_fallback():
    results = [SearchResult(title="t", url="u", snippet="s")]
    rendered = format_results(results, backend="corpus")
    assert "fallback" not in rendered
    assert rendered.startswith("1. t")


def test_searxng_is_still_announced():
    results = [SearchResult(title="t", url="u", snippet="s")]
    assert "searxng" in format_results(results, backend="searxng")


# ------------------------------------------------------------------ tool


async def test_tool_exposes_the_source_argument():
    from mcp_fetch_server.server import create_mcp_server

    mcp = create_mcp_server()
    tool = next(t for t in await mcp.list_tools() if t.name == "web_search")
    assert "source" in tool.inputSchema["properties"]


# ------------------------------------------- review findings, now regressions


@respx.mock
async def test_both_respects_max_results(hybrid, monkeypatch):
    """Regression: each side clamped independently and the lists were
    concatenated, so asking for 3 returned 6 and the ceiling was 40."""
    fake_corpus(
        monkeypatch,
        rows=[(f"C{i}", f"https://local.archive/doc/c{i}", "s") for i in range(5)],
    )
    page = "".join(
        '<div class="result">'
        f'<a class="result__a" href="https://example.com/w{i}">W{i}</a>'
        '<a class="result__snippet">s</a></div>'
        for i in range(5)
    )
    respx.post(DDG).mock(return_value=httpx.Response(200, text=page))

    results, _ = await search_web("anything", max_results=3, source="both")
    assert len(results) == 3


@respx.mock
async def test_both_gives_the_web_a_share_of_the_budget(hybrid, monkeypatch):
    """Taking corpus-first up to the limit would starve the web side whenever
    the archive alone could fill the request, defeating the point of asking
    for both."""
    fake_corpus(
        monkeypatch,
        rows=[(f"C{i}", f"https://local.archive/doc/c{i}", "s") for i in range(10)],
    )
    page = (
        '<div class="result">'
        '<a class="result__a" href="https://example.com/w0">W0</a>'
        '<a class="result__snippet">s</a></div>'
    )
    respx.post(DDG).mock(return_value=httpx.Response(200, text=page))

    results, backend = await search_web("anything", max_results=4, source="both")
    assert len(results) == 4
    assert any(r.url.startswith("https://example.com/") for r in results)
    assert backend == "corpus+web"


@respx.mock
async def test_both_keeps_the_searxng_signal(monkeypatch):
    """Regression: combined_search hardcoded "web", so a caller was never told
    DuckDuckGo had failed and SearXNG served the results."""
    monkeypatch.setattr(settings, "net_mode", "hybrid")
    monkeypatch.setattr(settings, "searxng_url", "http://localhost:8080")
    fake_corpus(monkeypatch, error="nothing matched")
    respx.post(DDG).mock(return_value=httpx.Response(503, text="down"))
    respx.get(SEARX).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"url": "https://example.org/x", "title": "Fallback", "content": "s"}
                ]
            },
        )
    )

    results, backend = await search_web("anything", source="both")
    assert backend == "searxng"
    assert "searxng" in format_results(results, backend=backend)


@respx.mock
async def test_both_reports_corpus_when_dedup_removed_every_web_result(hybrid, monkeypatch):
    """Regression: the backend name was computed before dedup, so a list that
    was entirely local archive was still labelled as mixed."""
    fake_corpus(
        monkeypatch,
        rows=[("Mirrored", "https://example.com/live", "The archived copy.")],
    )
    respx.post(DDG).mock(return_value=httpx.Response(200, text=ddg_page()))

    results, backend = await search_web("anything", source="both")
    assert len(results) == 1
    assert backend == "corpus"
    assert "live web" not in format_results(results, backend=backend)


async def test_offline_auto_explains_why_the_web_was_not_tried(monkeypatch):
    """Regression: degrading auto to the corpus dropped the explanation, so an
    air-gapped deployment reported only that the archive had no match."""
    monkeypatch.setattr(settings, "net_mode", "offline")
    fake_corpus(monkeypatch, error="No documents in the local archive match this query.")

    with pytest.raises(SearchError) as caught:
        await search_web("anything", source="auto")

    message = str(caught.value)
    assert "offline mode" in message
    assert "web was not searched" in message


async def test_explicit_corpus_miss_is_not_dressed_up_as_offline(hybrid, monkeypatch):
    """Asking for the corpus and getting nothing is not an offline-mode story."""
    fake_corpus(monkeypatch, error="No documents match.")
    with pytest.raises(SearchError) as caught:
        await search_web("anything", source="corpus")
    assert "offline mode" not in str(caught.value)


async def test_offline_refusal_does_not_tell_the_caller_how_to_disable_it(monkeypatch):
    """The operator remedy belongs in the log, not in the response to an
    untrusted caller."""
    monkeypatch.setattr(settings, "net_mode", "offline")
    with pytest.raises(SearchError) as caught:
        await search_web("anything", source="web")
    assert "FETCH_NET_MODE" not in str(caught.value)


@respx.mock
async def test_online_search_refuses_offline_mode_on_its_own(monkeypatch):
    """The air gap is asserted where the request happens, not only by call
    position, so a future caller reaching for this function cannot bypass it."""
    monkeypatch.setattr(settings, "net_mode", "offline")
    route = respx.post(DDG).mock(return_value=httpx.Response(200, text=ddg_page()))

    with pytest.raises(SearchError, match="offline mode"):
        await online_search("anything")
    assert route.call_count == 0


@respx.mock
async def test_combined_search_refuses_offline_mode_on_its_own(monkeypatch):
    monkeypatch.setattr(settings, "net_mode", "offline")
    fake_corpus(monkeypatch)
    route = respx.post(DDG).mock(return_value=httpx.Response(200, text=ddg_page()))

    results, backend = await combined_search("anything")
    # The corpus still answers; the web side is refused without a request.
    assert backend == "corpus"
    assert route.call_count == 0


@respx.mock
async def test_a_web_result_cannot_impersonate_the_archive(hybrid, monkeypatch):
    """A live-web hit served from the archive's own host would sit beside real
    local documents and inherit their credibility, while its title and snippet
    are attacker-influenceable."""
    monkeypatch.setattr(settings, "site_base_url", "https://local.archive")
    fake_corpus(monkeypatch)
    spoofed = (
        '<div class="result">'
        '<a class="result__a" href="https://local.archive/doc/trusted-policy">'
        "Internal Policy</a>"
        '<a class="result__snippet">Attacker text served from the live web.</a></div>'
    )
    respx.post(DDG).mock(return_value=httpx.Response(200, text=spoofed))

    results, backend = await search_web("anything", source="both")

    assert all("trusted-policy" not in result.url for result in results)
    assert backend == "corpus"


async def test_tool_schema_constrains_source_to_an_enum():
    from mcp_fetch_server.server import create_mcp_server

    mcp = create_mcp_server()
    tool = next(t for t in await mcp.list_tools() if t.name == "web_search")
    assert tool.inputSchema["properties"]["source"]["enum"] == [
        "auto",
        "web",
        "corpus",
        "both",
    ]
