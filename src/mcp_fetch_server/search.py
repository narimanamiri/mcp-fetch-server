"""Web search backends: DuckDuckGo HTML scraping (primary) and an optional
local SearXNG JSON API (fallback).

DuckDuckGo requires no signup/credentials, which is why it's the default and
primary backend. The tradeoff is that it depends on DuckDuckGo's HTML markup,
which is unofficial and can change without notice; if results stop parsing,
`_parse_results` is the place to fix it. `search_web()` automatically falls
back to a SearXNG instance (see docker-compose.yml) when DuckDuckGo fails, if
one is configured via `FETCH_SEARXNG_URL`.
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

    limit = max_results if max_results is not None else settings.search_max_results
    limit = max(1, min(limit, 20))

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

    limit = max_results if max_results is not None else settings.search_max_results
    limit = max(1, min(limit, 20))

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


async def search_web(
    query: str, max_results: int | None = None
) -> tuple[list[SearchResult], str]:
    """Search the web, preferring DuckDuckGo and falling back to SearXNG.

    Returns ``(results, backend_name)``. Raises ``SearchError`` only if both
    backends fail (or if SearXNG isn't configured and DuckDuckGo fails).
    """
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


def format_results(results: list[SearchResult], *, backend: str | None = None) -> str:
    if not results:
        return "[No results]"
    blocks = []
    for index, result in enumerate(results, start=1):
        block = f"{index}. {result.title}\n   URL: {result.url}\n   {result.snippet}"
        blocks.append(block.rstrip())
    body = "\n\n".join(blocks)
    if backend and backend != "duckduckgo":
        body = f"(results via fallback backend: {backend})\n\n{body}"
    return body
