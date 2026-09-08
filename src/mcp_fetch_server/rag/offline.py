"""Serving the corpus through the existing fetch tools.

This is the piece that makes the archive feel like the internet. ``fetch_url``,
``web_search`` and ``extract_links`` keep their signatures and their output
shape; they just resolve against the local catalog instead of the network. An
agent browses ingested documents with exactly the tools it already uses.

Resolution happens strictly before any network code runs, and never relaxes
the SSRF or robots checks that protect the online path: the archive host is
answered from SQLite and is never resolved, connected to, or validated as a
real address.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urldefrag

from mcp_fetch_server.config import settings
from mcp_fetch_server.rag import site
from mcp_fetch_server.rag.catalog import Catalog, CatalogError
from mcp_fetch_server.rag.site import Page

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LocalResponse:
    """A page served from the corpus, shaped like an HTTP response."""

    url: str
    status_code: int
    page: Page

    @property
    def content_type(self) -> str:
        return "text/markdown; charset=utf-8"

    def markdown(self) -> str:
        return self.page.render_markdown()

    def html(self) -> str:
        return self.page.render_html()


def _open_catalog() -> Catalog | None:
    try:
        return Catalog().open()
    except CatalogError as exc:
        logger.warning("Local corpus unavailable: %s", exc)
        return None


async def resolve(url: str, *, catalog: Catalog | None = None) -> LocalResponse | None:
    """Serve a URL from the corpus, or return None if the corpus has no answer.

    None means "not mine": the caller decides whether to fall through to the
    network (hybrid) or report a failure (offline).
    """
    if not url or not url.strip():
        return None

    # A fragment is a position within a page, not part of its identity.
    bare_url, _fragment = urldefrag(url.strip())

    active = catalog or _open_catalog()
    if active is None:
        return None
    owns_catalog = catalog is None

    try:
        # A document keeps whatever URL it came with, so an archived web page
        # is served from the corpus under its original address.
        record = active.get_document_by_url(bare_url)
        if record is not None:
            return LocalResponse(
                url=bare_url, status_code=200, page=site.build_document(active, record)
            )

        route = site.parse_route(bare_url)
        if route is None:
            return None  # not the archive host, and not an archived URL

        page = await _build_route_page(active, route, bare_url)
        return LocalResponse(url=bare_url, status_code=page.status_code, page=page)
    except Exception as exc:
        logger.exception("Failed to serve %s from the local corpus", bare_url)
        raise LocalArchiveError(f"Local archive failed to serve {bare_url}: {exc}") from exc
    finally:
        if owns_catalog:
            active.close()


class LocalArchiveError(Exception):
    """Raised when the archive exists but could not serve a page."""


async def _build_route_page(catalog: Catalog, route: site.ParsedRoute, url: str) -> Page:
    if route.kind == "home":
        return site.build_home(catalog)
    if route.kind == "category":
        return site.build_category(catalog, route.value)
    if route.kind == "tag":
        return site.build_tag(catalog, route.value)
    if route.kind == "search":
        return await build_search_page(catalog, route.value)
    if route.kind in ("document", "document_raw"):
        record = catalog.get_document_by_url(site.document_url_for(route.value))
        if record is None:
            return site.build_not_found(url, catalog)
        return site.build_document(catalog, record, raw=route.kind == "document_raw")
    return site.build_not_found(url, catalog)


async def build_search_page(catalog: Catalog, query: str) -> Page:
    """Render a results page, so browsing and searching agree with each other."""
    cleaned = (query or "").strip()
    if not cleaned:
        return Page(
            url=site.search_url(""),
            title="Search the archive",
            kind="search",
            body="Provide a query, for example `/search?q=your+question`.",
            links=[site.PageLink(url=f"{site.site_root()}/", text="Local Archive")],
            status_code=400,
        )

    from mcp_fetch_server.rag.retrieve import Retriever

    retriever = Retriever(catalog=catalog)
    try:
        result = await retriever.search(cleaned, top_k=10)
    except Exception as exc:
        logger.warning("Archive search failed for %r: %s", cleaned, exc)
        return Page(
            url=site.search_url(cleaned),
            title=f"Search: {cleaned}",
            kind="search",
            body=(
                f"The archive index could not be searched: {exc}\n\n"
                "It may not have been built yet."
            ),
            links=[site.PageLink(url=f"{site.site_root()}/", text="Local Archive")],
            status_code=503,
        )
    finally:
        # Closes the vector store but leaves the caller's catalog open, since
        # the retriever did not open it.
        retriever.close()

    if not result:
        return Page(
            url=site.search_url(cleaned),
            title=f"Search: {cleaned}",
            kind="search",
            body=f"No documents in the archive match `{cleaned}`.",
            links=[site.PageLink(url=f"{site.site_root()}/", text="Local Archive")],
            status_code=404,
        )

    blocks: list[str] = []
    links: list[site.PageLink] = [site.PageLink(url=f"{site.site_root()}/", text="Local Archive")]
    for index, hit in enumerate(result.hits, start=1):
        heading = f"{index}. [{hit.title or hit.doc_id}]({hit.citation_url})"
        if hit.heading_trail:
            heading += f" — {hit.heading_trail}"
        snippet = " ".join(hit.text.split())[:320]
        blocks.append(f"{heading}\n\n{snippet}")
        links.append(site.PageLink(url=hit.citation_url, text=hit.title or hit.doc_id))

    return Page(
        url=site.search_url(cleaned),
        title=f"Search: {cleaned}",
        kind="search",
        body=f"{len(result.hits)} result(s) for `{cleaned}`.\n\n" + "\n\n".join(blocks),
        links=links,
    )


async def search_corpus(query: str, max_results: int) -> list[tuple[str, str, str]]:
    """Search the corpus and return (title, url, snippet) triples.

    Shaped for ``web_search`` so an agent gets an ordinary result list, with
    snippets taken from the best-matching passage rather than a document
    summary. That means an agent which only ever calls ``web_search`` still
    receives grounded, query-relevant text.
    """
    cleaned = (query or "").strip()
    if not cleaned:
        return []

    from mcp_fetch_server.rag.retrieve import Retriever

    retriever = Retriever()
    try:
        result = await retriever.search(cleaned, top_k=max_results, per_document=1)
    finally:
        retriever.close()

    return [
        (
            hit.title or hit.doc_id,
            hit.citation_url,
            " ".join(hit.text.split())[:300],
        )
        for hit in result.hits
    ]


def offline_only() -> bool:
    """True when the network must never be used."""
    return settings.net_mode == "offline"


def corpus_enabled() -> bool:
    """True when the corpus should be consulted at all."""
    return settings.offline_enabled
