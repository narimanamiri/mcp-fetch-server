"""Tests for structured link/image extraction."""

from __future__ import annotations

from mcp_fetch_server.links import extract_links, format_links


def test_extract_links_resolves_relative_urls() -> None:
    html = """
    <html><body>
        <a href="/about">About</a>
        <a href="https://other.example/page">Other</a>
        <a href="#section">Skip me</a>
        <a href="javascript:void(0)">Skip me too</a>
        <a href="mailto:test@example.com">Skip mail</a>
    </body></html>
    """
    links = extract_links(html, base_url="https://example.com/")

    urls = {link.url for link in links}
    assert "https://example.com/about" in urls
    assert "https://other.example/page" in urls
    assert not any(u.startswith("javascript:") for u in urls)
    assert not any(u.startswith("mailto:") for u in urls)
    assert len(links) == 2


def test_extract_links_includes_images() -> None:
    html = '<html><body><img src="/logo.png" alt="Logo"></body></html>'
    links = extract_links(html, base_url="https://example.com/")

    assert len(links) == 1
    assert links[0].kind == "image"
    assert links[0].url == "https://example.com/logo.png"
    assert links[0].text == "Logo"


def test_extract_links_skips_data_uri_images() -> None:
    html = '<html><body><img src="data:image/png;base64,AAAA"></body></html>'
    links = extract_links(html, base_url="https://example.com/")
    assert links == []


def test_extract_links_deduplicates() -> None:
    html = """
    <html><body>
        <a href="/about">About us</a>
        <a href="/about">About</a>
    </body></html>
    """
    links = extract_links(html, base_url="https://example.com/")
    assert len(links) == 1


def test_extract_links_respects_max_links() -> None:
    anchors = "".join(f'<a href="/p{i}">p{i}</a>' for i in range(10))
    html = f"<html><body>{anchors}</body></html>"
    links = extract_links(html, base_url="https://example.com/", max_links=3)
    assert len(links) == 3


def test_extract_links_empty_input() -> None:
    assert extract_links("", base_url="https://example.com/") == []
    assert extract_links("   ", base_url="https://example.com/") == []


def test_format_links_empty() -> None:
    assert format_links([]) == "[No links found]"


def test_format_links_marks_kind() -> None:
    links = extract_links(
        '<a href="/a">A</a><img src="/b.png" alt="B">',
        base_url="https://example.com/",
    )
    formatted = format_links(links)
    assert "[LINK]" in formatted
    assert "[IMG]" in formatted
