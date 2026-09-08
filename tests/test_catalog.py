"""Tests for the SQLite catalog and blob store."""

from __future__ import annotations

import time

import pytest

from mcp_fetch_server.rag.catalog import (
    Catalog,
    CatalogError,
    DocumentRecord,
    content_hash,
    make_doc_id,
    mint_url,
)
from mcp_fetch_server.rag.documents import Chunk


@pytest.fixture
def catalog(tmp_path):
    with Catalog(tmp_path / "corpus") as instance:
        yield instance


def make_record(catalog: Catalog, *, url: str = "https://local.archive/doc/a-1", **kwargs):
    text = kwargs.pop("text", "# Title\n\nBody text.")
    digest, blob_path = catalog.store_blob(text)
    defaults = dict(
        doc_id=make_doc_id(url),
        url=url,
        content_hash=digest,
        doctype="markdown",
        blob_path=blob_path,
        source_path="/corpus/a.md",
        title="Title",
        language="en",
        char_count=len(text),
    )
    defaults.update(kwargs)
    record = DocumentRecord(**defaults)
    catalog.upsert_document(record)
    return record


def chunks_for(doc_id: str, count: int = 3) -> list[Chunk]:
    return [
        Chunk(
            doc_id=doc_id,
            chunk_index=index,
            text=f"chunk {index} text",
            heading_path=["Section", f"Sub {index}"],
            page_start=index + 1,
            page_end=index + 1,
            char_start=index * 100,
            char_end=index * 100 + 90,
            token_estimate=40,
        )
        for index in range(count)
    ]


# ------------------------------------------------------------- identity


def test_content_hash_is_stable_across_types():
    assert content_hash("abc") == content_hash(b"abc")
    assert content_hash("abc") != content_hash("abd")


def test_make_doc_id_is_stable_and_short():
    assert make_doc_id("https://x/1") == make_doc_id("https://x/1")
    assert len(make_doc_id("https://x/1")) == 16
    assert make_doc_id("https://x/1") != make_doc_id("https://x/2")


def test_mint_url_is_readable_and_stable():
    url = mint_url("Annual Report 2024", "/docs/report.pdf", base_url="https://local.archive")
    assert url.startswith("https://local.archive/doc/annual-report-2024-")
    assert url == mint_url("Annual Report 2024", "/docs/report.pdf", base_url="https://local.archive")


def test_mint_url_disambiguates_same_title_in_different_folders():
    first = mint_url("Report", "/a/report.pdf", base_url="https://x")
    second = mint_url("Report", "/b/report.pdf", base_url="https://x")
    assert first != second


def test_mint_url_percent_encodes_persian_titles():
    url = mint_url("گزارش سالانه", "/docs/fa.pdf", base_url="https://x")
    assert url.startswith("https://x/doc/")
    assert " " not in url
    assert "%" in url  # non-ASCII slug is encoded, so the URL stays valid


# ------------------------------------------------------------ blob store


def test_store_blob_is_content_addressed_and_idempotent(catalog):
    digest_one, path_one = catalog.store_blob("hello world")
    digest_two, path_two = catalog.store_blob("hello world")
    assert (digest_one, path_one) == (digest_two, path_two)
    assert catalog.read_blob(path_one) == "hello world"
    assert path_one.startswith(digest_one[:2] + "/")


def test_store_blob_leaves_no_temporary_files(catalog):
    catalog.store_blob("content")
    assert not list(catalog.objects_dir.rglob("*.tmp"))


def test_read_missing_blob_raises(catalog):
    with pytest.raises(CatalogError, match="Could not read blob"):
        catalog.read_blob("ff/nonexistent.md")


def test_blob_exists(catalog):
    _, path = catalog.store_blob("x")
    assert catalog.blob_exists(path)
    assert not catalog.blob_exists("00/missing.md")


def test_unicode_blob_round_trips(catalog):
    text = "# گزارش\n\nمتن فارسی با اعداد ۱۲۳"
    _, path = catalog.store_blob(text)
    assert catalog.read_blob(path) == text


# -------------------------------------------------------------- documents


def test_upsert_and_get_document(catalog):
    record = make_record(catalog)
    fetched = catalog.get_document(record.doc_id)
    assert fetched is not None
    assert fetched.url == record.url
    assert fetched.title == "Title"
    assert fetched.categories == []


def test_upsert_is_idempotent_and_updates_in_place(catalog):
    record = make_record(catalog)
    record.title = "Updated Title"
    record.status = "embedded"
    catalog.upsert_document(record)

    assert catalog.stats()["documents"] == 1
    fetched = catalog.get_document(record.doc_id)
    assert fetched.title == "Updated Title"
    assert fetched.status == "embedded"


def test_updated_at_advances_on_upsert(catalog):
    record = make_record(catalog)
    first = catalog.get_document(record.doc_id).updated_at
    time.sleep(0.01)
    catalog.upsert_document(record)
    assert catalog.get_document(record.doc_id).updated_at > first


def test_lookup_by_url_and_source(catalog):
    record = make_record(catalog)
    assert catalog.get_document_by_url(record.url).doc_id == record.doc_id
    assert catalog.get_document_by_source("/corpus/a.md").doc_id == record.doc_id
    assert catalog.get_document_by_url("https://absent") is None


def test_json_columns_round_trip(catalog):
    record = make_record(
        catalog,
        categories=["ml/retrieval", "ml/embeddings"],
        tags=["rag", "بازیابی"],
        entities=["BM25"],
        questions=["What is RAG?"],
        meta={"author": "Nariman", "pages": 12},
    )
    fetched = catalog.get_document(record.doc_id)
    assert fetched.categories == ["ml/retrieval", "ml/embeddings"]
    assert fetched.tags == ["rag", "بازیابی"]
    assert fetched.entities == ["BM25"]
    assert fetched.questions == ["What is RAG?"]
    assert fetched.meta == {"author": "Nariman", "pages": 12}


def test_corrupt_json_column_degrades_to_empty(catalog):
    record = make_record(catalog)
    catalog.connection.execute(
        "UPDATE documents SET categories = ? WHERE doc_id = ?", ("not json", record.doc_id)
    )
    assert catalog.get_document(record.doc_id).categories == []


def test_duplicate_url_is_rejected(catalog):
    make_record(catalog, url="https://local.archive/doc/same")
    with pytest.raises(Exception):
        record = DocumentRecord(
            doc_id="different_id_00",
            url="https://local.archive/doc/same",
            content_hash="h",
            doctype="markdown",
            blob_path="aa/bb.md",
        )
        catalog.upsert_document(record)


def test_list_documents_filters_and_paginates(catalog):
    for index in range(5):
        make_record(
            catalog,
            url=f"https://local.archive/doc/d{index}",
            doc_id=make_doc_id(f"https://local.archive/doc/d{index}"),
            source_path=f"/corpus/{index}.md",
            language="fa" if index % 2 else "en",
            status="ingested" if index < 3 else "embedded",
        )
    assert len(catalog.list_documents(limit=2)) == 2
    assert len(catalog.list_documents(status="embedded")) == 2
    assert len(catalog.list_documents(language="fa")) == 2
    assert len(catalog.list_documents(limit=10, offset=4)) == 1


def test_delete_document_removes_chunks(catalog):
    record = make_record(catalog)
    catalog.replace_chunks(record.doc_id, chunks_for(record.doc_id))
    assert catalog.stats()["chunks"] == 3

    assert catalog.delete_document(record.doc_id) is True
    assert catalog.get_document(record.doc_id) is None
    assert catalog.stats()["chunks"] == 0
    assert catalog.delete_document(record.doc_id) is False


def test_iter_documents(catalog):
    make_record(catalog, url="https://x/1", doc_id=make_doc_id("https://x/1"))
    make_record(
        catalog,
        url="https://x/2",
        doc_id=make_doc_id("https://x/2"),
        source_path="/corpus/b.md",
        status="embedded",
    )
    assert len(list(catalog.iter_documents())) == 2
    assert len(list(catalog.iter_documents(status="embedded"))) == 1


# ----------------------------------------------------------------- chunks


def test_replace_chunks_round_trips(catalog):
    record = make_record(catalog)
    assert catalog.replace_chunks(record.doc_id, chunks_for(record.doc_id)) == 3

    stored = catalog.get_chunks(record.doc_id)
    assert [chunk.chunk_index for chunk in stored] == [0, 1, 2]
    assert stored[0].heading_path == ["Section", "Sub 0"]
    assert stored[1].char_start == 100
    assert catalog.get_document(record.doc_id).chunk_count == 3


def test_replace_chunks_swaps_rather_than_appends(catalog):
    record = make_record(catalog)
    catalog.replace_chunks(record.doc_id, chunks_for(record.doc_id, 5))
    catalog.replace_chunks(record.doc_id, chunks_for(record.doc_id, 2))
    assert len(catalog.get_chunks(record.doc_id)) == 2
    assert catalog.get_document(record.doc_id).chunk_count == 2


def test_replace_chunks_is_atomic_on_failure(catalog):
    record = make_record(catalog)
    catalog.replace_chunks(record.doc_id, chunks_for(record.doc_id, 3))

    broken = chunks_for(record.doc_id, 2)
    broken[1].chunk_index = 0  # violates UNIQUE(doc_id, chunk_index)
    with pytest.raises(Exception):
        catalog.replace_chunks(record.doc_id, broken)

    # The original chunks must survive a failed replacement.
    assert len(catalog.get_chunks(record.doc_id)) == 3


def test_get_chunk_and_batch_lookup(catalog):
    record = make_record(catalog)
    catalog.replace_chunks(record.doc_id, chunks_for(record.doc_id, 4))

    assert catalog.get_chunk(f"{record.doc_id}:2").chunk_index == 2
    assert catalog.get_chunk("missing:0") is None

    ids = [f"{record.doc_id}:0", f"{record.doc_id}:3", "absent:9"]
    found = catalog.get_chunks_by_ids(ids)
    assert set(found) == {f"{record.doc_id}:0", f"{record.doc_id}:3"}
    assert catalog.get_chunks_by_ids([]) == {}


def test_neighbour_chunks_returns_a_window(catalog):
    record = make_record(catalog)
    catalog.replace_chunks(record.doc_id, chunks_for(record.doc_id, 5))

    window = catalog.neighbour_chunks(record.doc_id, 2, window=1)
    assert [chunk.chunk_index for chunk in window] == [1, 2, 3]

    edge = catalog.neighbour_chunks(record.doc_id, 0, window=1)
    assert [chunk.chunk_index for chunk in edge] == [0, 1]


def test_chunks_are_deleted_with_their_document(catalog):
    record = make_record(catalog)
    catalog.replace_chunks(record.doc_id, chunks_for(record.doc_id))
    catalog.connection.execute("DELETE FROM documents WHERE doc_id = ?", (record.doc_id,))
    assert catalog.get_chunks(record.doc_id) == []


# ------------------------------------------------------------------ links


def test_links_round_trip(catalog):
    record = make_record(catalog)
    catalog.replace_links(
        record.doc_id,
        [("https://local.archive/doc/other", "Other doc"), ("https://example.com", "External")],
    )
    outbound = catalog.outbound_links(record.doc_id)
    assert ("https://example.com", "External") in outbound
    assert catalog.inbound_links("https://local.archive/doc/other") == [record.doc_id]


def test_replace_links_swaps(catalog):
    record = make_record(catalog)
    catalog.replace_links(record.doc_id, [("https://a", "a"), ("https://b", "b")])
    catalog.replace_links(record.doc_id, [("https://c", "c")])
    assert catalog.outbound_links(record.doc_id) == [("https://c", "c")]


# ------------------------------------------------------------------ stats


def test_stats_counts_and_groups(catalog):
    make_record(catalog, url="https://x/1", doc_id=make_doc_id("https://x/1"))
    make_record(
        catalog,
        url="https://x/2",
        doc_id=make_doc_id("https://x/2"),
        source_path="/corpus/b.pdf",
        doctype="pdf",
        language="fa",
        text="different content",
    )
    stats = catalog.stats()
    assert stats["documents"] == 2
    assert stats["by_doctype"] == {"markdown": 1, "pdf": 1}
    assert set(stats["by_language"]) == {"en", "fa"}
    assert stats["characters"] > 0
    assert stats["db_bytes"] > 0


def test_stats_on_empty_catalog(catalog):
    stats = catalog.stats()
    assert stats["documents"] == 0
    assert stats["chunks"] == 0
    assert stats["characters"] == 0


# ------------------------------------------------------------ garbage


def test_collect_garbage_removes_unreferenced_blobs(catalog):
    record = make_record(catalog)
    catalog.store_blob("orphaned content nobody references")
    assert len(list(catalog.objects_dir.rglob("*.md"))) == 2

    assert catalog.collect_garbage() == 1
    assert catalog.blob_exists(record.blob_path)


def test_collect_garbage_on_empty_store(tmp_path):
    with Catalog(tmp_path / "empty") as instance:
        assert instance.collect_garbage() == 0


# ---------------------------------------------------------------- reopen


def test_catalog_survives_reopen(tmp_path):
    directory = tmp_path / "persist"
    with Catalog(directory) as first:
        record = make_record(first)
        first.replace_chunks(record.doc_id, chunks_for(record.doc_id))

    with Catalog(directory) as second:
        assert second.stats()["documents"] == 1
        assert len(second.get_chunks(record.doc_id)) == 3


def test_open_is_idempotent(catalog):
    assert catalog.open() is catalog
    assert catalog.stats()["documents"] == 0


def test_close_then_use_reopens(tmp_path):
    instance = Catalog(tmp_path / "reopen")
    instance.open()
    instance.close()
    assert instance.stats()["documents"] == 0  # lazily reopened
    instance.close()
