"""Qdrant vector store: collection management, upsert and hybrid search.

Two named vectors per chunk. ``dense`` carries meaning and comes from the
embedding model; ``sparse`` carries exact terms and is computed locally (see
``rag.sparse``). A query runs both and fuses the result lists with reciprocal
rank fusion, which needs no score calibration between two incomparable
scoring scales.

The collection stores only what search needs: vectors, filterable metadata and
the chunk id. Chunk text lives in the SQLite catalog, which stays the source
of truth, so the whole collection can be dropped and rebuilt without touching
a single source file. It also keeps the Qdrant payload small, which matters
because payload size drives memory use far more than people expect.

Qdrant can run as a server or, when ``FETCH_QDRANT_URL`` is empty, embedded on
local disk. Embedded mode makes a single-user offline setup work with no
Docker at all.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from qdrant_client import QdrantClient, models

from mcp_fetch_server.config import settings
from mcp_fetch_server.rag.catalog import DocumentRecord
from mcp_fetch_server.rag.documents import Chunk
from mcp_fetch_server.rag.sparse import SparseVector

logger = logging.getLogger(__name__)

DENSE_VECTOR = "dense"
SPARSE_VECTOR = "sparse"

# Chunk ids are strings ("<doc_id>:<index>"), but Qdrant point ids must be an
# unsigned integer or a UUID. The chunk id is carried in the payload and a
# deterministic UUID derived from it is used as the point id, so an upsert of
# the same chunk overwrites rather than duplicates.
_UUID_NAMESPACE_BYTES = b"mcp-fetch-server/chunk"


class StoreError(Exception):
    """Raised when the vector store cannot be reached or updated."""


def point_id(chunk_id: str) -> str:
    """Deterministic UUID for a chunk id."""
    import hashlib
    import uuid

    digest = hashlib.blake2b(
        _UUID_NAMESPACE_BYTES + chunk_id.encode("utf-8"), digest_size=16
    ).digest()
    return str(uuid.UUID(bytes=digest, version=5))


@dataclass(slots=True)
class SearchHit:
    chunk_id: str
    doc_id: str
    score: float
    url: str = ""
    title: str = ""
    heading_path: list[str] = field(default_factory=list)
    page_start: int | None = None
    page_end: int | None = None
    chunk_index: int = 0
    categories: list[str] = field(default_factory=list)
    language: str = ""
    # Filled in by the retriever from the catalog, not stored in Qdrant.
    text: str = ""

    @property
    def heading_trail(self) -> str:
        return " > ".join(self.heading_path)

    @property
    def citation_url(self) -> str:
        """URL with a page anchor when the source has pages."""
        if self.page_start:
            return f"{self.url}#p{self.page_start}"
        return self.url


def chunk_payload(chunk: Chunk, record: DocumentRecord) -> dict[str, Any]:
    """Metadata stored with a vector: enough to filter and cite, no more."""
    return {
        "chunk_id": chunk.chunk_id,
        "doc_id": chunk.doc_id,
        "chunk_index": chunk.chunk_index,
        "url": record.url,
        "title": record.title or "",
        "heading_path": chunk.heading_path,
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
        "language": record.language or "",
        "doctype": record.doctype,
        "categories": record.categories,
        "tags": record.tags,
        "published_at": record.published_at,
    }


def _hit_from_point(point: Any) -> SearchHit:
    payload = point.payload or {}
    return SearchHit(
        chunk_id=str(payload.get("chunk_id") or ""),
        doc_id=str(payload.get("doc_id") or ""),
        score=float(getattr(point, "score", 0.0) or 0.0),
        url=str(payload.get("url") or ""),
        title=str(payload.get("title") or ""),
        heading_path=list(payload.get("heading_path") or []),
        page_start=payload.get("page_start"),
        page_end=payload.get("page_end"),
        chunk_index=int(payload.get("chunk_index") or 0),
        categories=list(payload.get("categories") or []),
        language=str(payload.get("language") or ""),
    )


class VectorStore:
    """Thin wrapper over Qdrant, holding the collection layout in one place."""

    def __init__(
        self,
        *,
        url: str | None = None,
        api_key: str | None = None,
        collection: str | None = None,
        client: QdrantClient | None = None,
        local_path: str | None = None,
    ) -> None:
        self.collection = collection or settings.qdrant_collection
        self._url = settings.qdrant_url if url is None else url
        self._api_key = api_key if api_key is not None else settings.qdrant_api_key
        self._local_path = local_path
        self._client = client

    # -- lifecycle --------------------------------------------------------

    @property
    def client(self) -> QdrantClient:
        if self._client is None:
            try:
                if self._local_path is not None:
                    self._client = QdrantClient(path=self._local_path)
                elif self._url.strip():
                    self._client = QdrantClient(
                        url=self._url.strip(), api_key=self._api_key or None
                    )
                else:
                    # No server configured: run embedded on local disk, so a
                    # single-user setup needs no Docker.
                    path = settings.corpus_dir / "qdrant"
                    path.mkdir(parents=True, exist_ok=True)
                    self._client = QdrantClient(path=str(path))
            except Exception as exc:
                raise StoreError(f"Could not connect to Qdrant: {exc}") from exc
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                logger.debug("Qdrant client close failed", exc_info=True)
            self._client = None

    def __enter__(self) -> VectorStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- schema -----------------------------------------------------------

    def exists(self) -> bool:
        try:
            return self.client.collection_exists(self.collection)
        except Exception as exc:
            raise StoreError(f"Could not query Qdrant: {exc}") from exc

    def ensure_collection(self, *, dimension: int, recreate: bool = False) -> bool:
        """Create the collection if needed. Returns True if it was created.

        Raises when an existing collection has a different vector size: that
        means the embedding model changed, and mixing vectors from two models
        in one index produces silently meaningless results.
        """
        try:
            if self.exists():
                if not recreate:
                    self._check_dimension(dimension)
                    return False
                self.client.delete_collection(self.collection)

            self.client.create_collection(
                collection_name=self.collection,
                vectors_config={
                    DENSE_VECTOR: models.VectorParams(
                        size=dimension,
                        distance=models.Distance.COSINE,
                        # int8 quantization cuts vector memory roughly 4x while
                        # keeping the originals on disk for rescoring.
                        quantization_config=models.ScalarQuantization(
                            scalar=models.ScalarQuantizationConfig(
                                type=models.ScalarType.INT8,
                                quantile=0.99,
                                always_ram=True,
                            )
                        ),
                    )
                },
                sparse_vectors_config={
                    SPARSE_VECTOR: models.SparseVectorParams(
                        # Qdrant applies IDF itself, so ingestion stays
                        # stateless and adding documents never invalidates
                        # weights already written.
                        modifier=models.Modifier.IDF,
                    )
                },
                hnsw_config=models.HnswConfigDiff(m=32, ef_construct=256),
            )
        except StoreError:
            raise
        except Exception as exc:
            raise StoreError(f"Could not create collection {self.collection!r}: {exc}") from exc

        self._create_payload_indexes()
        return True

    def _check_dimension(self, dimension: int) -> None:
        actual = self.vector_dimension()
        if actual is not None and actual != dimension:
            raise StoreError(
                f"Collection {self.collection!r} stores {actual}-dimensional vectors but the "
                f"embedding model produces {dimension}. Vectors from two different models "
                "cannot share an index. Re-embed with --recreate, or point "
                "FETCH_QDRANT_COLLECTION at a new collection."
            )

    def vector_dimension(self) -> int | None:
        try:
            info = self.client.get_collection(self.collection)
        except Exception:
            return None
        vectors = info.config.params.vectors
        if isinstance(vectors, dict):
            params = vectors.get(DENSE_VECTOR)
            return int(params.size) if params else None
        return int(vectors.size) if vectors else None

    def _create_payload_indexes(self) -> None:
        """Index the fields used for filtering. Unindexed filters scan."""
        fields: list[tuple[str, models.PayloadSchemaType]] = [
            ("doc_id", models.PayloadSchemaType.KEYWORD),
            ("url", models.PayloadSchemaType.KEYWORD),
            ("language", models.PayloadSchemaType.KEYWORD),
            ("doctype", models.PayloadSchemaType.KEYWORD),
            ("categories", models.PayloadSchemaType.KEYWORD),
            ("tags", models.PayloadSchemaType.KEYWORD),
            ("published_at", models.PayloadSchemaType.KEYWORD),
        ]
        import warnings

        for name, schema in fields:
            try:
                with warnings.catch_warnings():
                    # Embedded Qdrant warns that payload indexes do nothing
                    # there. True, and harmless: filters still work, they just
                    # scan. Not worth a warning per field on every startup.
                    warnings.filterwarnings("ignore", message=".*[Pp]ayload indexes.*")
                    self.client.create_payload_index(
                        collection_name=self.collection, field_name=name, field_schema=schema
                    )
            except Exception:
                # An index that already exists is not an error worth failing on.
                logger.debug("Could not create payload index on %s", name, exc_info=True)

    def drop(self) -> None:
        try:
            if self.exists():
                self.client.delete_collection(self.collection)
        except Exception as exc:
            raise StoreError(f"Could not drop collection: {exc}") from exc

    # -- writes -----------------------------------------------------------

    def upsert_chunks(
        self,
        chunks: Sequence[Chunk],
        dense_vectors: Sequence[Sequence[float]],
        sparse_vectors: Sequence[SparseVector],
        record: DocumentRecord,
    ) -> int:
        if not chunks:
            return 0
        if not (len(chunks) == len(dense_vectors) == len(sparse_vectors)):
            raise StoreError(
                f"Vector count mismatch: {len(chunks)} chunks, {len(dense_vectors)} dense, "
                f"{len(sparse_vectors)} sparse"
            )

        points = [
            models.PointStruct(
                id=point_id(chunk.chunk_id),
                vector={
                    DENSE_VECTOR: list(dense),
                    SPARSE_VECTOR: models.SparseVector(
                        indices=sparse.indices, values=sparse.values
                    ),
                },
                payload=chunk_payload(chunk, record),
            )
            for chunk, dense, sparse in zip(chunks, dense_vectors, sparse_vectors, strict=True)
        ]

        try:
            self.client.upsert(collection_name=self.collection, points=points, wait=True)
        except Exception as exc:
            raise StoreError(f"Could not upsert into {self.collection!r}: {exc}") from exc
        return len(points)

    def delete_document(self, doc_id: str) -> None:
        """Remove every chunk of a document, e.g. before re-embedding it."""
        try:
            self.client.delete(
                collection_name=self.collection,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="doc_id", match=models.MatchValue(value=doc_id)
                            )
                        ]
                    )
                ),
                wait=True,
            )
        except Exception as exc:
            raise StoreError(f"Could not delete chunks for {doc_id}: {exc}") from exc

    # -- search -----------------------------------------------------------

    def search(
        self,
        *,
        dense: Sequence[float] | None,
        sparse: SparseVector | None,
        limit: int,
        query_filter: models.Filter | None = None,
    ) -> list[SearchHit]:
        """Hybrid search: dense and sparse prefetches fused with RRF.

        Reciprocal rank fusion is used rather than a weighted score sum
        because cosine similarity and IDF-weighted term overlap are not on a
        comparable scale; fusing on rank avoids inventing a calibration.
        """
        if dense is None and not sparse:
            return []

        prefetch: list[models.Prefetch] = []
        # Over-fetch each arm so fusion has something to work with: a document
        # ranked 30th on one side and 3rd on the other should still surface.
        arm_limit = max(limit * 2, limit + 20)

        if dense is not None:
            prefetch.append(
                models.Prefetch(query=list(dense), using=DENSE_VECTOR, limit=arm_limit)
            )
        if sparse:
            prefetch.append(
                models.Prefetch(
                    query=models.SparseVector(indices=sparse.indices, values=sparse.values),
                    using=SPARSE_VECTOR,
                    limit=arm_limit,
                )
            )

        try:
            if len(prefetch) == 1:
                # One arm only: query it directly rather than fusing a single
                # list, which some backends reject.
                only = prefetch[0]
                response = self.client.query_points(
                    collection_name=self.collection,
                    query=only.query,
                    using=only.using,
                    limit=limit,
                    query_filter=query_filter,
                    with_payload=True,
                )
            else:
                response = self.client.query_points(
                    collection_name=self.collection,
                    prefetch=prefetch,
                    query=models.FusionQuery(fusion=models.Fusion.RRF),
                    limit=limit,
                    query_filter=query_filter,
                    with_payload=True,
                )
        except Exception as exc:
            raise StoreError(f"Search failed: {exc}") from exc

        return [_hit_from_point(point) for point in response.points]

    def count(self) -> int:
        try:
            if not self.exists():
                return 0
            return int(self.client.count(self.collection, exact=True).count)
        except StoreError:
            raise
        except Exception as exc:
            raise StoreError(f"Could not count points: {exc}") from exc

    def stats(self) -> dict[str, Any]:
        if not self.exists():
            return {"exists": False, "collection": self.collection, "points": 0}
        return {
            "exists": True,
            "collection": self.collection,
            "points": self.count(),
            "dimension": self.vector_dimension(),
            "location": self._url.strip() or str(settings.corpus_dir / "qdrant"),
        }


def build_filter(
    *,
    doc_ids: Iterable[str] | None = None,
    categories: Iterable[str] | None = None,
    languages: Iterable[str] | None = None,
    doctypes: Iterable[str] | None = None,
    tags: Iterable[str] | None = None,
) -> models.Filter | None:
    """Build a Qdrant filter, or None when nothing is being filtered."""
    conditions: list[models.Condition] = []

    for key, values in (
        ("doc_id", doc_ids),
        ("categories", categories),
        ("language", languages),
        ("doctype", doctypes),
        ("tags", tags),
    ):
        cleaned = [str(value) for value in (values or []) if str(value).strip()]
        if cleaned:
            conditions.append(
                models.FieldCondition(key=key, match=models.MatchAny(any=cleaned))
            )

    return models.Filter(must=conditions) if conditions else None
