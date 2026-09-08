"""Tests for the Qdrant vector store, embedding pass and retriever.

These run against an in-memory Qdrant, so they exercise real indexing and real
hybrid search rather than a mock of it.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from qdrant_client import QdrantClient

from mcp_fetch_server.rag.catalog import Catalog, DocumentRecord, make_doc_id
from mcp_fetch_server.rag.documents import Chunk
from mcp_fetch_server.rag.embed import embedding_inputs, run_embedding, sparse_inputs
from mcp_fetch_server.rag.llm import LocalLLM
from mcp_fetch_server.rag.retrieve import (
    Retriever,
    deduplicate_by_document,
    hydrate,
)
from mcp_fetch_server.rag.sparse import encode
from mcp_fetch_server.rag.store import (
    SearchHit,
    StoreError,
    VectorStore,
    build_filter,
    chunk_payload,
    point_id,
)

OLLAMA = "http://localhost:11434"
DIMENSION = 8


@pytest.fixture
def catalog(tmp_path):
    with Catalog(tmp_path / "corpus") as instance:
        yield instance


@pytest.fixture
def store():
    with VectorStore(client=QdrantClient(location=":memory:"), collection="test") as instance:
        yield instance


def make_record(catalog: Catalog, *, url: str, text: str = "# T\n\nBody.", **kwargs):
    digest, blob_path = catalog.store_blob(text)
    defaults = dict(
        doc_id=make_doc_id(url),
        url=url,
        content_hash=digest,
        doctype="markdown",
        blob_path=blob_path,
        source_path=f"/corpus/{url.rsplit('/', 1)[-1]}.md",
        title="Title",
        language="en",
        char_count=len(text),
    )
    defaults.update(kwargs)
    record = DocumentRecord(**defaults)
    catalog.upsert_document(record)
    return record


def make_chunks(doc_id: str, texts: list[str]) -> list[Chunk]:
    return [
        Chunk(
            doc_id=doc_id,
            chunk_index=index,
            text=text,
            heading_path=["Section"],
            page_start=index + 1,
            page_end=index + 1,
            char_start=index * 100,
            char_end=index * 100 + 80,
            token_estimate=20,
        )
        for index, text in enumerate(texts)
    ]


def vector(seed: float) -> list[float]:
    return [seed] * DIMENSION


def llm_client() -> LocalLLM:
    return LocalLLM(
        backend="ollama",
        base_url=OLLAMA,
        chat_model="gemma3:4b",
        embed_model="bge-m3",
        retries=0,
        timeout=5.0,
    )


# --------------------------------------------------------------- point ids


def test_point_id_is_deterministic_and_uuid_shaped():
    import uuid

    first = point_id("abc:0")
    assert first == point_id("abc:0")
    assert first != point_id("abc:1")
    uuid.UUID(first)  # must parse


# ------------------------------------------------------------- collection


def test_ensure_collection_creates_once(store):
    assert store.ensure_collection(dimension=DIMENSION) is True
    assert store.ensure_collection(dimension=DIMENSION) is False
    assert store.exists()
    assert store.vector_dimension() == DIMENSION


def test_ensure_collection_rejects_a_dimension_change(store):
    """Vectors from two different embedding models in one index produce
    silently meaningless results, so this must fail loudly."""
    store.ensure_collection(dimension=DIMENSION)
    with pytest.raises(StoreError, match="cannot share an index"):
        store.ensure_collection(dimension=DIMENSION + 1)


def test_recreate_allows_a_dimension_change(store):
    store.ensure_collection(dimension=DIMENSION)
    assert store.ensure_collection(dimension=16, recreate=True) is True
    assert store.vector_dimension() == 16


def test_drop_removes_the_collection(store):
    store.ensure_collection(dimension=DIMENSION)
    store.drop()
    assert not store.exists()
    store.drop()  # dropping twice must not raise


def test_stats_on_missing_collection(store):
    stats = store.stats()
    assert stats["exists"] is False
    assert stats["points"] == 0


# ------------------------------------------------------------------ upsert


def test_upsert_and_count(catalog, store):
    store.ensure_collection(dimension=DIMENSION)
    record = make_record(catalog, url="https://x/1")
    chunks = make_chunks(record.doc_id, ["alpha text", "beta text"])

    written = store.upsert_chunks(
        chunks, [vector(0.1), vector(0.2)], [encode(c.text) for c in chunks], record
    )
    assert written == 2
    assert store.count() == 2


def test_upsert_is_idempotent(catalog, store):
    store.ensure_collection(dimension=DIMENSION)
    record = make_record(catalog, url="https://x/1")
    chunks = make_chunks(record.doc_id, ["alpha"])

    for _ in range(3):
        store.upsert_chunks(chunks, [vector(0.1)], [encode("alpha")], record)
    assert store.count() == 1


def test_upsert_rejects_mismatched_vector_counts(catalog, store):
    store.ensure_collection(dimension=DIMENSION)
    record = make_record(catalog, url="https://x/1")
    chunks = make_chunks(record.doc_id, ["a", "b"])
    with pytest.raises(StoreError, match="mismatch"):
        store.upsert_chunks(chunks, [vector(0.1)], [encode("a"), encode("b")], record)


def test_upsert_empty_is_a_noop(catalog, store):
    store.ensure_collection(dimension=DIMENSION)
    record = make_record(catalog, url="https://x/1")
    assert store.upsert_chunks([], [], [], record) == 0


def test_delete_document_removes_only_its_chunks(catalog, store):
    store.ensure_collection(dimension=DIMENSION)
    first = make_record(catalog, url="https://x/1")
    second = make_record(catalog, url="https://x/2", source_path="/corpus/2.md")

    for record, seed in ((first, 0.1), (second, 0.9)):
        chunks = make_chunks(record.doc_id, ["a", "b"])
        store.upsert_chunks(
            chunks, [vector(seed), vector(seed)], [encode("a"), encode("b")], record
        )
    assert store.count() == 4

    store.delete_document(first.doc_id)
    assert store.count() == 2


def test_chunk_payload_carries_filter_and_citation_fields(catalog):
    record = make_record(
        catalog, url="https://x/1", categories=["ml/retrieval"], tags=["rag"], language="fa"
    )
    chunk = make_chunks(record.doc_id, ["text"])[0]
    payload = chunk_payload(chunk, record)

    assert payload["chunk_id"] == chunk.chunk_id
    assert payload["url"] == record.url
    assert payload["categories"] == ["ml/retrieval"]
    assert payload["language"] == "fa"
    assert payload["page_start"] == 1


# ------------------------------------------------------------------ search


def index_corpus(catalog, store):
    """Two documents whose dense vectors are far apart, for search tests."""
    store.ensure_collection(dimension=DIMENSION)

    first = make_record(catalog, url="https://x/dense", categories=["ml"], language="en")
    second = make_record(
        catalog,
        url="https://x/lexical",
        source_path="/corpus/2.md",
        categories=["ops"],
        language="fa",
    )

    first_chunks = make_chunks(first.doc_id, ["retrieval systems rank documents"])
    second_chunks = make_chunks(second.doc_id, ["the bge-m3 model produces embeddings"])

    catalog.replace_chunks(first.doc_id, first_chunks)
    catalog.replace_chunks(second.doc_id, second_chunks)

    store.upsert_chunks(
        first_chunks, [[1.0] + [0.0] * (DIMENSION - 1)], [encode(first_chunks[0].text)], first
    )
    store.upsert_chunks(
        second_chunks, [[0.0] * (DIMENSION - 1) + [1.0]], [encode(second_chunks[0].text)], second
    )
    return first, second


def test_search_finds_by_dense_similarity(catalog, store):
    first, _ = index_corpus(catalog, store)
    hits = store.search(dense=[1.0] + [0.0] * (DIMENSION - 1), sparse=None, limit=5)
    assert hits
    assert hits[0].doc_id == first.doc_id


def test_search_finds_by_exact_term(catalog, store):
    """The point of the sparse arm: an exact token must be findable even when
    the dense vector points elsewhere."""
    _, second = index_corpus(catalog, store)
    hits = store.search(dense=None, sparse=encode("bge-m3"), limit=5)
    assert hits
    assert hits[0].doc_id == second.doc_id


def test_hybrid_search_returns_both_arms(catalog, store):
    index_corpus(catalog, store)
    hits = store.search(
        dense=[1.0] + [0.0] * (DIMENSION - 1), sparse=encode("bge-m3"), limit=5
    )
    assert len({hit.doc_id for hit in hits}) == 2


def test_search_with_no_query_returns_nothing(catalog, store):
    index_corpus(catalog, store)
    assert store.search(dense=None, sparse=encode(""), limit=5) == []


def test_search_respects_a_category_filter(catalog, store):
    first, _ = index_corpus(catalog, store)
    hits = store.search(
        dense=[1.0] + [0.0] * (DIMENSION - 1),
        sparse=None,
        limit=5,
        query_filter=build_filter(categories=["ml"]),
    )
    assert {hit.doc_id for hit in hits} == {first.doc_id}


def test_search_hits_carry_citation_metadata(catalog, store):
    index_corpus(catalog, store)
    hit = store.search(dense=[1.0] + [0.0] * (DIMENSION - 1), sparse=None, limit=1)[0]
    assert hit.url.startswith("https://x/")
    assert hit.citation_url.endswith("#p1")
    assert hit.heading_trail == "Section"


# ------------------------------------------------------------------ filter


def test_build_filter_returns_none_when_empty():
    assert build_filter() is None
    assert build_filter(categories=[], languages=None) is None
    assert build_filter(categories=["  "]) is None


def test_build_filter_combines_conditions():
    built = build_filter(categories=["a"], languages=["en", "fa"])
    assert built is not None
    assert len(built.must) == 2


# ------------------------------------------------------------ embed inputs


def test_embedding_inputs_prepend_title_and_headings(catalog):
    record = make_record(catalog, url="https://x/1", title="Guide")
    chunk = make_chunks(record.doc_id, ["Body text."])[0]
    text = embedding_inputs(chunk, record)
    assert text.startswith("Guide")
    assert "Section" in text
    assert "Body text." in text


def test_sparse_inputs_fold_questions_into_the_first_chunk(catalog):
    record = make_record(
        catalog, url="https://x/1", questions=["How do I install it?"], tags=["setup"]
    )
    chunks = make_chunks(record.doc_id, ["Body.", "More."])

    first = sparse_inputs(chunks[0], record)
    second = sparse_inputs(chunks[1], record)

    from mcp_fetch_server.rag.sparse import term_id

    assert term_id("install") in first.indices
    assert term_id("install") not in second.indices
    # Tags are folded into every chunk.
    assert term_id("setup") in second.indices


# ---------------------------------------------------------- embedding pass


@respx.mock
async def test_run_embedding_indexes_the_catalog(catalog, store):
    respx.post(f"{OLLAMA}/api/embed").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json={
                "embeddings": [
                    [0.5] * DIMENSION
                    for _ in __import__("json").loads(request.content)["input"]
                ]
            },
        )
    )
    record = make_record(catalog, url="https://x/1")
    catalog.replace_chunks(record.doc_id, make_chunks(record.doc_id, ["a", "b", "c"]))

    client = llm_client()
    try:
        summary = await run_embedding(catalog=catalog, store=store, llm=client)
    finally:
        await client.aclose()

    assert summary.embedded == 1
    assert summary.total_chunks == 3
    assert summary.dimension == DIMENSION
    assert store.count() == 3
    assert catalog.get_document(record.doc_id).embedded_at is not None
    assert catalog.get_document(record.doc_id).status == "embedded"


@respx.mock
async def test_run_embedding_skips_unchanged_documents(catalog, store):
    respx.post(f"{OLLAMA}/api/embed").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json={
                "embeddings": [
                    [0.5] * DIMENSION
                    for _ in __import__("json").loads(request.content)["input"]
                ]
            },
        )
    )
    record = make_record(catalog, url="https://x/1")
    catalog.replace_chunks(record.doc_id, make_chunks(record.doc_id, ["a"]))

    client = llm_client()
    try:
        await run_embedding(catalog=catalog, store=store, llm=client)
        second = await run_embedding(catalog=catalog, store=store, llm=client)
    finally:
        await client.aclose()

    assert second.embedded == 0
    assert second.results == []


@respx.mock
async def test_reembedding_replaces_rather_than_duplicates(catalog, store):
    respx.post(f"{OLLAMA}/api/embed").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json={
                "embeddings": [
                    [0.5] * DIMENSION
                    for _ in __import__("json").loads(request.content)["input"]
                ]
            },
        )
    )
    record = make_record(catalog, url="https://x/1")
    catalog.replace_chunks(record.doc_id, make_chunks(record.doc_id, ["a", "b", "c"]))

    client = llm_client()
    try:
        await run_embedding(catalog=catalog, store=store, llm=client)
        # Re-chunk to fewer chunks; the leftovers must not stay searchable.
        catalog.replace_chunks(record.doc_id, make_chunks(record.doc_id, ["a"]))
        await run_embedding(catalog=catalog, store=store, llm=client, reembed=True)
    finally:
        await client.aclose()

    assert store.count() == 1


# --------------------------------------------------------------- retriever


def test_deduplicate_caps_passages_per_document():
    hits = [SearchHit(chunk_id=f"d1:{i}", doc_id="d1", score=1.0) for i in range(5)]
    hits += [SearchHit(chunk_id=f"d2:{i}", doc_id="d2", score=0.9) for i in range(5)]

    kept = deduplicate_by_document(hits, per_document=2)
    assert len(kept) == 4
    assert sum(1 for hit in kept if hit.doc_id == "d1") == 2


def test_deduplicate_disabled_keeps_everything():
    hits = [SearchHit(chunk_id=f"d1:{i}", doc_id="d1", score=1.0) for i in range(5)]
    assert len(deduplicate_by_document(hits, per_document=0)) == 5


def test_hydrate_attaches_text_and_drops_stale_hits(catalog):
    record = make_record(catalog, url="https://x/1")
    catalog.replace_chunks(record.doc_id, make_chunks(record.doc_id, ["real text"]))

    hits = [
        SearchHit(chunk_id=f"{record.doc_id}:0", doc_id=record.doc_id, score=1.0),
        SearchHit(chunk_id="gone:0", doc_id="gone", score=0.5),
    ]
    hydrated = hydrate(hits, catalog)

    assert len(hydrated) == 1
    assert hydrated[0].text == "real text"


def test_hydrate_empty():
    assert hydrate([], None) == []


@respx.mock
async def test_retriever_searches_and_renders(catalog, store):
    respx.post(f"{OLLAMA}/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[1.0] + [0.0] * (DIMENSION - 1)]})
    )
    first, _ = index_corpus(catalog, store)

    client = llm_client()
    retriever = Retriever(catalog=catalog, store=store, llm=client)
    try:
        result = await retriever.search("retrieval systems", top_k=2)
    finally:
        await client.aclose()

    assert result
    assert result.hits[0].text
    rendered = result.render()
    assert "LOCAL CORPUS" in rendered
    assert "Source: https://x/" in rendered
    assert result.as_dict()["results"][0]["url"].endswith("#p1")


@respx.mock
async def test_retriever_render_does_not_repeat_the_title(catalog, store):
    respx.post(f"{OLLAMA}/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[1.0] + [0.0] * (DIMENSION - 1)]})
    )
    store.ensure_collection(dimension=DIMENSION)
    record = make_record(catalog, url="https://x/1", title="Guide")
    chunks = make_chunks(record.doc_id, ["body"])
    chunks[0].heading_path = ["Guide", "Install"]
    catalog.replace_chunks(record.doc_id, chunks)
    store.upsert_chunks(chunks, [[1.0] + [0.0] * (DIMENSION - 1)], [encode("body")], record)

    client = llm_client()
    retriever = Retriever(catalog=catalog, store=store, llm=client)
    try:
        rendered = (await retriever.search("body")).render()
    finally:
        await client.aclose()

    assert "Guide > Install" in rendered
    assert "Guide > Guide" not in rendered


async def test_retriever_empty_query_returns_nothing(catalog, store):
    retriever = Retriever(catalog=catalog, store=store, llm=llm_client())
    try:
        result = await retriever.search("   ")
    finally:
        retriever.close()
    assert not result


@respx.mock
async def test_retriever_reports_no_matches_helpfully(catalog, store):
    respx.post(f"{OLLAMA}/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[1.0] + [0.0] * (DIMENSION - 1)]})
    )
    store.ensure_collection(dimension=DIMENSION)

    client = llm_client()
    retriever = Retriever(catalog=catalog, store=store, llm=client)
    try:
        result = await retriever.search("anything at all")
    finally:
        await client.aclose()

    assert not result
    assert "No passages in the local corpus" in result.render()
