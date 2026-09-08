"""Tests for structure-aware chunking and the shared document model."""

from __future__ import annotations

from mcp_fetch_server.rag.chunking import (
    _overlap_text,
    _same_section,
    _split_sentences,
    chunk_document,
)
from mcp_fetch_server.rag.documents import (
    Block,
    Chunk,
    NormalizedDoc,
    estimate_tokens,
    slugify,
)


def doc(*blocks: Block, **kwargs) -> NormalizedDoc:
    defaults = {"source_path": "test.md", "doctype": "markdown"}
    defaults.update(kwargs)
    return NormalizedDoc(blocks=list(blocks), **defaults)


def para(text: str, page: int | None = None) -> Block:
    return Block(text=text, kind="paragraph", page=page)


def heading(text: str, level: int = 1, page: int | None = None) -> Block:
    return Block(text=text, kind="heading", level=level, page=page)


def words(count: int, token: str = "alpha") -> str:
    return " ".join(f"{token}{i}" for i in range(count))


# ------------------------------------------------------- token estimation


def test_estimate_tokens_scales_with_length():
    assert estimate_tokens("") == 0
    short = estimate_tokens("one two three")
    long = estimate_tokens(words(100))
    assert short < long


def test_estimate_tokens_counts_persian_more_densely_than_latin():
    """Persian fragments harder under XLM-R, so equal character counts must
    not be treated as equal token counts."""
    persian = "بازیابی اطلاعات چندزبانه برای مدل های زبانی بزرگ"
    latin = "multilingual information retrieval for large language models"
    assert estimate_tokens(persian) > estimate_tokens(latin) * 0.8


# --------------------------------------------------------------- slugify


def test_slugify_ascii():
    assert slugify("Annual Report 2024!") == "annual-report-2024"


def test_slugify_preserves_persian():
    slug = slugify("گزارش سالانه")
    assert "گزارش" in slug
    assert " " not in slug


def test_slugify_falls_back_and_truncates():
    assert slugify("!!!") == "document"
    assert len(slugify(words(80))) <= 60


# ------------------------------------------------------ canonical render


def test_to_markdown_is_stable_and_stamps_offsets():
    document = doc(heading("Title"), para("Body text."))
    first = document.to_markdown()
    assert document.to_markdown() is first  # cached, offsets stay valid
    assert first == "# Title\n\nBody text."

    title_block, body_block = document.blocks
    assert first[title_block.char_start : title_block.char_end] == "# Title"
    assert first[body_block.char_start : body_block.char_end] == "Body text."


def test_best_title_prefers_metadata_then_heading_then_filename():
    assert doc(heading("From heading"), title="From metadata").best_title() == "From metadata"
    assert doc(heading("From heading")).best_title() == "From heading"
    assert doc(para("no heading"), source_path="/tmp/report.pdf").best_title() == "report"


# --------------------------------------------------------- basic chunking


def test_empty_document_produces_no_chunks():
    assert chunk_document(doc(), "d1") == []
    assert chunk_document(doc(para("   ")), "d1") == []


def test_short_document_is_one_chunk():
    chunks = chunk_document(doc(heading("Intro"), para("Short body.")), "d1")
    assert len(chunks) == 1
    assert chunks[0].chunk_id == "d1:0"
    assert chunks[0].heading_path == ["Intro"]


def test_chunks_respect_the_token_budget():
    blocks = [para(words(40)) for _ in range(20)]
    chunks = chunk_document(doc(*blocks), "d1", target_tokens=100, overlap_ratio=0.0)
    assert len(chunks) > 1
    # Allow slack for the overlap-free packing heuristic, but nothing wild.
    assert all(chunk.token_estimate <= 160 for chunk in chunks)


def test_chunk_indexes_are_sequential():
    blocks = [para(words(40)) for _ in range(12)]
    chunks = chunk_document(doc(*blocks), "d1", target_tokens=100, overlap_ratio=0.0)
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))


# ------------------------------------------------------- heading handling


def test_new_section_starts_a_new_chunk():
    chunks = chunk_document(
        doc(heading("A"), para("Alpha body."), heading("B"), para("Beta body.")),
        "d1",
        target_tokens=2000,
    )
    assert len(chunks) == 2
    assert chunks[0].heading_path == ["A"]
    assert chunks[1].heading_path == ["B"]
    assert "Beta" not in chunks[0].text


def test_heading_path_nests_and_pops():
    chunks = chunk_document(
        doc(
            heading("Chapter", 1),
            heading("Section", 2),
            para("Nested body."),
            heading("Next chapter", 1),
            para("Other body."),
        ),
        "d1",
        target_tokens=2000,
    )
    nested = next(c for c in chunks if "Nested" in c.text)
    other = next(c for c in chunks if "Other" in c.text)
    assert nested.heading_path == ["Chapter", "Section"]
    assert other.heading_path == ["Next chapter"]


def test_heading_leads_its_chunk():
    chunks = chunk_document(doc(heading("Findings"), para("Body.")), "d1")
    assert chunks[0].text.startswith("# Findings")


def test_embedding_text_prepends_heading_trail():
    chunk = Chunk(doc_id="d", chunk_index=0, text="Body", heading_path=["A", "B"])
    assert chunk.embedding_text() == "A > B\n\nBody"
    assert Chunk(doc_id="d", chunk_index=0, text="Body").embedding_text() == "Body"


# ---------------------------------------------------------------- overlap


def test_overlap_repeats_tail_of_previous_chunk():
    blocks = [para(f"Sentence {i} about retrieval systems and indexes." * 4) for i in range(8)]
    chunks = chunk_document(doc(*blocks), "d1", target_tokens=120, overlap_ratio=0.3)
    assert len(chunks) > 1
    overlapping = sum(
        1
        for previous, following in zip(chunks, chunks[1:], strict=False)
        if any(
            fragment and fragment in following.text
            for fragment in previous.text.split(".")[-3:-1]
        )
    )
    assert overlapping >= 1


def test_zero_overlap_ratio_produces_no_carry_over():
    blocks = [para(words(60, f"w{i}_")) for i in range(6)]
    chunks = chunk_document(doc(*blocks), "d1", target_tokens=100, overlap_ratio=0.0)
    for previous, following in zip(chunks, chunks[1:], strict=False):
        assert previous.text.split()[0] not in following.text


def test_overlap_text_returns_sentence_aligned_tail():
    text = "First sentence here. Second sentence here. Third sentence here."
    tail = _overlap_text(text, 12)
    assert tail
    assert text.endswith(tail.split(".")[-2].strip() + ".")
    assert _overlap_text(text, 0) == ""


# -------------------------------------------------------- oversized blocks


def test_oversized_block_splits_on_sentences():
    sentences = " ".join(f"This is sentence number {i} in a long paragraph." for i in range(60))
    chunks = chunk_document(doc(para(sentences)), "d1", target_tokens=100, overlap_ratio=0.0)
    assert len(chunks) > 1
    # No chunk should end mid-word.
    for chunk in chunks:
        assert not chunk.text.rstrip().endswith("sentenc")


def test_block_with_no_sentence_boundaries_still_splits():
    chunks = chunk_document(doc(para(words(500))), "d1", target_tokens=100, overlap_ratio=0.0)
    assert len(chunks) > 1
    assert all(chunk.text.strip() for chunk in chunks)


def test_split_sentences_handles_persian_punctuation():
    parts = _split_sentences("جمله اول است؟ جمله دوم است. جمله سوم")
    assert len(parts) == 3


# ------------------------------------------------------------ page anchors


def test_pages_are_tracked_across_a_chunk():
    chunks = chunk_document(
        doc(para("A", page=3), para("B", page=4), para("C", page=5)),
        "d1",
        target_tokens=2000,
    )
    assert chunks[0].page_start == 3
    assert chunks[0].page_end == 5


def test_pages_absent_when_document_has_none():
    chunks = chunk_document(doc(para("A")), "d1")
    assert chunks[0].page_start is None


# ------------------------------------------------------------ citations


def test_char_spans_resolve_against_the_canonical_text():
    document = doc(heading("Title"), para(words(80)), para(words(80)))
    markdown = document.to_markdown()
    chunks = chunk_document(document, "d1", target_tokens=60, overlap_ratio=0.0)

    for chunk in chunks:
        assert 0 <= chunk.char_start <= chunk.char_end <= len(markdown)
        # The span must contain real document text, not drift off the end.
        assert markdown[chunk.char_start : chunk.char_end].strip()


def test_spans_are_monotonic():
    blocks = [para(words(50)) for _ in range(10)]
    chunks = chunk_document(doc(*blocks), "d1", target_tokens=100, overlap_ratio=0.0)
    starts = [chunk.char_start for chunk in chunks]
    assert starts == sorted(starts)


# ------------------------------------------------------------- tail merge


def test_subsections_pack_together_until_the_budget():
    """A document of many short "###" sections must not become a pile of
    tiny, context-free chunks."""
    blocks = [heading("Guide", 1)]
    for index in range(6):
        blocks.append(heading(f"Step {index}", 3))
        blocks.append(para(f"Short instruction number {index}."))

    chunks = chunk_document(doc(*blocks), "d1", target_tokens=600, section_break_level=2)
    assert len(chunks) == 1
    assert all(f"Step {index}" in chunks[0].text for index in range(6))


def test_major_headings_always_break_even_when_small():
    blocks = []
    for name in ("Alpha", "Beta", "Gamma"):
        blocks.append(heading(name, 2))
        blocks.append(para(f"Body for {name}."))

    chunks = chunk_document(doc(*blocks), "d1", target_tokens=600, section_break_level=2)
    assert len(chunks) == 3
    assert [chunk.heading_path for chunk in chunks] == [["Alpha"], ["Beta"], ["Gamma"]]


def test_packed_subsections_keep_the_path_from_the_chunk_start():
    chunks = chunk_document(
        doc(
            heading("Guide", 1),
            heading("First", 3),
            para("Body one."),
            heading("Second", 3),
            para("Body two."),
        ),
        "d1",
        target_tokens=600,
        section_break_level=2,
    )
    assert len(chunks) == 1
    assert chunks[0].heading_path == ["Guide", "First"]
    # The later subsection heading is still visible in the text.
    assert "### Second" in chunks[0].text


def test_subsections_still_break_when_the_budget_is_reached():
    blocks = [heading("Guide", 1)]
    for index in range(6):
        blocks.append(heading(f"Step {index}", 3))
        blocks.append(para(words(60)))

    chunks = chunk_document(doc(*blocks), "d1", target_tokens=120, section_break_level=2)
    assert len(chunks) > 1


def test_section_break_level_one_breaks_only_at_top_level():
    chunks = chunk_document(
        doc(heading("A", 1), para("Body a."), heading("B", 2), para("Body b.")),
        "d1",
        target_tokens=600,
        section_break_level=1,
    )
    assert len(chunks) == 1


def test_same_section_helper():
    assert _same_section(["Guide"], ["Guide", "Install"])
    assert _same_section(["Guide", "Install"], ["Guide"])
    assert _same_section([], ["Anything"])
    assert not _same_section(["Guide"], ["Reference"])
    assert not _same_section(["Guide", "Install"], ["Guide2", "Install"])


def test_no_heading_only_chunks_when_section_body_is_oversized():
    """Regression: an oversized body used to flush the pending heading on its
    own, producing a 3-token chunk that matched the section title and then had
    nothing to return."""
    chunks = chunk_document(
        doc(heading("Dense Retrieval", 2), para(words(400))),
        "d1",
        target_tokens=120,
    )
    for chunk in chunks:
        stripped = chunk.text.replace("#", "").strip()
        assert stripped != "Dense Retrieval"
        assert chunk.token_estimate >= 10
    # The heading must lead the first piece, not vanish.
    assert chunks[0].text.startswith("## Dense Retrieval")


def test_heading_with_empty_section_is_dropped_from_text():
    """A heading whose section has no body contributes no content; its title
    already lives in the heading path of what follows."""
    chunks = chunk_document(
        doc(heading("Empty", 2), heading("Real", 1), para("Body.")),
        "d1",
    )
    assert len(chunks) == 1
    assert "Empty" not in chunks[0].text
    assert chunks[0].heading_path == ["Real"]


def test_orphans_are_merged_mid_document_not_only_at_the_end():
    """Regression: only the final chunk used to be merge-eligible, so split
    remainders in the middle of a document stayed as orphans."""
    chunks = chunk_document(
        doc(
            heading("A", 1),
            para(words(300)),
            heading("B", 1),
            para(words(300)),
            heading("C", 1),
            para(words(300)),
        ),
        "d1",
        target_tokens=120,
        overlap_ratio=0.0,
    )
    assert all(chunk.token_estimate >= 48 for chunk in chunks[:-1])


def test_merge_does_not_cross_section_boundaries():
    chunks = chunk_document(
        doc(heading("A"), para(words(200)), heading("B"), para("Tiny tail.")),
        "d1",
        target_tokens=100,
        overlap_ratio=0.0,
    )
    tail = chunks[-1]
    assert tail.heading_path == ["B"]
    assert "alpha" not in tail.text


def test_merge_respects_the_budget_ceiling():
    chunks = chunk_document(
        doc(*[para(words(45)) for _ in range(12)]),
        "d1",
        target_tokens=100,
        overlap_ratio=0.0,
    )
    assert all(chunk.token_estimate <= 140 for chunk in chunks)


def test_tiny_trailing_chunk_is_merged_back():
    blocks = [para(words(60)) for _ in range(4)]
    blocks.append(para("Tiny."))
    chunks = chunk_document(doc(*blocks), "d1", target_tokens=100, overlap_ratio=0.0)
    assert chunks[-1].token_estimate >= 48 or len(chunks) == 1
    assert "Tiny." in chunks[-1].text


def test_tiny_tail_kept_when_it_belongs_to_another_section():
    chunks = chunk_document(
        doc(heading("A"), para(words(200)), heading("B"), para("Tiny.")),
        "d1",
        target_tokens=100,
        overlap_ratio=0.0,
    )
    assert chunks[-1].heading_path == ["B"]
