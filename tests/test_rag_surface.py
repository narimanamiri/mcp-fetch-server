"""Tests for the corpus's MCP surface: prompts, introspection, chunk resource."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from qdrant_client import QdrantClient

from mcp_fetch_server.config import settings
from mcp_fetch_server.rag.catalog import Catalog, DocumentRecord, make_doc_id
from mcp_fetch_server.rag.documents import Chunk
from mcp_fetch_server.rag.llm import LocalLLM
from mcp_fetch_server.rag.retrieve import RetrievalTrace, Retriever
from mcp_fetch_server.rag.sparse import encode
from mcp_fetch_server.rag.store import SearchHit, VectorStore
from mcp_fetch_server.server import create_mcp_server

OLLAMA = "http://localhost:11434"
DIMENSION = 8

RAG_PROMPTS = {
    "research_corpus",
    "cite_claim",
    "compare_documents",
    "summarize_category",
    "explore_archive",
}


@pytest.fixture
def catalog(tmp_path):
    with Catalog(tmp_path / "corpus") as instance:
        yield instance


@pytest.fixture
def store():
    with VectorStore(client=QdrantClient(location=":memory:"), collection="t") as instance:
        instance.ensure_collection(dimension=DIMENSION)
        yield instance


def llm_client() -> LocalLLM:
    return LocalLLM(
        backend="ollama", base_url=OLLAMA, chat_model="gemma3:4b",
        embed_model="bge-m3", retries=0, timeout=5.0,
    )


def seed(catalog: Catalog, store: VectorStore):
    """One document whose chunk is findable by both arms."""
    url = "https://local.archive/doc/guide-1"
    text = "Dense retrievers embed text. The bge-m3 model produces 1024 dimensions."
    digest, blob_path = catalog.store_blob(text)
    record = DocumentRecord(
        doc_id=make_doc_id(url), url=url, content_hash=digest, doctype="markdown",
        blob_path=blob_path, source_path="/corpus/guide.md", title="Guide",
        language="en", char_count=len(text),
    )
    catalog.upsert_document(record)
    chunk = Chunk(
        doc_id=record.doc_id, chunk_index=0, text=text,
        heading_path=["Guide", "Dense"], page_start=2, page_end=2,
        char_start=0, char_end=len(text), token_estimate=18,
    )
    catalog.replace_chunks(record.doc_id, [chunk])
    store.upsert_chunks(chunk_list := [chunk], [[1.0] + [0.0] * (DIMENSION - 1)],
                        [encode(text)], record)
    assert chunk_list
    return record, chunk


# ---------------------------------------------------------------- prompts


async def test_corpus_prompts_are_registered():
    """The web side shipped five workflows and the corpus none, which left it
    reachable only by a client that already knew the tool order."""
    mcp = create_mcp_server()
    names = {prompt.name for prompt in await mcp.list_prompts()}
    assert RAG_PROMPTS <= names
    # The original web prompts must survive alongside them.
    assert {"research_topic", "compare_sources"} <= names


@pytest.mark.parametrize("name", sorted(RAG_PROMPTS))
async def test_each_prompt_renders_and_warns_about_untrusted_content(name):
    mcp = create_mcp_server()
    arguments = {
        "research_corpus": {"topic": "retrieval"},
        "cite_claim": {"claim": "bge-m3 has 1024 dimensions"},
        "compare_documents": {"question": "which model?"},
        "summarize_category": {"category": "research"},
        "explore_archive": {},
    }[name]

    result = await mcp.get_prompt(name, arguments)
    text = " ".join(
        message.content.text for message in result.messages
        if hasattr(message.content, "text")
    )
    assert text.strip()
    # A corpus document can carry an injected instruction exactly as a web
    # page can, so every workflow has to say so.
    assert "untrusted data" in text


async def test_prompts_name_real_tools():
    """A workflow that cites a tool the server does not expose is worse than
    no workflow: the client follows it and fails."""
    mcp = create_mcp_server()
    available = {tool.name for tool in await mcp.list_tools()}

    for name, arguments in (
        ("research_corpus", {"topic": "x"}),
        ("cite_claim", {"claim": "x"}),
        ("compare_documents", {"question": "x"}),
        ("explore_archive", {}),
    ):
        result = await mcp.get_prompt(name, arguments)
        text = " ".join(
            message.content.text for message in result.messages
            if hasattr(message.content, "text")
        )
        for mentioned in ("rag_search", "corpus_stats", "fetch_url"):
            if mentioned in text:
                assert mentioned in available, f"{name} cites missing tool {mentioned}"


async def test_compare_documents_threads_a_category_through():
    mcp = create_mcp_server()
    result = await mcp.get_prompt("compare_documents", {"question": "q", "category": "ml"})
    text = " ".join(
        message.content.text for message in result.messages
        if hasattr(message.content, "text")
    )
    assert 'categories="ml"' in text


# ----------------------------------------------------------- introspection


@respx.mock
async def test_explain_reports_each_arm_separately(catalog, store):
    """The point of the trace: the arms can be inspected on their own, which
    is what distinguishes a coverage problem from a vocabulary one."""
    respx.post(f"{OLLAMA}/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[1.0] + [0.0] * (DIMENSION - 1)]})
    )
    record, chunk = seed(catalog, store)

    client = llm_client()
    retriever = Retriever(catalog=catalog, store=store, llm=client)
    try:
        trace = await retriever.explain("bge-m3 dimensions", top_k=3)
    finally:
        await client.aclose()

    assert trace.dense and trace.sparse and trace.fused
    assert trace.dense[0].chunk_id == chunk.chunk_id
    assert trace.sparse[0].chunk_id == chunk.chunk_id
    assert trace.candidates >= 1
    assert "bge-m3" in trace.terms


@respx.mock
async def test_explain_shows_which_terms_the_index_dropped(catalog, store):
    respx.post(f"{OLLAMA}/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[1.0] + [0.0] * (DIMENSION - 1)]})
    )
    seed(catalog, store)

    client = llm_client()
    retriever = Retriever(catalog=catalog, store=store, llm=client)
    try:
        trace = await retriever.explain("what is the model", top_k=3)
    finally:
        await client.aclose()

    # "what", "is" and "the" are stop words; "model" is not.
    assert "model" not in trace.dropped_terms
    assert set(trace.dropped_terms) & {"what", "is", "the"}


async def test_explain_on_an_empty_query(catalog, store):
    client = llm_client()
    retriever = Retriever(catalog=catalog, store=store, llm=client)
    try:
        trace = await retriever.explain("   ")
    finally:
        await client.aclose()
    assert trace.fused == []
    assert "Provide a query" in trace.render()


@respx.mock
async def test_explain_renders_guidance_and_serialises(catalog, store):
    respx.post(f"{OLLAMA}/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[1.0] + [0.0] * (DIMENSION - 1)]})
    )
    seed(catalog, store)

    client = llm_client()
    retriever = Retriever(catalog=catalog, store=store, llm=client)
    try:
        trace = await retriever.explain("bge-m3", top_k=2)
    finally:
        await client.aclose()

    rendered = trace.render()
    assert "Dense arm" in rendered
    assert "Sparse arm" in rendered
    assert "Fused" in rendered
    assert "coverage problem" in rendered

    payload = trace.as_dict()
    assert set(payload["arms"]) >= {"dense", "sparse", "fused"}
    assert payload["arms"]["fused"][0]["rank"] == 1
    json.dumps(payload)  # must be serialisable for the JSON path


def test_trace_render_does_not_repeat_the_title():
    trace = RetrievalTrace(query="q", top_k=1)
    hit = SearchHit(chunk_id="d:0", doc_id="d", score=1.0, title="Guide")
    hit.heading_path = ["Guide", "Install"]
    trace.fused = [hit]
    rendered = trace.render()
    assert "Guide > Install" in rendered
    assert "Guide > Guide" not in rendered


# -------------------------------------------------------- chunk resource


async def test_chunk_resource_is_registered():
    mcp = create_mcp_server()
    templates = {template.uriTemplate for template in await mcp.list_resource_templates()}
    assert "corpus://chunk/{chunk_id}" in templates


async def test_chunk_resource_returns_span_and_text(catalog, store, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "corpus_data_dir", str(tmp_path / "corpus"))
    record, chunk = seed(catalog, store)

    mcp = create_mcp_server()
    contents = await mcp.read_resource(f"corpus://chunk/{chunk.chunk_id}")
    payload = json.loads(next(iter(contents)).content)

    assert payload["chunk_id"] == chunk.chunk_id
    assert payload["heading_path"] == ["Guide", "Dense"]
    assert payload["pages"] == [2, 2]
    assert payload["char_span"] == [0, len(chunk.text)]
    assert payload["document"]["url"] == record.url
    assert "bge-m3" in payload["text"]


async def test_chunk_resource_reports_a_missing_chunk(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "corpus_data_dir", str(tmp_path / "empty"))
    mcp = create_mcp_server()
    contents = await mcp.read_resource("corpus://chunk/nothere:9")
    payload = json.loads(next(iter(contents)).content)
    assert "error" in payload


# ------------------------------------------------------------------ tool


async def test_rag_explain_tool_is_registered_read_only():
    mcp = create_mcp_server()
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert "rag_explain" in tools
    assert tools["rag_explain"].annotations.readOnlyHint is True
