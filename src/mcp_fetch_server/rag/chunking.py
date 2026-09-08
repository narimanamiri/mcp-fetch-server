"""Structure-aware chunking.

Splitting on a fixed character count is the single easiest way to ruin
retrieval: it cuts sentences in half, strips headings away from the text they
describe, and produces passages that cannot be cited. This chunker instead:

* packs whole blocks together up to a token budget, so paragraphs stay intact;
* starts a new chunk at a heading of the same or higher level, so a chunk
  never straddles two unrelated sections;
* tracks the heading path, which is prepended to the embedded text;
* splits oversized blocks on sentence boundaries rather than mid-word;
* overlaps consecutive chunks so a fact sitting on a boundary is retrievable
  from either side;
* records the character span in the canonical document, so every passage can
  be resolved back to its source text.
"""

from __future__ import annotations

import re

from mcp_fetch_server.config import settings
from mcp_fetch_server.rag.documents import Block, Chunk, NormalizedDoc, estimate_tokens

# Sentence-ish boundaries for Latin, Persian/Arabic (، ؛ ؟) and CJK (。！？).
_SENTENCE_END_RE = re.compile(r"(?<=[.!?؟。！？;؛])\s+|\n+")

# Below this, a fragment is merged into a neighbour rather than left as its own
# chunk: a 20-token orphan retrieves badly and pollutes the result list.
_MIN_CHUNK_TOKENS = 48

# A merge may overshoot the budget by this factor. Slightly oversized beats a
# scattering of orphans, but not without limit.
_MERGE_MAX_RATIO = 1.4


def _split_sentences(text: str) -> list[str]:
    parts = [part.strip() for part in _SENTENCE_END_RE.split(text) if part and part.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def _split_oversized(block: Block, budget: int) -> list[Block]:
    """Break one over-budget block into sentence-aligned sub-blocks.

    Character offsets are recomputed by searching forward within the block, so
    the sub-blocks still point at real spans of the canonical document.
    """
    sentences = _split_sentences(block.text)
    if len(sentences) <= 1:
        return _split_hard(block, budget)

    pieces: list[Block] = []
    current: list[str] = []
    current_tokens = 0
    search_from = block.char_start

    def flush() -> None:
        nonlocal current, current_tokens, search_from
        if not current:
            return
        text = " ".join(current)
        start = block.text.find(current[0], search_from - block.char_start)
        start = block.char_start + (start if start >= 0 else 0)
        pieces.append(
            Block(
                text=text,
                kind=block.kind,
                level=block.level,
                page=block.page,
                char_start=start,
                char_end=min(start + len(text), block.char_end),
            )
        )
        search_from = pieces[-1].char_end
        current = []
        current_tokens = 0

    for sentence in sentences:
        tokens = estimate_tokens(sentence)
        if current and current_tokens + tokens > budget:
            flush()
        if tokens > budget:
            flush()
            single = Block(
                text=sentence,
                kind=block.kind,
                level=block.level,
                page=block.page,
                char_start=block.char_start,
                char_end=block.char_end,
            )
            pieces.extend(_split_hard(single, budget))
            continue
        current.append(sentence)
        current_tokens += tokens

    flush()
    return pieces or [block]


def _split_hard(block: Block, budget: int) -> list[Block]:
    """Last resort for text with no usable boundaries: split on whitespace."""
    words = block.text.split()
    if not words:
        return [block]

    pieces: list[Block] = []
    current: list[str] = []
    offset = block.char_start

    for word in words:
        candidate = [*current, word]
        if current and estimate_tokens(" ".join(candidate)) > budget:
            text = " ".join(current)
            pieces.append(
                Block(
                    text=text,
                    kind=block.kind,
                    level=block.level,
                    page=block.page,
                    char_start=offset,
                    char_end=offset + len(text),
                )
            )
            offset += len(text) + 1
            current = [word]
        else:
            current = candidate

    if current:
        text = " ".join(current)
        pieces.append(
            Block(
                text=text,
                kind=block.kind,
                level=block.level,
                page=block.page,
                char_start=offset,
                char_end=min(offset + len(text), block.char_end),
            )
        )
    return pieces


def _overlap_text(text: str, overlap_tokens: int) -> str:
    """Tail of ``text`` worth roughly ``overlap_tokens`` tokens, sentence-aligned."""
    if overlap_tokens <= 0 or not text:
        return ""
    sentences = _split_sentences(text)
    tail: list[str] = []
    total = 0
    for sentence in reversed(sentences):
        tokens = estimate_tokens(sentence)
        if total + tokens > overlap_tokens and tail:
            break
        tail.insert(0, sentence)
        total += tokens
    return " ".join(tail)


def chunk_document(
    doc: NormalizedDoc,
    doc_id: str,
    *,
    target_tokens: int | None = None,
    overlap_ratio: float | None = None,
    section_break_level: int | None = None,
) -> list[Chunk]:
    """Split a loaded document into overlapping, heading-aware chunks."""
    budget = target_tokens or settings.chunk_target_tokens
    budget = max(64, budget)
    ratio = overlap_ratio if overlap_ratio is not None else settings.chunk_overlap_ratio
    ratio = min(max(ratio, 0.0), 0.5)
    overlap_tokens = int(budget * ratio)
    break_level = (
        section_break_level
        if section_break_level is not None
        else settings.chunk_section_break_level
    )

    doc.to_markdown()  # stamps char spans on every block

    chunks: list[Chunk] = []
    heading_stack: list[tuple[int, str]] = []

    pending: list[Block] = []
    pending_tokens = 0
    pending_headings: list[str] = []
    carry_over = ""

    def current_headings() -> list[str]:
        return [text for _, text in heading_stack]

    def flush() -> None:
        nonlocal pending, pending_tokens, pending_headings, carry_over
        if not pending:
            return

        # A chunk holding nothing but headings is dead weight: it matches the
        # section title and then has no content to return. Keep the headings
        # pending so they lead the next real chunk instead.
        if all(block.kind == "heading" for block in pending):
            return

        body = "\n\n".join(block.rendered() for block in pending if block.rendered())
        if not body.strip():
            pending = []
            pending_tokens = 0
            return

        text = f"{carry_over}\n\n{body}".strip() if carry_over else body
        pages = [block.page for block in pending if block.page is not None]

        chunks.append(
            Chunk(
                doc_id=doc_id,
                chunk_index=len(chunks),
                text=text,
                heading_path=list(pending_headings),
                page_start=min(pages) if pages else None,
                page_end=max(pages) if pages else None,
                char_start=pending[0].char_start,
                char_end=pending[-1].char_end,
                token_estimate=estimate_tokens(text),
            )
        )
        carry_over = _overlap_text(body, overlap_tokens)
        pending = []
        pending_tokens = 0
        pending_headings = current_headings()

    for block in doc.blocks:
        if not block.text.strip():
            continue

        if block.kind == "heading":
            level = min(max(block.level or 1, 1), 6)
            heading_tokens = estimate_tokens(block.text)
            # A major heading starts a new topic, so it always closes the
            # current chunk. A deeper subsection belongs to the same topic and
            # only forces a break when the budget is already spent; otherwise a
            # document of many short "###" sections becomes a pile of tiny,
            # context-free chunks.
            is_major = level <= break_level
            if pending and (is_major or pending_tokens + heading_tokens > budget):
                flush()
            # Anything still pending is a heading whose section turned out to be
            # empty. It contributes no text, and its title is already in the
            # heading path, so drop it rather than stacking titles.
            if pending and all(item.kind == "heading" for item in pending):
                pending = []
                pending_tokens = 0

            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, block.text.strip()))
            if not pending:
                # The heading path is the one in force where the chunk starts.
                pending_headings = current_headings()
                if is_major:
                    carry_over = ""
            # The heading itself leads the chunk, giving it a visible title.
            pending.append(block)
            pending_tokens += heading_tokens
            continue

        if not pending:
            pending_headings = current_headings()

        tokens = estimate_tokens(block.text)

        if tokens > budget:
            flush()
            # Carry any pending heading onto the first piece so the split
            # section keeps its title instead of orphaning it.
            lead = [b for b in pending if b.kind == "heading"]
            pending = []
            pending_tokens = 0
            for index, piece in enumerate(_split_oversized(block, budget)):
                pending = [*lead, piece] if index == 0 else [piece]
                pending_tokens = estimate_tokens(piece.text)
                pending_headings = current_headings()
                flush()
            continue

        if pending_tokens + tokens > budget:
            flush()
            pending_headings = current_headings()

        pending.append(block)
        pending_tokens += tokens

    flush()
    return _merge_orphans(chunks, budget)


def _absorb(target: Chunk, other: Chunk) -> None:
    """Fold ``other`` into ``target``, which must precede it."""
    target.text = f"{target.text}\n\n{other.text}"
    target.char_start = min(target.char_start, other.char_start)
    target.char_end = max(target.char_end, other.char_end)
    target.token_estimate = estimate_tokens(target.text)
    pages = [p for p in (target.page_start, other.page_start) if p is not None]
    if pages:
        target.page_start = min(pages)
    pages = [p for p in (target.page_end, other.page_end) if p is not None]
    if pages:
        target.page_end = max(pages)


def _same_section(first: list[str], second: list[str]) -> bool:
    """True when one heading path sits inside the other's section.

    ``["Guide"]`` and ``["Guide", "Install"]`` are the same topic, so an
    undersized subsection may fold into its parent. ``["Guide"]`` and
    ``["Reference"]`` are not, and must stay apart.
    """
    shorter, longer = sorted((first, second), key=len)
    return longer[: len(shorter)] == shorter


def _merge_orphans(chunks: list[Chunk], budget: int) -> list[Chunk]:
    """Fold undersized chunks into a neighbour from the same section.

    Splitting an oversized block leaves a short remainder, and a short section
    yields a short chunk. Either way the orphan competes for a slot in the
    result list while carrying almost no information, so it is merged with the
    previous chunk of the same section where that does not blow the budget.
    """
    if len(chunks) < 2:
        return chunks

    ceiling = int(budget * _MERGE_MAX_RATIO)
    merged: list[Chunk] = [chunks[0]]

    for chunk in chunks[1:]:
        previous = merged[-1]
        too_small = chunk.token_estimate < _MIN_CHUNK_TOKENS
        fits = previous.token_estimate + chunk.token_estimate <= ceiling
        if too_small and fits and _same_section(previous.heading_path, chunk.heading_path):
            _absorb(previous, chunk)
            continue
        merged.append(chunk)

    for index, chunk in enumerate(merged):
        chunk.chunk_index = index
    return merged
