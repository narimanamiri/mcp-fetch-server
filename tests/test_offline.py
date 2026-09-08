"""Tests for the simulated internet: site rendering and offline resolution."""

from __future__ import annotations

import httpx
import pytest
import respx

from mcp_fetch_server.config import settings
from mcp_fetch_server.converters import ARCHIVE_PREFIX
from mcp_fetch_server.fetch_service import fetch_and_record
from mcp_fetch_server.fetcher import FetchError
from mcp_fetch_server.rag import site
from mcp_fetch_server.rag.catalog import Catalog, DocumentRecord, make_doc_id, mint_url
from mcp_fetch_server.rag.documents import Chunk
from mcp_fetch_server.rag.offline import resolve
from mcp_fetch_server.rag.taxonomy import Category, Taxonomy

BASE = "https://local.archive"


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    """A small archive with two documents, one Persian, both categorised."""
    monkeypatch.setattr(settings, "corpus_data_dir", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "site_base_url", BASE)
    monkeypatch.setattr(settings, "net_mode", "offline")

    Taxonomy(
        categories=[
            Category(path="research", label="Research", description="Studies and results"),
            Category(path="manuals", label="Manuals"),
        ]
    ).save()

    with Catalog() as catalog:
        add(
            catalog,
            title="Retrieval Guide",
            text="# Retrieval Guide\n\n## Dense\n\nDense retrievers embed text.",
            categories=["manuals"],
            tags=["retrieval"],
            summary="How retrieval works.",
        )
        add(
            catalog,
            title="گزارش سالانه",
            text="# گزارش سالانه\n\n## مقدمه\n\nنتایج پژوهش بازیابی اطلاعات.",
            categories=["research"],
            tags=["پژوهش"],
            language="fa",
            summary="نتایج پژوهش.",
        )
        yield catalog


def add(catalog: Catalog, *, title: str, text: str, **kwargs) -> DocumentRecord:
    source = f"/corpus/{abs(hash(title)) % 10000}.md"
    url = mint_url(title, source, base_url=BASE)
    digest, blob_path = catalog.store_blob(text)
    defaults = dict(
        doc_id=make_doc_id(url),
        url=url,
        content_hash=digest,
        doctype="markdown",
        blob_path=blob_path,
        source_path=source,
        title=title,
        language="en",
        char_count=len(text),
    )
    defaults.update(kwargs)
    record = DocumentRecord(**defaults)
    catalog.upsert_document(record)
    catalog.replace_chunks(
        record.doc_id,
        [Chunk(doc_id=record.doc_id, chunk_index=0, text=text, char_start=0, char_end=len(text))],
    )
    return record


def document(catalog: Catalog, title: str) -> DocumentRecord:
    return next(r for r in catalog.iter_documents() if r.title == title)


# ----------------------------------------------------------------- routes


@pytest.mark.parametrize(
    ("url", "kind", "value"),
    [
        (f"{BASE}/", "home", ""),
        (f"{BASE}", "home", ""),
        (f"{BASE}/category/research", "category", "research"),
        (f"{BASE}/tag/rag", "tag", "rag"),
        (f"{BASE}/search?q=hello", "search", "hello"),
        (f"{BASE}/doc/some-slug-1234", "document", "some-slug-1234"),
        (f"{BASE}/doc/some-slug-1234/raw", "document_raw", "some-slug-1234"),
        (f"{BASE}/nonsense", "unknown", "/nonsense"),
    ],
)
def test_parse_route(monkeypatch, url, kind, value):
    monkeypatch.setattr(settings, "site_base_url", BASE)
    route = site.parse_route(url)
    assert route is not None
    assert (route.kind, route.value) == (kind, value)


def test_parse_route_ignores_other_hosts(monkeypatch):
    monkeypatch.setattr(settings, "site_base_url", BASE)
    assert site.parse_route("https://example.com/doc/x") is None
    assert site.parse_route("not a url") is None


def test_parse_route_decodes_percent_encoding(monkeypatch):
    monkeypatch.setattr(settings, "site_base_url", BASE)
    route = site.parse_route(f"{BASE}/tag/%D9%BE%DA%98%D9%88%D9%87%D8%B4")
    assert route.value == "پژوهش"


# ------------------------------------------------------------- rendering


def test_page_renders_markdown_and_html():
    page = site.Page(
        url=f"{BASE}/",
        title="Local Archive",
        kind="home",
        body="Body text.",
        links=[site.PageLink(url=f"{BASE}/category/x", text="X")],
    )
    markdown = page.render_markdown()
    assert markdown.startswith(ARCHIVE_PREFIX)
    assert "# Local Archive" in markdown
    assert f"[X]({BASE}/category/x)" in markdown

    html = page.render_html()
    assert f'<a href="{BASE}/category/x">X</a>' in html
    assert "<title>Local Archive</title>" in html


def test_html_rendering_escapes_content():
    page = site.Page(url=BASE, title="<script>", kind="home", body="a & b <tag>")
    html = page.render_html()
    title = html.split("<title>")[1].split("</title>")[0]
    assert title == "&lt;script&gt;"
    assert "&amp;" in html
    assert "<tag>" not in html


def test_raw_page_adds_no_title_or_navigation():
    """The raw view exists to return exactly what was ingested."""
    page = site.Page(
        url=f"{BASE}/doc/x/raw",
        title="Doc",
        kind="document_raw",
        body="# Doc\n\nOriginal body.",
        links=[site.PageLink(url=BASE, text="Home")],
    )
    rendered = page.render_markdown()
    assert rendered == ARCHIVE_PREFIX + "# Doc\n\nOriginal body."
    assert "## Links" not in rendered


# ----------------------------------------------------------------- pages


def test_home_lists_categories_and_documents(corpus):
    page = site.build_home(corpus)
    body = page.render_markdown()
    assert "Documents: 2" in body
    assert "[Research](https://local.archive/category/research)" in body
    assert "Studies and results" in body
    assert "Retrieval Guide" in body
    assert "گزارش سالانه" in body


def test_home_on_an_empty_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_data_dir", str(tmp_path / "empty"))
    with Catalog() as catalog:
        page = site.build_home(catalog)
    assert "empty" in page.render_markdown()
    assert "ingest" in page.render_markdown()


def test_category_page_lists_only_its_documents(corpus):
    page = site.build_category(corpus, "research")
    body = page.render_markdown()
    assert "گزارش سالانه" in body
    assert "Retrieval Guide" not in body
    assert page.status_code == 200


def test_unknown_category_is_404_but_still_navigable(corpus):
    page = site.build_category(corpus, "does-not-exist")
    assert page.status_code == 404
    assert f"{BASE}/" in page.render_markdown()


def test_tag_page(corpus):
    page = site.build_tag(corpus, "پژوهش")
    assert "گزارش سالانه" in page.render_markdown()
    assert site.build_tag(corpus, "absent").status_code == 404


def test_document_page_has_metadata_links_and_body(corpus):
    record = document(corpus, "Retrieval Guide")
    page = site.build_document(corpus, record)
    body = page.render_markdown()

    assert "How retrieval works." in body
    assert "Dense retrievers embed text." in body
    assert f"[manuals]({BASE}/category/manuals)" in body
    assert "Original text without navigation" in body
    assert page.status_code == 200


def test_document_page_links_related_documents(corpus):
    """Two documents share no category here, so there is nothing to relate."""
    record = document(corpus, "Retrieval Guide")
    assert site._related_documents(corpus, record) == []

    record.categories = ["research"]
    corpus.upsert_document(record)
    related = site._related_documents(corpus, document(corpus, "گزارش سالانه"))
    assert [other.title for other in related] == ["Retrieval Guide"]


def test_not_found_page_suggests_categories(corpus):
    page = site.build_not_found(f"{BASE}/doc/absent", corpus)
    assert page.status_code == 404
    body = page.render_markdown()
    assert "No document in the local archive" in body
    assert "research" in body


# -------------------------------------------------------------- resolver


async def test_resolve_serves_a_document_by_its_url(corpus):
    record = document(corpus, "Retrieval Guide")
    response = await resolve(record.url, catalog=corpus)
    assert response is not None
    assert response.status_code == 200
    assert "Dense retrievers embed text." in response.markdown()


async def test_resolve_ignores_a_fragment(corpus):
    record = document(corpus, "Retrieval Guide")
    response = await resolve(f"{record.url}#p3", catalog=corpus)
    assert response is not None
    assert response.url == record.url


async def test_resolve_serves_the_raw_view(corpus):
    record = document(corpus, "گزارش سالانه")
    response = await resolve(f"{record.url}/raw", catalog=corpus)
    assert response is not None
    expected = "# گزارش سالانه\n\n## مقدمه\n\nنتایج پژوهش بازیابی اطلاعات."
    assert response.markdown() == ARCHIVE_PREFIX + expected


async def test_resolve_serves_home_and_category(corpus):
    home = await resolve(f"{BASE}/", catalog=corpus)
    assert home is not None and home.status_code == 200

    category = await resolve(f"{BASE}/category/manuals", catalog=corpus)
    assert category is not None
    assert "Retrieval Guide" in category.markdown()


async def test_resolve_returns_404_page_for_unknown_document(corpus):
    response = await resolve(f"{BASE}/doc/absent-0000", catalog=corpus)
    assert response is not None
    assert response.status_code == 404


async def test_resolve_declines_foreign_urls(corpus):
    assert await resolve("https://example.com/page", catalog=corpus) is None
    assert await resolve("", catalog=corpus) is None


async def test_resolve_serves_an_archived_web_url(corpus):
    """A document keeps whatever URL it came with, so an archived page is
    served from the corpus under its original address."""
    add(
        corpus,
        title="Archived Page",
        text="# Archived Page\n\nMirrored content.",
        url="https://example.com/article",
        doc_id=make_doc_id("https://example.com/article"),
    )
    response = await resolve("https://example.com/article", catalog=corpus)
    assert response is not None
    assert "Mirrored content." in response.markdown()


# ----------------------------------------------------- fetch integration


async def test_fetch_url_serves_the_archive_in_offline_mode(corpus):
    record = document(corpus, "Retrieval Guide")
    result = await fetch_and_record(
        record.url, max_length=5000, start_index=0, raw=False, ignore_robots_txt=False
    )
    assert result.status_code == 200
    assert "Dense retrievers" in result.content


@respx.mock
async def test_offline_mode_never_touches_the_network(corpus):
    route = respx.get("https://example.com/").mock(return_value=httpx.Response(200, text="hi"))
    with pytest.raises(FetchError, match="offline mode"):
        await fetch_and_record(
            "https://example.com/",
            max_length=500,
            start_index=0,
            raw=False,
            ignore_robots_txt=False,
        )
    assert route.call_count == 0


@respx.mock
async def test_hybrid_mode_falls_through_to_the_network(corpus, monkeypatch):
    monkeypatch.setattr(settings, "net_mode", "hybrid")
    respx.get("https://example.org/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://example.org/page").mock(
        return_value=httpx.Response(200, html="<html><body><p>From the web.</p></body></html>")
    )
    result = await fetch_and_record(
        "https://example.org/page",
        max_length=500,
        start_index=0,
        raw=False,
        ignore_robots_txt=True,
    )
    assert "From the web." in result.content


async def test_online_mode_ignores_the_archive(corpus, monkeypatch):
    """With net_mode=online the corpus must not intercept anything, so the
    original server behaviour is unchanged.

    The archive host is not a real domain, so an un-intercepted request fails
    DNS resolution inside the SSRF check. That failure is the evidence: it can
    only happen if the request went down the network path.
    """
    from mcp_fetch_server.security import SecurityError

    monkeypatch.setattr(settings, "net_mode", "online")
    record = document(corpus, "Retrieval Guide")

    with pytest.raises((SecurityError, FetchError)) as caught:
        await fetch_and_record(
            record.url, max_length=500, start_index=0, raw=False, ignore_robots_txt=True
        )
    assert "local.archive" in str(caught.value)


async def test_archive_host_is_never_resolved_in_offline_mode(corpus, monkeypatch):
    """Serving happens before the SSRF check, so the archive host is answered
    from SQLite and never treated as a real address."""
    import mcp_fetch_server.security as security_module

    def explode(host):
        raise AssertionError(f"DNS resolution attempted for {host}")

    monkeypatch.setattr(security_module, "resolve_host", explode)

    result = await fetch_and_record(
        f"{BASE}/", max_length=500, start_index=0, raw=False, ignore_robots_txt=False
    )
    assert result.status_code == 200


async def test_raw_fetch_returns_html_for_link_extraction(corpus):
    result = await fetch_and_record(
        f"{BASE}/", max_length=100000, start_index=0, raw=True, ignore_robots_txt=False
    )
    assert "<a href=" in result.content
    assert result.content_type == "text/html"


async def test_extract_links_walks_the_archive(corpus):
    from mcp_fetch_server.tools_extra import run_extract_links

    listing = await run_extract_links(f"{BASE}/category/manuals", max_links=20)
    assert "Retrieval Guide" in listing
    assert f"{BASE}/" in listing
