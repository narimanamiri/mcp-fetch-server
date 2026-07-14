"""Extract structured link/image data out of already-sanitized HTML."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin

from lxml import html as lxml_html

_SKIPPED_HREF_PREFIXES = ("javascript:", "mailto:", "tel:", "#")


@dataclass(slots=True)
class ExtractedLink:
    url: str
    text: str
    kind: str  # "link" or "image"


def extract_links(html_content: str, base_url: str, *, max_links: int = 200) -> list[ExtractedLink]:
    if not html_content.strip():
        return []
    try:
        document = lxml_html.fromstring(html_content)
    except Exception:
        return []

    links: list[ExtractedLink] = []
    seen: set[tuple[str, str]] = set()

    for anchor in document.xpath("//a[@href]"):
        href = (anchor.get("href") or "").strip()
        if not href or href.lower().startswith(_SKIPPED_HREF_PREFIXES):
            continue
        absolute = urljoin(base_url, href)
        key = ("link", absolute)
        if key in seen:
            continue
        seen.add(key)
        text = anchor.text_content().strip() or absolute
        links.append(ExtractedLink(url=absolute, text=text, kind="link"))
        if len(links) >= max_links:
            return links

    for img in document.xpath("//img[@src]"):
        src = (img.get("src") or "").strip()
        if not src or src.lower().startswith("data:"):
            continue
        absolute = urljoin(base_url, src)
        key = ("image", absolute)
        if key in seen:
            continue
        seen.add(key)
        alt = (img.get("alt") or "").strip() or absolute
        links.append(ExtractedLink(url=absolute, text=alt, kind="image"))
        if len(links) >= max_links:
            return links

    return links


def format_links(links: list[ExtractedLink]) -> str:
    if not links:
        return "[No links found]"
    lines = []
    for link in links:
        prefix = "IMG" if link.kind == "image" else "LINK"
        lines.append(f"[{prefix}] {link.text}: {link.url}")
    return "\n".join(lines)
