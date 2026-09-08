"""Query expansion.

A user's question and the passage that answers it often share almost no words.
The question is short, uses their vocabulary, and may be in a different
language from the document. Retrieval over a single phrasing inherits all of
that.

So the local model rewrites the query into a few alternative phrasings, each
is retrieved separately, and the result lists are fused by reciprocal rank.
A passage that several phrasings agree on rises; one that only matched a
single odd wording does not.

This costs a model call, so it is opt-in per search rather than always on.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, Field

from mcp_fetch_server.rag.llm import LLMError, LocalLLM, Message, get_llm
from mcp_fetch_server.rag.store import SearchHit

logger = logging.getLogger(__name__)

MAX_VARIANTS = 4

SYSTEM = (
    "You rewrite search queries to improve document retrieval. You produce "
    "alternative phrasings that a document answering the question would "
    "plausibly use. You never answer the question itself. Reply with JSON only."
)


class QueryVariants(BaseModel):
    queries: list[str] = Field(default_factory=list)


def variants_schema(count: int) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "queries": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": count,
            }
        },
        "required": ["queries"],
    }


def _clean(query: str, variants: Sequence[str], limit: int) -> list[str]:
    """Keep the original first, then distinct rewrites."""
    seen = {query.strip().lower()}
    cleaned = [query.strip()]
    for variant in variants:
        text = str(variant).strip()
        key = text.lower()
        if not text or key in seen or len(text) > 400:
            continue
        seen.add(key)
        cleaned.append(text)
        if len(cleaned) >= limit + 1:
            break
    return cleaned


async def expand_query(
    query: str,
    *,
    llm: LocalLLM | None = None,
    count: int = 3,
    languages: Sequence[str] | None = None,
) -> list[str]:
    """Return the original query plus up to ``count`` rewrites.

    Failure is not fatal: if the model is unreachable or answers badly, the
    original query is returned alone and retrieval proceeds as normal.
    """
    cleaned = query.strip()
    if not cleaned:
        return []

    count = max(1, min(count, MAX_VARIANTS))
    client = llm or get_llm()

    instruction = (
        f"Rewrite this search query into {count} alternative phrasings that a "
        "document answering it would likely use. Vary the vocabulary: include "
        "a keyword-style phrasing and a full-sentence phrasing."
    )
    if languages:
        listed = ", ".join(languages)
        instruction += (
            f" The corpus contains documents in: {listed}. Include at least one "
            "phrasing in each of those languages."
        )

    try:
        result = await client.chat_json(
            [
                Message(role="system", content=SYSTEM),
                Message(role="user", content=f"{instruction}\n\nQuery: {cleaned}"),
            ],
            QueryVariants,
            temperature=0.3,
            max_tokens=300,
            json_schema=variants_schema(count),
        )
    except LLMError as exc:
        logger.info("Query expansion unavailable, using the original query: %s", exc)
        return [cleaned]

    return _clean(cleaned, result.queries, count)


def fuse_by_rank(
    result_lists: Sequence[Sequence[SearchHit]], *, k: int = 60
) -> list[SearchHit]:
    """Reciprocal rank fusion across several result lists.

    Scores from different queries are not comparable, but ranks are, which is
    the same reason the dense and sparse arms are fused this way inside a
    single search.
    """
    scores: dict[str, float] = {}
    best: dict[str, SearchHit] = {}

    for hits in result_lists:
        for rank, hit in enumerate(hits):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (k + rank + 1)
            best.setdefault(hit.chunk_id, hit)

    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    fused: list[SearchHit] = []
    for chunk_id, score in ordered:
        hit = best[chunk_id]
        hit.score = score
        fused.append(hit)
    return fused
