"""Shared document model: what a loader produces and a chunker consumes.

A loader turns a file into a list of :class:`Block` objects that keep the
document's structure (heading levels, page numbers, tables) instead of
flattening it to a wall of text. That structure is what later lets chunks
carry a heading path and a page anchor, which is what makes a citation
checkable.

:meth:`NormalizedDoc.to_markdown` renders the blocks into the canonical text
that gets stored in the blob store, and records where each block landed in
it. Chunk offsets index into that canonical text, so a citation can be
resolved by slicing the stored document.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal

BlockKind = Literal["heading", "paragraph", "list", "table", "code", "caption"]

# Arabic (incl. Persian), Hebrew, CJK, Hangul, Hiragana/Katakana, Thai, Devanagari.
_NON_LATIN_RE = re.compile(
    r"[֐-׿؀-ۿ܀-ݏऀ-ॿ"
    r"฀-๿ᄀ-ᇿ぀-ヿ㐀-䶿一-鿿가-힯]"
)


def estimate_tokens(text: str) -> int:
    """Approximate the token count for a SentencePiece/XLM-R style tokenizer.

    Exact counts would mean shipping a tokenizer, which is a heavy dependency
    for something that only decides chunk boundaries. Latin script runs about
    1.3 tokens per whitespace word; Persian, Arabic and CJK fragment far more
    aggressively, closer to one token per two or three characters, so those
    characters are counted separately.
    """
    if not text:
        return 0
    non_latin = len(_NON_LATIN_RE.findall(text))
    latin_words = len(re.findall(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*", text))
    punctuation = len(re.findall(r"[^\w\s]", text))
    return max(1, int(latin_words * 1.3 + non_latin * 0.5 + punctuation * 0.3))


def slugify(value: str, *, max_length: int = 60) -> str:
    """Make a filesystem- and URL-safe slug, preserving non-Latin scripts.

    Persian and Arabic titles must survive this: transliterating them to
    ASCII would make every such document slug into an unreadable hash.
    """
    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    # Keep word characters from any script; collapse everything else.
    slug = re.sub(r"[^\w؀-ۿ]+", "-", normalized, flags=re.UNICODE)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip("-")
    return slug or "document"


@dataclass(slots=True)
class Block:
    """One structural unit of a document."""

    text: str
    kind: BlockKind = "paragraph"
    level: int | None = None
    page: int | None = None
    # Character span within NormalizedDoc.to_markdown(); filled in by render.
    char_start: int = -1
    char_end: int = -1

    def rendered(self) -> str:
        """Markdown for this block."""
        text = self.text.strip()
        if not text:
            return ""
        if self.kind == "heading":
            level = min(max(self.level or 1, 1), 6)
            return f"{'#' * level} {text}"
        if self.kind == "code":
            return f"```\n{text}\n```"
        if self.kind == "caption":
            return f"*{text}*"
        return text


@dataclass(slots=True)
class NormalizedDoc:
    """A loaded document, before chunking."""

    source_path: str
    doctype: str
    blocks: list[Block] = field(default_factory=list)
    title: str | None = None
    language: str | None = None
    page_count: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    _markdown: str | None = field(default=None, repr=False, compare=False)

    def to_markdown(self) -> str:
        """Render the canonical text and stamp each block's character span.

        Idempotent: repeated calls return the same string and leave the same
        offsets, because chunk offsets and stored blobs must agree exactly.
        """
        if self._markdown is not None:
            return self._markdown

        parts: list[str] = []
        cursor = 0
        for block in self.blocks:
            rendered = block.rendered()
            if not rendered:
                block.char_start = cursor
                block.char_end = cursor
                continue
            block.char_start = cursor
            block.char_end = cursor + len(rendered)
            parts.append(rendered)
            cursor = block.char_end + 2  # the "\n\n" joiner

        self._markdown = "\n\n".join(parts)
        return self._markdown

    @property
    def text_length(self) -> int:
        return len(self.to_markdown())

    def best_title(self) -> str:
        """Title from metadata, else the first heading, else the filename."""
        if self.title and self.title.strip():
            return self.title.strip()
        for block in self.blocks:
            if block.kind == "heading" and block.text.strip():
                return block.text.strip()
        from pathlib import Path

        return Path(self.source_path).stem or "Untitled"


@dataclass(slots=True)
class Chunk:
    """A retrievable passage, addressed by character span in the parent doc."""

    doc_id: str
    chunk_index: int
    text: str
    heading_path: list[str] = field(default_factory=list)
    page_start: int | None = None
    page_end: int | None = None
    char_start: int = 0
    char_end: int = 0
    token_estimate: int = 0

    @property
    def chunk_id(self) -> str:
        return f"{self.doc_id}:{self.chunk_index}"

    @property
    def heading_trail(self) -> str:
        return " > ".join(self.heading_path)

    def embedding_text(self) -> str:
        """Text handed to the embedding model.

        The heading path is prepended so a chunk taken from deep inside a
        document still carries the context that says what it is about; a bare
        paragraph about "the second phase" is close to meaningless on its own.
        """
        if not self.heading_path:
            return self.text
        return f"{self.heading_trail}\n\n{self.text}"
