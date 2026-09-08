"""The embedding pass: chunks in the catalog become vectors in Qdrant.

Kept separate from ingestion so a corpus can be parsed, inspected and fixed
before any GPU time is spent on it, and so re-embedding after a model change
does not mean re-parsing every source file.

Documents are embedded one at a time and marked as they go, so an interrupted
run resumes where it stopped rather than starting over.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mcp_fetch_server.rag.catalog import Catalog, DocumentRecord
from mcp_fetch_server.rag.documents import Chunk
from mcp_fetch_server.rag.llm import LLMError, LocalLLM, get_llm
from mcp_fetch_server.rag.sparse import SparseVector, encode
from mcp_fetch_server.rag.store import StoreError, VectorStore

logger = logging.getLogger(__name__)


def embedding_inputs(chunk: Chunk, record: DocumentRecord) -> str:
    """Text handed to the embedding model for one chunk.

    The heading path gives a passage from deep inside a document the context
    that says what it is about. The document title is added for the same
    reason: "the second phase" means nothing without knowing which document it
    belongs to.
    """
    parts: list[str] = []
    if record.title:
        parts.append(record.title)
    if chunk.heading_path:
        parts.append(chunk.heading_trail)
    parts.append(chunk.text)
    return "\n\n".join(parts)


def sparse_inputs(chunk: Chunk, record: DocumentRecord) -> SparseVector:
    """Lexical vector for a chunk, including its questions on the first chunk.

    Hypothetical questions from enrichment are folded into the document's
    first chunk so a query phrased as a question has something to match
    lexically, not only semantically.
    """
    text = embedding_inputs(chunk, record)
    if chunk.chunk_index == 0 and record.questions:
        text = f"{text}\n\n{' '.join(record.questions)}"
    if record.tags:
        text = f"{text}\n\n{' '.join(record.tags)}"
    return encode(text)


@dataclass(slots=True)
class EmbedResult:
    doc_id: str
    title: str | None
    status: str  # "embedded" | "skipped" | "failed"
    chunks: int = 0
    error: str | None = None
    duration: float = 0.0


@dataclass(slots=True)
class EmbedSummary:
    results: list[EmbedResult] = field(default_factory=list)
    duration: float = 0.0
    dimension: int | None = None

    @property
    def embedded(self) -> int:
        return sum(1 for result in self.results if result.status == "embedded")

    @property
    def skipped(self) -> int:
        return sum(1 for result in self.results if result.status == "skipped")

    @property
    def failed(self) -> int:
        return sum(1 for result in self.results if result.status == "failed")

    @property
    def total_chunks(self) -> int:
        return sum(result.chunks for result in self.results)

    @property
    def failures(self) -> list[EmbedResult]:
        return [result for result in self.results if result.status == "failed"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "embedded": self.embedded,
            "skipped": self.skipped,
            "failed": self.failed,
            "chunks": self.total_chunks,
            "dimension": self.dimension,
            "duration_seconds": round(self.duration, 2),
        }

    def render(self) -> str:
        rate = ""
        if self.total_chunks and self.duration > 0:
            rate = f", {self.total_chunks / self.duration:.0f} chunks/s"
        lines = [
            f"Embedded {self.embedded}, skipped {self.skipped}, failed {self.failed} "
            f"({self.total_chunks} chunks) in {self.duration:.1f}s{rate}"
        ]
        if self.failures:
            lines.append("")
            lines.append("Failures:")
            lines.extend(
                f"  {result.title or result.doc_id}: {result.error}" for result in self.failures
            )
        return "\n".join(lines)


async def embed_document(
    record: DocumentRecord,
    chunks: list[Chunk],
    *,
    llm: LocalLLM,
    store: VectorStore,
) -> int:
    """Embed and index one document's chunks. Returns the count written."""
    if not chunks:
        return 0

    dense = await llm.embed([embedding_inputs(chunk, record) for chunk in chunks])
    sparse = [sparse_inputs(chunk, record) for chunk in chunks]

    # Drop any previous vectors first: a re-chunked document can have fewer
    # chunks than before, and the leftovers would keep being retrievable.
    store.delete_document(record.doc_id)
    return store.upsert_chunks(chunks, dense, sparse, record)


async def run_embedding(
    *,
    catalog: Catalog,
    store: VectorStore | None = None,
    llm: LocalLLM | None = None,
    reembed: bool = False,
    recreate: bool = False,
    limit: int | None = None,
    on_result: Callable[[EmbedResult], None] | None = None,
) -> EmbedSummary:
    """Embed every document that has not been embedded since it last changed."""
    started = time.perf_counter()
    client = llm or get_llm()
    active_store = store or VectorStore()
    summary = EmbedSummary()

    try:
        dimension = await client.embedding_dimension()
    except LLMError as exc:
        raise LLMError(
            f"Could not determine the embedding dimension: {exc}. "
            "Run `mcp-fetch-server doctor` to check the model."
        ) from exc

    summary.dimension = dimension
    active_store.ensure_collection(dimension=dimension, recreate=recreate)

    pending: list[DocumentRecord] = []
    for record in catalog.iter_documents():
        if recreate or reembed or record.embedded_at is None:
            pending.append(record)
        elif record.updated_at > record.embedded_at:
            # The document changed after it was last embedded.
            pending.append(record)

    if limit is not None:
        pending = pending[:limit]

    if not pending:
        summary.duration = time.perf_counter() - started
        return summary

    # Embedding is one GPU-bound queue; the LLM client caps concurrency, and
    # running documents sequentially keeps memory predictable on a small card.
    for record in pending:
        document_started = time.perf_counter()
        result = EmbedResult(doc_id=record.doc_id, title=record.title, status="failed")
        try:
            chunks = catalog.get_chunks(record.doc_id)
            written = await embed_document(record, chunks, llm=client, store=active_store)
            record.embedded_at = time.time()
            record.status = "embedded"
            catalog.upsert_document(record)
            result.status = "embedded"
            result.chunks = written
        except (LLMError, StoreError) as exc:
            result.error = str(exc)
        except Exception as exc:
            logger.exception("Unexpected failure embedding %s", record.doc_id)
            result.error = f"{type(exc).__name__}: {exc}"
        finally:
            result.duration = time.perf_counter() - document_started
            summary.results.append(result)
            if on_result is not None:
                on_result(result)
        # Yield so a caller can cancel between documents.
        await asyncio.sleep(0)

    summary.duration = time.perf_counter() - started
    return summary
