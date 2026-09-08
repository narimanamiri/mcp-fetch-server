"""Tests for the document loaders.

Real files are generated where the library can write them (docx, pptx, epub,
pdf), so the loaders are exercised end to end rather than against mocks.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from mcp_fetch_server.rag.loaders import (
    EXTENSION_DOCTYPES,
    LoaderError,
    UnsupportedFormatError,
    _looks_like_heading,
    _render_table,
    _repair_pdf_linebreaks,
    blocks_from_html,
    detect_language,
    is_supported,
    load_document,
)


def kinds(document) -> list[str]:
    return [block.kind for block in document.blocks]


def texts(document) -> list[str]:
    return [block.text for block in document.blocks]


# ----------------------------------------------------------- language


def test_detect_language_english():
    assert detect_language("The quick brown fox jumps over the lazy dog.") == "en"


def test_detect_language_persian_distinguished_from_arabic():
    assert detect_language("این یک متن فارسی است که پژوهش را توضیح می دهد") == "fa"
    assert detect_language("هذا نص عربي يوضح البحث والدراسة") == "ar"


def test_detect_language_handles_empty_and_symbolic():
    assert detect_language("") == "und"
    assert detect_language("!!! ??? ...") == "und"


# ------------------------------------------------------------ headings


@pytest.mark.parametrize(
    "line",
    ["1. Introduction", "2.3 Related Work", "EXPERIMENTAL RESULTS", "Experimental Results"],
)
def test_heading_heuristic_accepts(line):
    assert _looks_like_heading(line) is not None


@pytest.mark.parametrize(
    "line",
    [
        "This sentence ends with a period.",
        "a lowercase fragment of prose",
        "x" * 120,
        "This is a long line of ordinary prose that simply runs on and on without stopping yet",
    ],
)
def test_heading_heuristic_rejects(line):
    assert _looks_like_heading(line) is None


# ---------------------------------------------------------------- text


def test_load_text_splits_paragraphs_and_finds_headings(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text(
        "1. Introduction\n\nThis is the opening paragraph of the document.\n\n"
        "2. Method\n\nWe describe the method here in prose.",
        encoding="utf-8",
    )
    document = load_document(path)
    assert kinds(document) == ["heading", "paragraph", "heading", "paragraph"]
    assert document.language == "en"


def test_wrapped_lines_join_into_one_paragraph(tmp_path):
    path = tmp_path / "wrapped.txt"
    path.write_text("first line\nsecond line\nthird line", encoding="utf-8")
    document = load_document(path)
    assert len(document.blocks) == 1
    assert document.blocks[0].text == "first line second line third line"


def test_non_utf8_encoding_falls_back(tmp_path):
    """Legacy Arabic-codepage files must load rather than raise."""
    path = tmp_path / "cp1256.txt"
    # Arabic yeh (U+064A), not Farsi yeh: cp1256 cannot encode the latter.
    path.write_bytes("هذا نص عربي".encode("cp1256"))
    document = load_document(path)
    assert document.blocks
    assert document.language in {"ar", "fa"}


# ------------------------------------------------------------ markdown


def test_load_markdown_keeps_headings_lists_and_code(tmp_path):
    path = tmp_path / "doc.md"
    path.write_text(
        "# Title\n\nIntro paragraph.\n\n## Section\n\n- first item\n- second item\n\n"
        "```python\nprint('hi')\n```\n\nClosing paragraph.\n",
        encoding="utf-8",
    )
    document = load_document(path)
    assert kinds(document) == [
        "heading",
        "paragraph",
        "heading",
        "list",
        "list",
        "code",
        "paragraph",
    ]
    assert document.blocks[0].level == 1
    assert document.blocks[2].level == 2
    assert "print('hi')" in texts(document)


def test_markdown_fenced_block_is_not_parsed_as_headings(tmp_path):
    path = tmp_path / "fence.md"
    path.write_text("```\n# not a heading\n```\n", encoding="utf-8")
    document = load_document(path)
    assert kinds(document) == ["code"]


def test_unterminated_fence_still_yields_content(tmp_path):
    path = tmp_path / "open.md"
    path.write_text("```\nunclosed code\n", encoding="utf-8")
    document = load_document(path)
    assert "unclosed code" in texts(document)


# ---------------------------------------------------------------- html


def test_blocks_from_html_extracts_structure():
    blocks, title = blocks_from_html(
        "<html><head><title>Doc</title></head><body>"
        "<h1>Main</h1><p>Body text.</p><h2>Sub</h2><ul><li>one</li></ul>"
        "<pre>code()</pre>"
        "<table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table>"
        "</body></html>"
    )
    assert title == "Doc"
    assert [block.kind for block in blocks] == [
        "heading",
        "paragraph",
        "heading",
        "list",
        "code",
        "table",
    ]
    assert blocks[0].level == 1
    assert "a | b" in blocks[5].text


def test_html_script_and_style_are_stripped():
    blocks, _ = blocks_from_html(
        "<body><script>alert(1)</script><style>p{}</style><p>Real text.</p></body>"
    )
    joined = " ".join(block.text for block in blocks)
    assert "alert" not in joined
    assert "Real text." in joined


def test_load_html_file(tmp_path):
    path = tmp_path / "page.html"
    path.write_text("<html><body><h1>Header</h1><p>Content here.</p></body></html>", "utf-8")
    document = load_document(path)
    assert document.doctype == "html"
    assert document.blocks[0].kind == "heading"


# ---------------------------------------------------------------- docx


def test_load_docx_maps_styles_to_structure(tmp_path):
    docx = pytest.importorskip("docx")
    path = tmp_path / "report.docx"

    document = docx.Document()
    document.core_properties.title = "Quarterly Report"
    document.add_heading("Overview", level=1)
    document.add_paragraph("This quarter revenue grew.")
    document.add_heading("Details", level=2)
    document.add_paragraph("Bullet one", style="List Bullet")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Metric"
    table.cell(0, 1).text = "Value"
    table.cell(1, 0).text = "Revenue"
    table.cell(1, 1).text = "42"
    document.save(str(path))

    loaded = load_document(path)
    assert loaded.title == "Quarterly Report"
    assert loaded.doctype == "docx"
    assert "heading" in kinds(loaded)
    assert "list" in kinds(loaded)
    assert "table" in kinds(loaded)
    heading = next(b for b in loaded.blocks if b.kind == "heading")
    assert heading.level == 1
    table_block = next(b for b in loaded.blocks if b.kind == "table")
    assert "Metric | Value" in table_block.text


def test_docx_with_no_text_is_rejected(tmp_path):
    docx = pytest.importorskip("docx")
    path = tmp_path / "empty.docx"
    docx.Document().save(str(path))
    with pytest.raises(LoaderError, match="no readable text"):
        load_document(path)


# ---------------------------------------------------------------- pptx


def test_load_pptx_uses_slide_numbers_as_pages(tmp_path):
    pptx = pytest.importorskip("pptx")
    path = tmp_path / "deck.pptx"

    presentation = pptx.Presentation()
    layout = presentation.slide_layouts[1]
    for index, (title, body) in enumerate(
        [("First Slide", "Point one"), ("Second Slide", "Point two")], start=1
    ):
        slide = presentation.slides.add_slide(layout)
        slide.shapes.title.text = title
        slide.placeholders[1].text = body
        assert index
    presentation.save(str(path))

    document = load_document(path)
    assert document.page_count == 2
    headings = [b for b in document.blocks if b.kind == "heading"]
    assert [b.text for b in headings] == ["First Slide", "Second Slide"]
    assert [b.page for b in headings] == [1, 2]
    assert any("Point two" in b.text for b in document.blocks if b.page == 2)


# ----------------------------------------------------------------- pdf


def _write_pdf(path: Path, pages: list[str]) -> None:
    """Build a small real PDF using pypdf plus a hand-written content stream."""
    pypdf = pytest.importorskip("pypdf")
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    def dictionary(**entries) -> DictionaryObject:
        return DictionaryObject({NameObject(k): v for k, v in entries.items()})

    writer = pypdf.PdfWriter()
    font = writer._add_object(
        dictionary(
            **{
                "/Type": NameObject("/Font"),
                "/Subtype": NameObject("/Type1"),
                "/BaseFont": NameObject("/Helvetica"),
            }
        )
    )

    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        commands = ["BT", "/F1 12 Tf", "72 720 Td", "14 TL"]
        for line in text.split("\n"):
            escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            commands.append(f"({escaped}) Tj T*")
        commands.append("ET")

        stream = DecodedStreamObject()
        stream.set_data("\n".join(commands).encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
        page[NameObject("/Resources")] = dictionary(
            **{"/Font": dictionary(**{"/F1": font})}
        )

    with path.open("wb") as handle:
        writer.write(handle)


def test_load_pdf_extracts_text_with_page_numbers(tmp_path):
    path = tmp_path / "paper.pdf"
    _write_pdf(
        path,
        [
            "1. Introduction\n\nRetrieval augmented generation is useful.",
            "2. Method\n\nWe embed passages and rank them.",
        ],
    )
    document = load_document(path)
    assert document.doctype == "pdf"
    assert document.page_count == 2
    pages = {block.page for block in document.blocks}
    assert pages == {1, 2}
    joined = " ".join(texts(document))
    assert "Retrieval augmented" in joined
    assert "embed passages" in joined


def test_scanned_pdf_is_rejected_with_an_ocr_hint(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    path = tmp_path / "scan.pdf"
    writer = pypdf.PdfWriter()
    for _ in range(5):
        writer.add_blank_page(width=612, height=792)
    with path.open("wb") as handle:
        writer.write(handle)

    with pytest.raises(LoaderError, match="ocrmypdf"):
        load_document(path)


def test_repair_pdf_linebreaks_rejoins_hyphens_and_wraps():
    assert _repair_pdf_linebreaks("hyphen-\nated") == "hyphenated"
    assert _repair_pdf_linebreaks("one\ntwo") == "one two"
    assert "\n\n" in _repair_pdf_linebreaks("para one\n\npara two")


# ---------------------------------------------------------------- epub


def _write_epub(path: Path) -> None:
    container = (
        '<?xml version="1.0"?>'
        '<container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    opf = (
        '<?xml version="1.0"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<dc:title>Test Book</dc:title></metadata>"
        '<manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="c2" href="ch2.xhtml" media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr(
            "OEBPS/ch1.xhtml",
            "<html><body><h1>Chapter One</h1><p>Opening chapter text.</p></body></html>",
        )
        archive.writestr(
            "OEBPS/ch2.xhtml",
            "<html><body><h1>Chapter Two</h1><p>Second chapter text.</p></body></html>",
        )


def test_load_epub_reads_spine_in_order(tmp_path):
    path = tmp_path / "book.epub"
    _write_epub(path)
    document = load_document(path)

    assert document.title == "Test Book"
    assert document.page_count == 2
    headings = [b.text for b in document.blocks if b.kind == "heading"]
    assert headings == ["Chapter One", "Chapter Two"]
    # Section order must follow the spine, not zip order.
    assert [b.page for b in document.blocks if b.kind == "heading"] == [1, 2]


def test_epub_without_container_is_rejected(tmp_path):
    path = tmp_path / "broken.epub"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
    with pytest.raises(LoaderError, match="container.xml"):
        load_document(path)


def test_corrupt_epub_is_rejected(tmp_path):
    path = tmp_path / "corrupt.epub"
    path.write_bytes(b"not a zip file at all")
    with pytest.raises(LoaderError, match="Could not open"):
        load_document(path)


# ------------------------------------------------------------- registry


def test_is_supported_matches_the_extension_table():
    assert is_supported(Path("a.pdf"))
    assert is_supported(Path("A.PDF"))
    assert not is_supported(Path("a.xlsx"))
    assert set(EXTENSION_DOCTYPES) == {ext for ext in EXTENSION_DOCTYPES}


def test_unsupported_extension_raises(tmp_path):
    path = tmp_path / "sheet.xlsx"
    path.write_text("nope", encoding="utf-8")
    with pytest.raises(UnsupportedFormatError, match="No loader"):
        load_document(path)


def test_missing_file_raises(tmp_path):
    with pytest.raises(LoaderError, match="Not a file"):
        load_document(tmp_path / "absent.txt")


def test_loader_records_byte_size(tmp_path):
    path = tmp_path / "sized.txt"
    path.write_text("some content here", encoding="utf-8")
    document = load_document(path)
    assert document.metadata["byte_size"] == path.stat().st_size


def test_render_table_handles_empty_input():
    assert _render_table([]) == ""
    assert _render_table([[], []]) == ""
