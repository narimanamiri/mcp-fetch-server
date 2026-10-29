"""Web search backends: DuckDuckGo HTML scraping (primary) and an optional
local SearXNG JSON API (fallback).

DuckDuckGo requires no signup/credentials, which is why it's the default and
primary backend. The tradeoff is that it depends on DuckDuckGo's HTML markup,
which is unofficial and can change without notice; if results stop parsing,
`_parse_results` is the place to fix it. `online_search()` automatically
falls back to a SearXNG instance (see docker-compose.yml) when DuckDuckGo
fails, if one is configured via `FETCH_SEARXNG_URL`. `search_web()` dispatches
between the live web and the local corpus; see its docstring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from lxml import html as lxml_html

from mcp_fetch_server.config import settings
from mcp_fetch_server.http_headers import request_headers

logger = logging.getLogger(__name__)

SEARCH_URL = "https://html.duckduckgo.com/html/"

MAX_RESULTS = 20

SEARCH_SOURCES = ("auto", "web", "corpus", "both")

# Which results came from the curated archive and which from the live web
# changes how they should be weighed. A corpus-only list is deliberately
# unlabelled: in offline mode it is the only web there is, and announcing it
# as a fallback only invites the model to distrust it.
_BACKEND_LABELS = {
    "corpus+web": "results from the local archive and the live web",
    "web": "results from the live web",
    "searxng": "results via fallback backend: searxng",
}


class SearchError(Exception):
    """Raised when a web search cannot be completed."""


@dataclass(slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str


def _clean_redirect(href: str) -> str:
    # DuckDuckGo's HTML endpoint wraps result links behind /l/?uddg=<encoded-target>.
    if "duckduckgo.com/l/" in href or href.startswith("/l/"):
        parsed = urlparse(href if "://" in href else f"https:{href}")
        target = parse_qs(parsed.query).get("uddg")
        if target:
            return unquote(target[0])
    return href


def _parse_results(html_content: str, *, max_results: int) -> list[SearchResult]:
    try:
        document = lxml_html.fromstring(html_content)
    except Exception:
        return []

    results: list[SearchResult] = []
    result_xpath = '//div[contains(concat(" ", normalize-space(@class), " "), " result ")]'
    for node in document.xpath(result_xpath):
        link_nodes = node.xpath('.//a[contains(@class, "result__a")]')
        if not link_nodes:
            continue
        link_node = link_nodes[0]
        href = _clean_redirect((link_node.get("href") or "").strip())
        title = link_node.text_content().strip()
        if not href or not title:
            continue

        snippet_nodes = node.xpath(
            './/a[contains(@class, "result__snippet")] '
            '| .//div[contains(@class, "result__snippet")]'
        )
        snippet = snippet_nodes[0].text_content().strip() if snippet_nodes else ""

        results.append(SearchResult(title=title, url=href, snippet=snippet))
        if len(results) >= max_results:
            break

    return results


async def web_search(query: str, max_results: int | None = None) -> list[SearchResult]:
    query = query.strip()
    if not query:
        raise SearchError("Search query must not be empty")

    limit = _result_limit(max_results)

    timeout = httpx.Timeout(settings.search_timeout_seconds)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                SEARCH_URL,
                data={"q": query},
                headers=request_headers(),
                follow_redirects=True,
            )
    except httpx.HTTPError as exc:
        raise SearchError(f"Search request failed: {exc}") from exc

    if response.status_code >= 400:
        raise SearchError(f"Search provider returned HTTP {response.status_code}")

    results = _parse_results(response.text, max_results=limit)
    if not results:
        raise SearchError(
            "No results found for this query (or DuckDuckGo's page layout changed)."
        )
    return results


def _parse_searxng_results(payload: dict, *, max_results: int) -> list[SearchResult]:
    results: list[SearchResult] = []
    for item in payload.get("results", []):
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        if not url or not title:
            continue
        snippet = str(item.get("content") or "").strip()
        results.append(SearchResult(title=title, url=url, snippet=snippet))
        if len(results) >= max_results:
            break
    return results


async def searxng_search(query: str, max_results: int | None = None) -> list[SearchResult]:
    """Query a local/self-hosted SearXNG instance via its JSON API.

    Requires ``FETCH_SEARXNG_URL`` to be set (defaults to
    ``http://localhost:8080``, matching the bundled docker-compose.yml) and
    the instance's settings.yml to have ``json`` enabled under
    ``search.formats`` (already the case in ``searxng/settings.yml``).
    """
    base_url = settings.searxng_url.strip().rstrip("/")
    if not base_url:
        raise SearchError("SearXNG is not configured (FETCH_SEARXNG_URL is empty)")

    query = query.strip()
    if not query:
        raise SearchError("Search query must not be empty")

    limit = _result_limit(max_results)

    timeout = httpx.Timeout(settings.searxng_timeout_seconds)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{base_url}/search",
                params={"q": query, "format": "json"},
                headers={"Accept": "application/json"},
            )
    except Exception as exc:
        # Broad catch: an unreachable local Docker instance should degrade to
        # a clean SearchError, not an unhandled connection/transport error.
        raise SearchError(f"SearXNG request failed: {exc}") from exc

    if response.status_code >= 400:
        raise SearchError(f"SearXNG returned HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise SearchError(
            "SearXNG did not return JSON (is the 'json' format enabled in its "
            "settings.yml?)"
        ) from exc

    results = _parse_searxng_results(payload, max_results=limit)
    if not results:
        raise SearchError("SearXNG returned no results for this query.")
    return results


async def corpus_search(query: str, max_results: int | None = None) -> list[SearchResult]:
    """Search the local offline corpus, shaped as ordinary search results.

    Snippets come from the best-matching passage rather than a document
    summary, so an agent that only ever calls ``web_search`` still receives
    grounded, query-relevant text.
    """
    try:
        from mcp_fetch_server.rag.offline import search_corpus
    except ImportError as exc:
        raise SearchError("The offline corpus extra is not installed") from exc

    limit = _result_limit(max_results)

    try:
        rows = await search_corpus(query, limit)
    except Exception as exc:
        raise SearchError(f"Local corpus search failed: {exc}") from exc

    if not rows:
        raise SearchError("No documents in the local archive match this query.")
    return [SearchResult(title=title, url=url, snippet=snippet) for title, url, snippet in rows]


def _result_limit(max_results: int | None) -> int:
    """Clamp a requested result count to the supported range."""
    limit = max_results if max_results is not None else settings.search_max_results
    return max(1, min(limit, MAX_RESULTS))


async def online_search(
    query: str, max_results: int | None = None
) -> tuple[list[SearchResult], str]:
    """Search the live web only: DuckDuckGo, falling back to SearXNG.

    Never consults the local corpus, so it stays reachable once a corpus
    exists. Raises ``SearchError`` if every web backend fails.
    """
    # The air gap is asserted here, at the point where a request is actually
    # made, as well as in the dispatcher. Enforcing it only by call position
    # means a future caller that reaches for this function -- the name reads
    # harmless -- would silently break an advertised guarantee.
    if settings.net_mode == "offline":
        raise SearchError(
            "The server is in offline mode, so the live web cannot be searched."
        )

    try:
        return await web_search(query, max_results=max_results), "duckduckgo"
    except SearchError as ddg_exc:
        if not settings.searxng_url.strip():
            raise

        logger.warning("DuckDuckGo search failed, falling back to SearXNG: %s", ddg_exc)
        try:
            return await searxng_search(query, max_results=max_results), "searxng"
        except SearchError as searxng_exc:
            raise SearchError(
                f"Both search backends failed. DuckDuckGo: {ddg_exc} | SearXNG: {searxng_exc}"
            ) from searxng_exc


async def combined_search(
    query: str, max_results: int | None = None
) -> tuple[list[SearchResult], str]:
    """Search the corpus and the live web, corpus results first.

    For a question where local documents and current information both matter,
    returning one and silently dropping the other is the wrong trade. Either
    side failing is tolerated as long as the other answered.
    """
    limit = _result_limit(max_results)
    corpus_results: list[SearchResult] = []
    web_results: list[SearchResult] = []
    web_backend = ""
    failures: list[str] = []

    try:
        corpus_results = await corpus_search(query, max_results=max_results)
    except SearchError as exc:
        failures.append(f"corpus: {exc}")

    try:
        web_results, web_backend = await online_search(query, max_results=max_results)
    except SearchError as exc:
        failures.append(f"web: {exc}")

    # A live-web result served from the archive's own host would appear beside
    # genuinely local documents and inherit their credibility, while its title
    # and snippet are attacker-influenceable. Search rankings can be gamed, so
    # the archive's name is not left available to borrow.
    archive_host = settings.site_host
    if archive_host:
        web_results = [
            result
            for result in web_results
            if (urlparse(result.url).hostname or "").lower() != archive_host
        ]

    # Corpus URLs are unique to this archive, so a plain URL check is enough
    # to keep an archived page from appearing twice.
    seen = {result.url for result in corpus_results}
    fresh_web = [result for result in web_results if result.url not in seen]

    if not corpus_results and not fresh_web:
        raise SearchError(f"No results from either source. {' | '.join(failures)}")

    # Split the budget so asking for both actually returns both. Taking
    # corpus-first up to the limit would starve the web side whenever the
    # archive alone can fill the request, which defeats the point of asking.
    corpus_share = (limit + 1) // 2 if fresh_web else limit
    merged = [*corpus_results[:corpus_share], *fresh_web[: limit - corpus_share]]
    # Top up from whichever side still has results, so a short answer on one
    # side does not shrink the total.
    for extra in (corpus_results[corpus_share:], fresh_web[limit - corpus_share :]):
        for result in extra:
            if len(merged) >= limit:
                break
            if result not in merged:
                merged.append(result)

    # The reported backend describes what survived into the result list, not
    # what was fetched: labelling a list "corpus+web" after dedup removed
    # every web entry defeats the label's whole purpose.
    kept_web = [result for result in merged if result in fresh_web]
    kept_corpus = [result for result in merged if result in corpus_results]
    if not kept_web:
        return merged, "corpus"
    if not kept_corpus:
        return merged, web_backend or "web"
    return merged, "corpus+web"


async def search_web(
    query: str,
    max_results: int | None = None,
    *,
    source: str = "auto",
) -> tuple[list[SearchResult], str]:
    """Search one or both sources, returning ``(results, backend_name)``.

    ``source`` selects where to look:

    * ``auto``   - corpus first when one is configured, web as a fallback
    * ``web``    - the live internet only
    * ``corpus`` - the local archive only
    * ``both``   - both, corpus results first

    ``auto`` exists because it was the original behaviour, and it has a sharp
    edge worth naming: once the corpus holds anything relevant, it answers and
    the web is never consulted. That is right for an air-gapped deployment and
    wrong for a question about current events, which is why the other three
    are selectable.

    Offline mode refuses the web whatever is asked for. A deployment that
    declares itself air-gapped should not be talked out of it by a tool
    argument.
    """
    if source not in SEARCH_SOURCES:
        raise SearchError(
            f"Unknown search source: {source!r}. Use {', '.join(SEARCH_SOURCES)}."
        )

    is_offline = settings.net_mode == "offline"
    # A separate name rather than reassigning the parameter, so the requested
    # source and the one actually used stay distinguishable.
    effective_source = source
    degraded_from_offline = False

    if is_offline and source in ("auto", "web", "both"):
        if source == "web":
            # The operator remedy goes to the log rather than the response:
            # telling the untrusted caller how to switch off the air gap is
            # not useful to it, and it is the operator's decision.
            logger.info(
                "Refused an explicit web search in offline mode. Set "
                "FETCH_NET_MODE=hybrid to allow the live web."
            )
            raise SearchError(
                "The server is in offline mode, so the live web cannot be "
                "searched. Use source='corpus' to search the local archive."
            )
        # auto and both degrade to the corpus rather than failing outright.
        effective_source = "corpus"
        degraded_from_offline = True

    if effective_source == "web":
        return await online_search(query, max_results=max_results)

    if effective_source == "corpus":
        if not settings.offline_enabled:
            raise SearchError(
                "No local corpus is being served. Set FETCH_NET_MODE=hybrid or "
                "offline to enable it."
            )
        try:
            return await corpus_search(query, max_results=max_results), "corpus"
        except SearchError as corpus_exc:
            if degraded_from_offline:
                # Without this the caller is told only that the archive had no
                # match, and never why the web was not tried instead.
                raise SearchError(
                    f"{corpus_exc} The server is in offline mode, so the web "
                    "was not searched."
                ) from corpus_exc
            raise

    if effective_source == "both":
        if not settings.offline_enabled:
            return await online_search(query, max_results=max_results)
        return await combined_search(query, max_results=max_results)

    # auto, with a corpus available: prefer it, fall back to the web.
    if settings.offline_enabled:
        try:
            return await corpus_search(query, max_results=max_results), "corpus"
        except SearchError as corpus_exc:
            logger.info("Local corpus had no answer, falling back to the web: %s", corpus_exc)

    return await online_search(query, max_results=max_results)


def format_results(results: list[SearchResult], *, backend: str | None = None) -> str:
    if not results:
        return "[No results]"
    blocks = []
    for index, result in enumerate(results, start=1):
        block = f"{index}. {result.title}\n   URL: {result.url}\n   {result.snippet}"
        blocks.append(block.rstrip())
    body = "\n\n".join(blocks)
    # The corpus alone is not a "fallback" to announce: in offline mode it is
    # the only web there is, and saying so just invites the model to distrust
    # it. A mixed result set is different -- which results are local archive
    # and which are live web changes how they should be weighed.
    label = _BACKEND_LABELS.get(backend or "")
    if label:
        body = f"({label})\n\n{body}"
    return body
