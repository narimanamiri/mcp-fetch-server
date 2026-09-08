"""The local archive as a browsable website.

An agent that can only call ``rag_search`` gets passages. An agent that can
*browse* gets a homepage, category indexes, document pages and links between
them, which is how it already knows how to explore the web. So the corpus is
served as a site under ``FETCH_SITE_BASE_URL``:

    /                      homepage: categories, recent documents
    /category/<path>       documents filed under one category
    /tag/<tag>             documents carrying one tag
    /search?q=...          search results, same ranking as web_search
    /doc/<slug>-<hash>     one document, in full
    /doc/<slug>-<hash>/raw the same text with no header or navigation

Pages are built as a :class:`Page` and rendered twice, to Markdown for reading
and to HTML for link extraction. Generating HTML and converting it back would
be both wasteful and lossy: the stored blob is already Markdown, and a round
trip would shift the character offsets that citations depend on.
"""

from __future__ import annotations

import html as html_module
import logging
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote, unquote, urlparse

from mcp_fetch_server.config import settings
from mcp_fetch_server.converters import ARCHIVE_PREFIX
from mcp_fetch_server.rag.catalog import Catalog, DocumentRecord
from mcp_fetch_server.rag.taxonomy import Taxonomy

logger = logging.getLogger(__name__)

RECENT_LIMIT = 25
CATEGORY_PAGE_SIZE = 50
RELATED_LIMIT = 6


@dataclass(slots=True)
class PageLink:
    url: str
    text: str


@dataclass(slots=True)
class Page:
    url: str
    title: str
    kind: str
    body: str = ""
    links: list[PageLink] = field(default_factory=list)
    status_code: int = 200

    def render_markdown(self) -> str:
        # The raw view exists to give back exactly what was ingested, so it
        # gets no title header and no navigation: the document already opens
        # with its own heading.
        if self.kind == "document_raw":
            return ARCHIVE_PREFIX + self.body.strip()

        parts = [f"# {self.title}", self.body.strip()]
        if self.links:
            listing = "\n".join(f"- [{link.text}]({link.url})" for link in self.links)
            parts.append(f"## Links\n\n{listing}")
        return ARCHIVE_PREFIX + "\n\n".join(part for part in parts if part.strip())

    def render_html(self) -> str:
        escape = html_module.escape
        anchors = "\n".join(
            f'<li><a href="{escape(link.url, quote=True)}">{escape(link.text)}</a></li>'
            for link in self.links
        )
        nav = f"<nav><ul>\n{anchors}\n</ul></nav>" if anchors else ""
        return (
            "<!DOCTYPE html>\n<html><head>"
            f"<title>{escape(self.title)}</title>"
            '<meta charset="utf-8"></head><body>'
            f"<h1>{escape(self.title)}</h1>"
            f"<div id=\"content\"><pre>{escape(self.body)}</pre></div>"
            f"{nav}</body></html>"
        )


def site_root() -> str:
    return settings.site_base_url.rstrip("/")


def category_url(path: str) -> str:
    return f"{site_root()}/category/{quote(path, safe='/')}"


def tag_url(tag: str) -> str:
    return f"{site_root()}/tag/{quote(tag, safe='')}"


def search_url(query: str) -> str:
    return f"{site_root()}/search?q={quote(query, safe='')}"


def _document_summary_line(record: DocumentRecord) -> str:
    title = record.title or record.doc_id
    line = f"- [{title}]({record.url})"
    details: list[str] = []
    if record.doctype:
        details.append(record.doctype)
    if record.language:
        details.append(record.language)
    if record.page_count:
        details.append(f"{record.page_count} pages")
    if details:
        line += f" — {', '.join(details)}"
    if record.summary:
        line += f"\n  {record.summary.strip()}"
    return line


def _category_counts(catalog: Catalog) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in catalog.iter_documents():
        for path in record.categories:
            counts[path] = counts.get(path, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def build_home(catalog: Catalog) -> Page:
    stats = catalog.stats()
    if not stats["documents"]:
        return Page(
            url=f"{site_root()}/",
            title="Local Archive",
            kind="home",
            body=(
                "This archive is empty. Documents are added with "
                "`mcp-fetch-server ingest <path>`."
            ),
        )

    counts = _category_counts(catalog)
    taxonomy = Taxonomy.load()
    recent = catalog.list_documents(limit=RECENT_LIMIT)

    sections = [
        "An offline document archive. Browse by category, or search.",
        "## About this archive\n\n"
        f"- Documents: {stats['documents']}\n"
        f"- Passages: {stats['chunks']}\n"
        f"- Formats: {', '.join(f'{k} ({v})' for k, v in stats['by_doctype'].items())}\n"
        f"- Languages: {', '.join(f'{k} ({v})' for k, v in stats['by_language'].items())}",
    ]

    if counts:
        rows = []
        for path, count in counts.items():
            category = taxonomy.get(path)
            label = category.label if category else path
            description = f" — {category.description}" if category and category.description else ""
            rows.append(f"- [{label}]({category_url(path)}) ({count}){description}")
        sections.append("## Categories\n\n" + "\n".join(rows))

    sections.append(
        "## Recent documents\n\n"
        + "\n".join(_document_summary_line(record) for record in recent)
    )

    links = [
        PageLink(url=category_url(path), text=path) for path in counts
    ] + [PageLink(url=record.url, text=record.title or record.doc_id) for record in recent]

    return Page(
        url=f"{site_root()}/",
        title="Local Archive",
        kind="home",
        body="\n\n".join(sections),
        links=links,
    )


def build_category(catalog: Catalog, path: str) -> Page:
    taxonomy = Taxonomy.load()
    category = taxonomy.get(path)
    matches = [
        record for record in catalog.iter_documents() if path in record.categories
    ][:CATEGORY_PAGE_SIZE]

    title = category.label if category else path
    if not matches:
        return Page(
            url=category_url(path),
            title=f"Category: {title}",
            kind="category",
            body=(
                f"No documents are filed under `{path}`.\n\n"
                f"[All categories]({site_root()}/)"
            ),
            links=[PageLink(url=f"{site_root()}/", text="Local Archive")],
            status_code=404,
        )

    header = category.description if category and category.description else ""
    body = "\n\n".join(
        part
        for part in [
            header,
            f"{len(matches)} document(s) in this category.",
            "\n".join(_document_summary_line(record) for record in matches),
        ]
        if part
    )

    return Page(
        url=category_url(path),
        title=f"Category: {title}",
        kind="category",
        body=body,
        links=[PageLink(url=f"{site_root()}/", text="Local Archive")]
        + [PageLink(url=record.url, text=record.title or record.doc_id) for record in matches],
    )


def build_tag(catalog: Catalog, tag: str) -> Page:
    lowered = tag.strip().lower()
    matches = [
        record
        for record in catalog.iter_documents()
        if any(existing.lower() == lowered for existing in record.tags)
    ][:CATEGORY_PAGE_SIZE]

    if not matches:
        return Page(
            url=tag_url(tag),
            title=f"Tag: {tag}",
            kind="tag",
            body=f"No documents are tagged `{tag}`.",
            links=[PageLink(url=f"{site_root()}/", text="Local Archive")],
            status_code=404,
        )

    return Page(
        url=tag_url(tag),
        title=f"Tag: {tag}",
        kind="tag",
        body=f"{len(matches)} document(s) tagged `{tag}`.\n\n"
        + "\n".join(_document_summary_line(record) for record in matches),
        links=[PageLink(url=f"{site_root()}/", text="Local Archive")]
        + [PageLink(url=record.url, text=record.title or record.doc_id) for record in matches],
    )


def _related_documents(catalog: Catalog, record: DocumentRecord) -> list[DocumentRecord]:
    """Other documents sharing a category, so a reader can keep going.

    Cheap and deterministic: vector neighbours would be better but would make
    every page render depend on the embedding model being up.
    """
    if not record.categories:
        return []
    wanted = set(record.categories)
    related = [
        other
        for other in catalog.iter_documents()
        if other.doc_id != record.doc_id and wanted & set(other.categories)
    ]
    return related[:RELATED_LIMIT]


def build_document(catalog: Catalog, record: DocumentRecord, *, raw: bool = False) -> Page:
    text = catalog.read_blob(record.blob_path)

    if raw:
        return Page(
            url=f"{record.url}/raw",
            title=record.title or record.doc_id,
            kind="document_raw",
            body=text,
        )

    facts: list[str] = []
    if record.doctype:
        facts.append(f"**Format:** {record.doctype}")
    if record.page_count:
        facts.append(f"**Pages:** {record.page_count}")
    if record.language:
        facts.append(f"**Language:** {record.language}")
    if record.published_at:
        facts.append(f"**Published:** {record.published_at}")
    if record.source_path:
        facts.append(f"**Source file:** `{record.source_path}`")

    header_parts: list[str] = []
    if record.summary:
        header_parts.append(f"> {record.summary.strip()}")
    if facts:
        header_parts.append(" · ".join(facts))
    if record.categories:
        header_parts.append(
            "**Categories:** "
            + ", ".join(f"[{path}]({category_url(path)})" for path in record.categories)
        )
    if record.tags:
        header_parts.append(
            "**Tags:** " + ", ".join(f"[{tag}]({tag_url(tag)})" for tag in record.tags)
        )

    related = _related_documents(catalog, record)
    footer_parts: list[str] = []
    if related:
        footer_parts.append(
            "## Related documents\n\n"
            + "\n".join(f"- [{other.title or other.doc_id}]({other.url})" for other in related)
        )

    body = "\n\n".join(
        part
        for part in ["\n\n".join(header_parts), "---", text, "\n\n".join(footer_parts)]
        if part.strip()
    )

    links = [
        PageLink(url=f"{site_root()}/", text="Local Archive"),
        PageLink(url=f"{record.url}/raw", text="Original text without navigation"),
    ]
    links += [PageLink(url=category_url(path), text=path) for path in record.categories]
    links += [PageLink(url=other.url, text=other.title or other.doc_id) for other in related]

    return Page(
        url=record.url,
        title=record.title or record.doc_id,
        kind="document",
        body=body,
        links=links,
    )


def build_not_found(url: str, catalog: Catalog) -> Page:
    """A 404 that tells an agent what it *can* do, not just that it failed."""
    counts = _category_counts(catalog)
    suggestions = "\n".join(f"- [{path}]({category_url(path)})" for path in list(counts)[:10])
    body = (
        f"No document in the local archive is published at `{url}`.\n\n"
        f"Browse the archive from its homepage, or search it.\n\n"
        + (f"### Categories\n\n{suggestions}" if suggestions else "")
    )
    return Page(
        url=url,
        title="Not Found",
        kind="notfound",
        body=body,
        links=[PageLink(url=f"{site_root()}/", text="Local Archive")],
        status_code=404,
    )


@dataclass(slots=True)
class ParsedRoute:
    kind: str  # home | category | tag | search | document | document_raw | unknown
    value: str = ""


def parse_route(url: str) -> ParsedRoute | None:
    """Map a URL on the archive host to a route. None if it is not ours."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host or host != settings.site_host:
        return None

    path = unquote(parsed.path or "/").rstrip("/")

    if path in ("", "/"):
        return ParsedRoute(kind="home")
    if path == "/search":
        query = parse_qs(parsed.query).get("q", [""])[0]
        return ParsedRoute(kind="search", value=query)
    if path.startswith("/category/"):
        return ParsedRoute(kind="category", value=path[len("/category/") :])
    if path.startswith("/tag/"):
        return ParsedRoute(kind="tag", value=path[len("/tag/") :])
    if path.startswith("/doc/"):
        remainder = path[len("/doc/") :]
        if remainder.endswith("/raw"):
            return ParsedRoute(kind="document_raw", value=remainder[: -len("/raw")])
        return ParsedRoute(kind="document", value=remainder)

    return ParsedRoute(kind="unknown", value=path)


def document_url_for(slug: str) -> str:
    """Canonical document URL for a slug taken from a route."""
    return f"{site_root()}/doc/{quote(slug, safe='')}"
