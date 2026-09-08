"""Tests for reranking, query expansion and grounded answering."""

from __future__ import annotations

import httpx
import pytest
import respx

from mcp_fetch_server.config import settings
from mcp_fetch_server.rag.answer import (
    Answer,
    answer_question,
    build_context,
    strip_unresolvable_citations,
)
from mcp_fetch_server.rag.expand import _clean, expand_query, fuse_by_rank, variants_schema
from mcp_fetch_server.rag.llm import LocalLLM
from mcp_fetch_server.rag.rerank import (
    CrossEncoderReranker,
    RerankUnavailable,
    maybe_rerank,
    passage_text,
)
from mcp_fetch_server.rag.retrieve import RetrievalResult
from mcp_fetch_server.rag.store import SearchHit

OLLAMA = "http://localhost:11434"


def llm() -> LocalLLM:
    return LocalLLM(
        backend="ollama",
        base_url=OLLAMA,
        chat_model="gemma3:4b",
        embed_model="bge-m3",
        retries=0,
        timeout=5.0,
    )


def reply(payload: str) -> httpx.Response:
    return httpx.Response(200, json={"message": {"content": payload}})


def hit(chunk_id: str, *, doc="d1", text="passage text", title="Doc", score=1.0) -> SearchHit:
    return SearchHit(chunk_id=chunk_id, doc_id=doc, score=score, title=title, text=text)


# ------------------------------------------------------------- citations


def test_citations_are_collected():
    text, cited = strip_unresolvable_citations("A claim [1]. Another [2].", 3)
    assert cited == [1, 2]
    assert text == "A claim [1]. Another [2]."


def test_grouped_citations_are_recognised():
    """Regression: models write [1, 3] at least as often as [1][3], and
    matching only the single form reported a grounded answer as uncited."""
    text, cited = strip_unresolvable_citations("Both say so [1, 3].", 3)
    assert cited == [1, 3]
    assert "[1, 3]" in text

    _, compact = strip_unresolvable_citations("Both [1,3].", 3)
    assert compact == [1, 3]


def test_out_of_range_citations_are_stripped():
    """A citation that does not resolve is worse than none: it looks checked."""
    text, cited = strip_unresolvable_citations("Real [1]. Invented [7].", 3)
    assert cited == [1]
    assert "[7]" not in text
    assert "Real [1]." in text


def test_partially_valid_group_keeps_only_real_numbers():
    text, cited = strip_unresolvable_citations("Mixed [2, 9].", 3)
    assert cited == [2]
    assert "[2]" in text
    assert "9" not in text


def test_uncited_answer_is_flagged():
    answer = Answer(question="q", text="No markers here.", hits=[hit("d1:0")], cited=[])
    assert "may not be grounded" in answer.render()
    assert answer.as_dict()["sources"][0]["cited"] is False


def test_cited_answer_is_not_flagged():
    answer = Answer(question="q", text="Grounded [1].", hits=[hit("d1:0")], cited=[1])
    rendered = answer.render()
    assert "may not be grounded" not in rendered
    assert "https" not in rendered or "Sources:" in rendered


def test_build_context_numbers_passages():
    context = build_context([hit("d1:0", text="first"), hit("d2:0", doc="d2", text="second")])
    assert "[1]" in context and "[2]" in context
    assert "first" in context and "second" in context


# --------------------------------------------------------------- answers


@respx.mock
async def test_answer_uses_only_retrieved_passages(monkeypatch):
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=reply("The model is bge-m3 [1].")
    )
    result = RetrievalResult(
        query="which model", hits=[hit("d1:0", text="bge-m3 produces 1024 dimensions")]
    )

    client = llm()
    try:
        answer = await answer_question("which model", llm=client, result=result)
    finally:
        await client.aclose()

    assert answer.grounded is True
    assert answer.cited == [1]
    assert "bge-m3" in answer.text


async def test_answer_without_hits_says_so():
    client = llm()
    try:
        answer = await answer_question(
            "anything", llm=client, result=RetrievalResult(query="anything")
        )
    finally:
        await client.aclose()

    assert answer.grounded is False
    assert "no passage" in answer.text
    assert answer.hits == []


async def test_empty_question():
    client = llm()
    try:
        answer = await answer_question("   ", llm=client)
    finally:
        await client.aclose()
    assert answer.grounded is False


@respx.mock
async def test_answer_serialises(monkeypatch):
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=reply("Answer [1]."))
    result = RetrievalResult(query="q", hits=[hit("d1:0")])

    client = llm()
    try:
        answer = await answer_question("q", llm=client, result=result)
    finally:
        await client.aclose()

    payload = answer.as_dict()
    assert payload["grounded"] is True
    assert payload["sources"][0]["n"] == 1
    assert payload["sources"][0]["cited"] is True


# --------------------------------------------------------------- expand


def test_clean_keeps_original_first_and_deduplicates():
    cleaned = _clean("original", ["Original", "variant", "variant", ""], 3)
    assert cleaned[0] == "original"
    assert cleaned == ["original", "variant"]


def test_clean_respects_the_limit():
    cleaned = _clean("q", [f"v{index}" for index in range(10)], 2)
    assert len(cleaned) == 3  # original plus two


def test_variants_schema_bounds_the_list():
    schema = variants_schema(3)
    assert schema["properties"]["queries"]["maxItems"] == 3
    assert schema["required"] == ["queries"]


@respx.mock
async def test_expand_query_returns_variants():
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=reply('{"queries": ["chunk splitting strategy", "how text is divided"]}')
    )
    client = llm()
    try:
        variants = await expand_query("how is chunking done", llm=client)
    finally:
        await client.aclose()

    assert variants[0] == "how is chunking done"
    assert "chunk splitting strategy" in variants


@respx.mock
async def test_expansion_failure_falls_back_to_the_original():
    """Expansion is an optimisation; losing it must not lose the search."""
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=httpx.Response(500, text="down"))
    client = llm()
    try:
        assert await expand_query("original query", llm=client) == ["original query"]
    finally:
        await client.aclose()


async def test_expand_empty_query():
    client = llm()
    try:
        assert await expand_query("  ", llm=client) == []
    finally:
        await client.aclose()


def test_fuse_by_rank_rewards_agreement():
    """A passage several phrasings agree on must beat one that only matched
    a single odd wording."""
    agreed = hit("shared:0")
    lists = [
        [hit("only-a:0"), agreed],
        [agreed, hit("only-b:0")],
        [agreed, hit("only-c:0")],
    ]
    fused = fuse_by_rank(lists)
    assert fused[0].chunk_id == "shared:0"


def test_fuse_by_rank_deduplicates():
    fused = fuse_by_rank([[hit("a:0")], [hit("a:0")], [hit("b:0")]])
    assert [item.chunk_id for item in fused] == ["a:0", "b:0"]


def test_fuse_empty():
    assert fuse_by_rank([]) == []


# --------------------------------------------------------------- rerank


def test_passage_text_includes_heading_context():
    item = hit("d1:0", text="Body.")
    item.heading_path = ["Guide", "Install"]
    text = passage_text(item)
    assert "Doc > Guide > Install" in text
    assert "Body." in text


def test_passage_text_is_trimmed():
    item = hit("d1:0", text="x" * 5000)
    assert len(passage_text(item)) < 3000


class FakeReranker(CrossEncoderReranker):
    def __init__(self, scores):
        super().__init__(model_name="fake")
        self._scores = scores

    async def score(self, query, passages):
        return self._scores[: len(passages)]


class BrokenReranker(CrossEncoderReranker):
    async def score(self, query, passages):
        raise RerankUnavailable("not installed")


async def test_rerank_reorders_by_score():
    hits = [hit("a:0", score=1.0), hit("b:0", doc="d2", score=0.9), hit("c:0", doc="d3")]
    reranked, applied = await maybe_rerank(
        "query", hits, reranker=FakeReranker([0.1, 9.0, 5.0])
    )
    assert applied is True
    assert [item.chunk_id for item in reranked] == ["b:0", "c:0", "a:0"]


async def test_rerank_respects_top_k():
    hits = [hit(f"{index}:0", doc=f"d{index}") for index in range(5)]
    reranked = await FakeReranker([1, 2, 3, 4, 5]).rerank("q", hits, top_k=2)
    assert len(reranked) == 2


async def test_missing_reranker_degrades_to_fused_order():
    """A missing dependency must cost quality, never availability."""
    hits = [hit("a:0", score=2.0), hit("b:0", doc="d2", score=1.0)]
    result, applied = await maybe_rerank("query", hits, reranker=BrokenReranker())
    assert applied is False
    assert [item.chunk_id for item in result] == ["a:0", "b:0"]


async def test_rerank_disabled_by_configuration(monkeypatch):
    monkeypatch.setattr(settings, "rag_rerank_enabled", False)
    hits = [hit("a:0")]
    result, applied = await maybe_rerank("q", hits, reranker=FakeReranker([9.0]))
    assert applied is False
    assert result == hits


async def test_rerank_empty_input():
    result, applied = await maybe_rerank("q", [], reranker=FakeReranker([]))
    assert result == []
    assert applied is False


async def test_unavailable_reranker_is_not_retried_every_query():
    """Retrying a missing dependency on each query adds a slow no-op to all
    of them."""
    reranker = CrossEncoderReranker(model_name="definitely-not-a-real-model")
    calls = {"count": 0}

    def fail():
        calls["count"] += 1
        raise RerankUnavailable("missing")

    reranker._load = fail

    for _ in range(3):
        with pytest.raises(RerankUnavailable):
            await reranker.model()
    assert calls["count"] == 1
