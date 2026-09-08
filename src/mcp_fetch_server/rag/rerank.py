"""Cross-encoder reranking.

Retrieval fuses two cheap signals that score every passage without ever
comparing it to the query directly. A cross-encoder does exactly that: it
reads the query and one passage together and scores the pair. It is far too
slow to run over a whole collection, which is why it runs last, over the
fifty candidates retrieval already narrowed to.

The model runs on CPU through ONNX, deliberately: it keeps the GPU free for
the embedding and chat models, which on an 8 GB card do not have room to
spare. Measured throughput is high enough that this is not the bottleneck.

Reranking is optional. Without the ``rerank`` extra installed, retrieval still
works and simply returns the fused order, so a missing dependency costs
quality rather than availability.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

from mcp_fetch_server.config import settings
from mcp_fetch_server.rag.store import SearchHit

logger = logging.getLogger(__name__)

# Cross-encoders truncate long inputs anyway, and a passage's relevance is
# usually decided by its opening; trimming keeps scoring fast.
MAX_PASSAGE_CHARS = 2000


class RerankUnavailable(Exception):
    """Raised when no cross-encoder can be loaded."""


def passage_text(hit: SearchHit) -> str:
    """What the cross-encoder scores: heading context plus the passage."""
    parts = [part for part in (hit.title, hit.heading_trail) if part]
    body = hit.text[:MAX_PASSAGE_CHARS]
    return f"{' > '.join(parts)}\n\n{body}" if parts else body


class CrossEncoderReranker:
    """Lazily loaded ONNX cross-encoder, shared across requests."""

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name or settings.rag_reranker_model
        self._model: Any = None
        self._lock = asyncio.Lock()
        self._unavailable_reason: str | None = None

    def _load(self) -> Any:
        """Blocking load, called in a worker thread."""
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
        except ImportError as exc:
            raise RerankUnavailable(
                "fastembed is not installed. Install reranking with "
                "`uv sync --extra rerank`, or set FETCH_RAG_RERANK_ENABLED=false."
            ) from exc

        try:
            return TextCrossEncoder(model_name=self.model_name)
        except Exception as exc:
            raise RerankUnavailable(
                f"Could not load reranker {self.model_name!r}: {exc}"
            ) from exc

    async def model(self) -> Any:
        if self._unavailable_reason is not None:
            raise RerankUnavailable(self._unavailable_reason)
        if self._model is not None:
            return self._model

        async with self._lock:
            if self._model is None:
                loop = asyncio.get_running_loop()
                try:
                    # First use downloads the model, which takes a while;
                    # doing it on the event loop would stall the whole server.
                    self._model = await loop.run_in_executor(None, self._load)
                except RerankUnavailable as exc:
                    # Remember the failure: retrying a missing dependency on
                    # every single query would add a slow no-op to each one.
                    self._unavailable_reason = str(exc)
                    raise
        return self._model

    async def score(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        model = await self.model()
        loop = asyncio.get_running_loop()
        scores = await loop.run_in_executor(
            None, lambda: list(model.rerank(query, list(passages)))
        )
        return [float(score) for score in scores]

    async def rerank(
        self, query: str, hits: Sequence[SearchHit], *, top_k: int | None = None
    ) -> list[SearchHit]:
        """Reorder hits by cross-encoder score, highest first."""
        if not hits:
            return []

        scores = await self.score(query, [passage_text(hit) for hit in hits])
        for hit, score in zip(hits, scores, strict=True):
            hit.score = score

        ordered = sorted(hits, key=lambda hit: hit.score, reverse=True)
        return ordered[:top_k] if top_k else ordered


_reranker: CrossEncoderReranker | None = None


def get_reranker() -> CrossEncoderReranker:
    global _reranker
    if _reranker is None:
        _reranker = CrossEncoderReranker()
    return _reranker


def reset_reranker() -> None:
    """Drop the shared reranker (used by tests and config reloads)."""
    global _reranker
    _reranker = None


async def maybe_rerank(
    query: str,
    hits: Sequence[SearchHit],
    *,
    top_k: int | None = None,
    reranker: CrossEncoderReranker | None = None,
) -> tuple[list[SearchHit], bool]:
    """Rerank when possible, otherwise return the fused order unchanged.

    Returns ``(hits, reranked)``. A missing or broken reranker degrades
    quality, never availability: search must keep working.
    """
    if not settings.rag_rerank_enabled or not hits:
        return list(hits), False

    active = reranker or get_reranker()
    try:
        return await active.rerank(query, hits, top_k=top_k), True
    except RerankUnavailable as exc:
        logger.info("Reranking unavailable, using fused order: %s", exc)
        return list(hits), False
    except Exception as exc:
        logger.warning("Reranking failed, using fused order: %s", exc)
        return list(hits), False
