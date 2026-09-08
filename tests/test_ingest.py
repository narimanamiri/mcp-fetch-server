"""Tests for the ingestion pipeline and its CLI."""

from __future__ import annotations

import pytest

from mcp_fetch_server.__main__ import main as cli_main
from mcp_fetch_server.rag.catalog import Catalog
from mcp_fetch_server.rag.ingest import discover_files, ingest_paths

BASE = "https://local.archive"


@pytest.fixture
def catalog(tmp_path):
    with Catalog(tmp_path / "corpus") as instance:
        yield instance


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "guide.md").write_text(
        "# Retrieval Guide\n\n## Dense\n\nDense retrievers embed text into vectors.\n\n"
        "## Sparse\n\nBM25 ranks by term frequency.\n",
        encoding="utf-8",
    )
    (root / "notes.txt").write_text(
        "1. Overview\n\nThis note explains the ingestion pipeline in prose.",
        encoding="utf-8",
    )
    return root


async def run_ingest(catalog, paths, **kwargs):
    return await ingest_paths(paths, catalog=catalog, base_url=BASE, **kwargs)


# ------------------------------------------------------------- discovery


def test_discover_files_walks_directories(corpus):
    found = discover_files([corpus])
    assert {path.name for path in found} == {"guide.md", "notes.txt"}


def test_discover_files_skips_unsupported_and_hidden(corpus):
    (corpus / "data.xlsx").write_text("nope", encoding="utf-8")
    (corpus / ".hidden").mkdir()
    (corpus / ".hidden" / "secret.md").write_text("# hidden", encoding="utf-8")
    (corpus / "node_modules").mkdir()
    (corpus / "node_modules" / "pkg.md").write_text("# vendored", encoding="utf-8")

    names = {path.name for path in discover_files([corpus])}
    assert names == {"guide.md", "notes.txt"}


def test_discover_files_deduplicates_and_accepts_single_files(corpus):
    found = discover_files([corpus, corpus / "guide.md", corpus / "guide.md"])
    assert len(found) == 2


def test_discover_files_ignores_missing_paths(tmp_path):
    assert discover_files([tmp_path / "nowhere"]) == []


def test_discover_files_is_sorted(corpus):
    found = discover_files([corpus])
    assert found == sorted(found)


# --------------------------------------------------------------- ingest


async def test_ingest_stores_documents_and_chunks(catalog, corpus):
    summary = await run_ingest(catalog, [corpus])

    assert summary.ingested == 2
    assert summary.failed == 0
    assert summary.total_chunks > 0

    documents = catalog.list_documents()
    assert len(documents) == 2
    guide = catalog.get_document_by_source(corpus / "guide.md")
    assert guide is not None
    assert guide.title == "Retrieval Guide"
    assert guide.url.startswith(f"{BASE}/doc/retrieval-guide-")
    assert guide.doctype == "markdown"
    assert guide.language == "en"
    assert guide.chunk_count == len(catalog.get_chunks(guide.doc_id))
    assert guide.status == "ingested"


async def test_stored_blob_matches_the_canonical_text(catalog, corpus):
    await run_ingest(catalog, [corpus])
    guide = catalog.get_document_by_source(corpus / "guide.md")
    blob = catalog.read_blob(guide.blob_path)
    assert blob.startswith("# Retrieval Guide")
    assert guide.char_count == len(blob)


async def test_chunk_spans_resolve_against_the_stored_blob(catalog, corpus):
    await run_ingest(catalog, [corpus])
    guide = catalog.get_document_by_source(corpus / "guide.md")
    blob = catalog.read_blob(guide.blob_path)
    for chunk in catalog.get_chunks(guide.doc_id):
        assert 0 <= chunk.char_start <= chunk.char_end <= len(blob)
        assert blob[chunk.char_start : chunk.char_end].strip()


async def test_ingest_is_incremental(catalog, corpus):
    first = await run_ingest(catalog, [corpus])
    assert first.ingested == 2

    second = await run_ingest(catalog, [corpus])
    assert second.skipped == 2
    assert second.ingested == 0
    assert catalog.stats()["documents"] == 2


async def test_reingest_forces_reparse(catalog, corpus):
    await run_ingest(catalog, [corpus])
    summary = await run_ingest(catalog, [corpus], reingest=True)
    assert summary.ingested == 2
    assert catalog.stats()["documents"] == 2


async def test_changed_file_is_reingested_and_replaces_chunks(catalog, corpus):
    await run_ingest(catalog, [corpus])
    guide = catalog.get_document_by_source(corpus / "guide.md")
    original_hash = guide.content_hash

    (corpus / "guide.md").write_text(
        "# Retrieval Guide\n\n## Dense\n\nCompletely rewritten body text here.\n",
        encoding="utf-8",
    )
    summary = await run_ingest(catalog, [corpus])
    assert summary.ingested == 1
    assert summary.skipped == 1

    updated = catalog.get_document_by_source(corpus / "guide.md")
    assert updated.content_hash != original_hash
    assert updated.doc_id == guide.doc_id  # the URL, and so the id, is stable
    assert "rewritten" in catalog.read_blob(updated.blob_path)
    assert catalog.stats()["documents"] == 2


async def test_reingest_clears_stale_enrichment(catalog, corpus):
    await run_ingest(catalog, [corpus])
    guide = catalog.get_document_by_source(corpus / "guide.md")
    guide.summary = "A summary of the old text."
    guide.enriched_at = 123.0
    catalog.upsert_document(guide)

    (corpus / "guide.md").write_text("# Retrieval Guide\n\nEntirely new content.", encoding="utf-8")
    await run_ingest(catalog, [corpus])

    updated = catalog.get_document_by_source(corpus / "guide.md")
    assert updated.summary is None
    assert updated.enriched_at is None


async def test_unchanged_reingest_preserves_enrichment(catalog, corpus):
    await run_ingest(catalog, [corpus])
    guide = catalog.get_document_by_source(corpus / "guide.md")
    guide.summary = "Still accurate."
    guide.tags = ["retrieval"]
    catalog.upsert_document(guide)

    await run_ingest(catalog, [corpus], reingest=True)

    updated = catalog.get_document_by_source(corpus / "guide.md")
    assert updated.summary == "Still accurate."
    assert updated.tags == ["retrieval"]


# --------------------------------------------------------------- failures


async def test_one_bad_file_does_not_stop_the_run(catalog, corpus):
    scan = corpus / "scanned.pdf"
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    for _ in range(4):
        writer.add_blank_page(width=612, height=792)
    with scan.open("wb") as handle:
        writer.write(handle)

    summary = await run_ingest(catalog, [corpus])
    assert summary.ingested == 2
    assert summary.failed == 1
    assert "ocrmypdf" in summary.failures[0].error
    assert catalog.stats()["documents"] == 2


async def test_empty_file_is_reported_as_a_failure(catalog, corpus):
    (corpus / "blank.md").write_text("   \n\n  ", encoding="utf-8")
    summary = await run_ingest(catalog, [corpus])
    assert summary.failed == 1
    assert "no readable text" in summary.failures[0].error


async def test_empty_directory_yields_empty_summary(catalog, tmp_path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    summary = await run_ingest(catalog, [empty])
    assert summary.results == []
    assert summary.ingested == 0


# ----------------------------------------------------------------- links


async def test_html_links_are_recorded_in_the_graph(catalog, tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    (root / "index.html").write_text(
        "<html><head><title>Index</title></head><body><h1>Index</h1>"
        "<p>Welcome to the archive.</p>"
        '<a href="page2.html">Page Two</a>'
        '<a href="#anchor">Ignored</a>'
        '<a href="mailto:x@y.z">Ignored</a>'
        "</body></html>",
        encoding="utf-8",
    )
    await run_ingest(catalog, [root])

    document = catalog.get_document_by_source(root / "index.html")
    outbound = catalog.outbound_links(document.doc_id)
    assert outbound == [("page2.html", "Page Two")]


# ------------------------------------------------------------- behaviour


async def test_limit_caps_the_number_of_files(catalog, corpus):
    summary = await run_ingest(catalog, [corpus], limit=1)
    assert len(summary.results) == 1


async def test_on_result_callback_fires_per_file(catalog, corpus):
    seen = []
    await run_ingest(catalog, [corpus], on_result=seen.append)
    assert len(seen) == 2
    assert {result.status for result in seen} == {"ingested"}


async def test_results_are_sorted_by_path(catalog, corpus):
    summary = await run_ingest(catalog, [corpus], workers=4)
    paths = [str(result.path) for result in summary.results]
    assert paths == sorted(paths)


async def test_summary_serialises(catalog, corpus):
    summary = await run_ingest(catalog, [corpus])
    payload = summary.as_dict()
    assert payload["ingested"] == 2
    assert payload["chunks"] == summary.total_chunks
    assert len(payload["documents"]) == 2
    assert "Ingested 2" in summary.render()


async def test_persian_document_is_ingested_with_language_and_url(catalog, tmp_path):
    root = tmp_path / "fa"
    root.mkdir()
    (root / "gozaresh.md").write_text(
        "# گزارش سالانه\n\nاین سند نتایج پژوهش را توضیح می دهد و شامل جزئیات است.\n",
        encoding="utf-8",
    )
    summary = await run_ingest(catalog, [root])
    assert summary.ingested == 1

    document = catalog.get_document_by_source(root / "gozaresh.md")
    assert document.language == "fa"
    assert document.title == "گزارش سالانه"
    assert document.url.startswith(f"{BASE}/doc/")
    assert catalog.get_chunks(document.doc_id)


# ------------------------------------------------------------------- cli


def test_cli_ingest_reports_and_returns_zero(corpus, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FETCH_CORPUS_DATA_DIR", str(tmp_path / "clidata"))
    from mcp_fetch_server.config import settings

    monkeypatch.setattr(settings, "corpus_data_dir", str(tmp_path / "clidata"))

    assert cli_main(["ingest", str(corpus)]) == 0
    output = capsys.readouterr().out
    assert "guide.md" in output
    assert "Ingested 2" in output


def test_cli_ingest_dry_run_writes_nothing(corpus, tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "drydata"
    from mcp_fetch_server.config import settings

    monkeypatch.setattr(settings, "corpus_data_dir", str(data_dir))

    assert cli_main(["ingest", str(corpus), "--dry-run"]) == 0
    assert "2 file(s) would be ingested" in capsys.readouterr().out
    assert not (data_dir / "catalog.db").exists()


def test_cli_ingest_json_output(corpus, tmp_path, monkeypatch, capsys):
    import json

    from mcp_fetch_server.config import settings

    monkeypatch.setattr(settings, "corpus_data_dir", str(tmp_path / "jsondata"))

    assert cli_main(["ingest", str(corpus), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ingested"] == 2


def test_cli_ingest_returns_one_on_failure(tmp_path, monkeypatch, capsys):
    from mcp_fetch_server.config import settings

    monkeypatch.setattr(settings, "corpus_data_dir", str(tmp_path / "faildata"))
    root = tmp_path / "bad"
    root.mkdir()
    (root / "blank.md").write_text("  ", encoding="utf-8")

    assert cli_main(["ingest", str(root)]) == 1
    assert "Failures:" in capsys.readouterr().out
