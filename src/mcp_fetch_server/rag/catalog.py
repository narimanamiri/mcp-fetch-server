"""SQLite catalog and content-addressed blob store for the offline corpus.

The catalog is the source of truth. Qdrant holds vectors and can be rebuilt
from here without re-parsing a single source file, which matters because
re-parsing a large corpus costs days while re-embedding costs hours.

Layout under ``FETCH_CORPUS_DATA_DIR``::

    catalog.db          documents, chunks, links
    objects/ab/abcd..md canonical document text, addressed by content hash

Chunk text is stored alongside its character span, because they are not the
same thing: the span says where the passage came from in the document (used
for citations and page anchors), while the stored text is what was actually
embedded, including the heading prefix and the overlap carried from the
previous chunk.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from mcp_fetch_server.config import settings
from mcp_fetch_server.rag.documents import Chunk, slugify

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id        TEXT PRIMARY KEY,
    url           TEXT NOT NULL UNIQUE,
    source_path   TEXT,
    content_hash  TEXT NOT NULL,
    doctype       TEXT NOT NULL,
    title         TEXT,
    language      TEXT,
    byte_size     INTEGER,
    page_count    INTEGER,
    char_count    INTEGER,
    chunk_count   INTEGER DEFAULT 0,
    blob_path     TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'ingested',
    error         TEXT,
    ingested_at   REAL NOT NULL,
    updated_at    REAL NOT NULL,
    -- Enrichment (populated later by the local model)
    summary       TEXT,
    categories    TEXT,
    tags          TEXT,
    entities      TEXT,
    questions     TEXT,
    published_at  TEXT,
    enriched_at   REAL,
    embedded_at   REAL,
    meta          TEXT
);

CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_path);
CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_documents_language ON documents(language);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id       TEXT PRIMARY KEY,
    doc_id         TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    chunk_index    INTEGER NOT NULL,
    text           TEXT NOT NULL,
    heading_path   TEXT,
    page_start     INTEGER,
    page_end       INTEGER,
    char_start     INTEGER NOT NULL,
    char_end       INTEGER NOT NULL,
    token_estimate INTEGER NOT NULL,
    UNIQUE(doc_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

CREATE TABLE IF NOT EXISTS links (
    from_doc_id TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    to_url      TEXT NOT NULL,
    anchor_text TEXT
);

CREATE INDEX IF NOT EXISTS idx_links_from ON links(from_doc_id);
CREATE INDEX IF NOT EXISTS idx_links_to ON links(to_url);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class CatalogError(Exception):
    """Raised when the catalog cannot be read or written."""


def content_hash(data: bytes | str) -> str:
    payload = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(payload).hexdigest()


def make_doc_id(url: str) -> str:
    """Stable id derived from the URL, so re-ingesting keeps the same id."""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def mint_url(title: str, source_path: str | Path, *, base_url: str | None = None) -> str:
    """Mint a stable, readable URL for a local document.

    The slug is derived from the title and disambiguated with a short hash of
    the source path, so two files called "report.pdf" in different folders do
    not collide, and re-ingesting the same file lands on the same URL.
    """
    base = (base_url or settings.site_base_url).rstrip("/")
    digest = hashlib.sha1(str(source_path).encode("utf-8")).hexdigest()[:8]
    slug = slugify(title)
    return f"{base}/doc/{quote(slug, safe='')}-{digest}"


@dataclass(slots=True)
class DocumentRecord:
    doc_id: str
    url: str
    content_hash: str
    doctype: str
    blob_path: str
    source_path: str | None = None
    title: str | None = None
    language: str | None = None
    byte_size: int | None = None
    page_count: int | None = None
    char_count: int | None = None
    chunk_count: int = 0
    status: str = "ingested"
    error: str | None = None
    ingested_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    summary: str | None = None
    categories: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    published_at: str | None = None
    enriched_at: float | None = None
    embedded_at: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)


def _loads_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except ValueError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _loads_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _row_to_document(row: sqlite3.Row) -> DocumentRecord:
    return DocumentRecord(
        doc_id=row["doc_id"],
        url=row["url"],
        content_hash=row["content_hash"],
        doctype=row["doctype"],
        blob_path=row["blob_path"],
        source_path=row["source_path"],
        title=row["title"],
        language=row["language"],
        byte_size=row["byte_size"],
        page_count=row["page_count"],
        char_count=row["char_count"],
        chunk_count=row["chunk_count"] or 0,
        status=row["status"],
        error=row["error"],
        ingested_at=row["ingested_at"],
        updated_at=row["updated_at"],
        summary=row["summary"],
        categories=_loads_list(row["categories"]),
        tags=_loads_list(row["tags"]),
        entities=_loads_list(row["entities"]),
        questions=_loads_list(row["questions"]),
        published_at=row["published_at"],
        enriched_at=row["enriched_at"],
        embedded_at=row["embedded_at"],
        meta=_loads_dict(row["meta"]),
    )


def _row_to_chunk(row: sqlite3.Row) -> Chunk:
    return Chunk(
        doc_id=row["doc_id"],
        chunk_index=row["chunk_index"],
        text=row["text"],
        heading_path=_loads_list(row["heading_path"]),
        page_start=row["page_start"],
        page_end=row["page_end"],
        char_start=row["char_start"],
        char_end=row["char_end"],
        token_estimate=row["token_estimate"],
    )


class Catalog:
    """SQLite-backed document catalog with a content-addressed blob store."""

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir is not None else settings.corpus_dir
        self.objects_dir = self.data_dir / "objects"
        self.db_path = self.data_dir / "catalog.db"
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None

    # -- lifecycle --------------------------------------------------------

    def open(self) -> Catalog:
        if self._connection is not None:
            return self
        try:
            self.objects_dir.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                isolation_level=None,
            )
        except (OSError, sqlite3.Error) as exc:
            raise CatalogError(f"Could not open catalog at {self.db_path}: {exc}") from exc

        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(_SCHEMA)
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self._connection = connection
        return self

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def __enter__(self) -> Catalog:
        return self.open()

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            self.open()
        assert self._connection is not None
        return self._connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self.connection
            connection.execute("BEGIN")
            try:
                yield connection
            except Exception:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

    # -- blob store -------------------------------------------------------

    def store_blob(self, text: str) -> tuple[str, str]:
        """Write canonical text into the blob store, returning (hash, path).

        Content addressing makes writes idempotent: re-ingesting an unchanged
        document rewrites nothing, and two identical documents share a blob.
        """
        digest = content_hash(text)
        relative = f"{digest[:2]}/{digest}.md"
        target = self.objects_dir / relative
        if not target.exists():
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                # Write to a temporary file and move it, so a crash mid-write
                # cannot leave a truncated blob under a hash that claims to
                # describe complete content.
                temporary = target.with_suffix(".tmp")
                temporary.write_text(text, encoding="utf-8")
                temporary.replace(target)
            except OSError as exc:
                raise CatalogError(f"Could not write blob {relative}: {exc}") from exc
        return digest, relative

    def read_blob(self, blob_path: str) -> str:
        target = self.objects_dir / blob_path
        try:
            return target.read_text(encoding="utf-8")
        except OSError as exc:
            raise CatalogError(f"Could not read blob {blob_path}: {exc}") from exc

    def blob_exists(self, blob_path: str) -> bool:
        return (self.objects_dir / blob_path).is_file()

    # -- documents --------------------------------------------------------

    def upsert_document(self, record: DocumentRecord) -> None:
        record.updated_at = time.time()
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO documents (
                    doc_id, url, source_path, content_hash, doctype, title, language,
                    byte_size, page_count, char_count, chunk_count, blob_path, status,
                    error, ingested_at, updated_at, summary, categories, tags, entities,
                    questions, published_at, enriched_at, embedded_at, meta
                ) VALUES (
                    :doc_id, :url, :source_path, :content_hash, :doctype, :title, :language,
                    :byte_size, :page_count, :char_count, :chunk_count, :blob_path, :status,
                    :error, :ingested_at, :updated_at, :summary, :categories, :tags, :entities,
                    :questions, :published_at, :enriched_at, :embedded_at, :meta
                )
                ON CONFLICT(doc_id) DO UPDATE SET
                    url=excluded.url,
                    source_path=excluded.source_path,
                    content_hash=excluded.content_hash,
                    doctype=excluded.doctype,
                    title=excluded.title,
                    language=excluded.language,
                    byte_size=excluded.byte_size,
                    page_count=excluded.page_count,
                    char_count=excluded.char_count,
                    chunk_count=excluded.chunk_count,
                    blob_path=excluded.blob_path,
                    status=excluded.status,
                    error=excluded.error,
                    updated_at=excluded.updated_at,
                    summary=excluded.summary,
                    categories=excluded.categories,
                    tags=excluded.tags,
                    entities=excluded.entities,
                    questions=excluded.questions,
                    published_at=excluded.published_at,
                    enriched_at=excluded.enriched_at,
                    embedded_at=excluded.embedded_at,
                    meta=excluded.meta
                """,
                {
                    "doc_id": record.doc_id,
                    "url": record.url,
                    "source_path": record.source_path,
                    "content_hash": record.content_hash,
                    "doctype": record.doctype,
                    "title": record.title,
                    "language": record.language,
                    "byte_size": record.byte_size,
                    "page_count": record.page_count,
                    "char_count": record.char_count,
                    "chunk_count": record.chunk_count,
                    "blob_path": record.blob_path,
                    "status": record.status,
                    "error": record.error,
                    "ingested_at": record.ingested_at,
                    "updated_at": record.updated_at,
                    "summary": record.summary,
                    "categories": _dumps(record.categories),
                    "tags": _dumps(record.tags),
                    "entities": _dumps(record.entities),
                    "questions": _dumps(record.questions),
                    "published_at": record.published_at,
                    "enriched_at": record.enriched_at,
                    "embedded_at": record.embedded_at,
                    "meta": _dumps(record.meta),
                },
            )

    def get_document(self, doc_id: str) -> DocumentRecord | None:
        row = self.connection.execute(
            "SELECT * FROM documents WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        return _row_to_document(row) if row else None

    def get_document_by_url(self, url: str) -> DocumentRecord | None:
        row = self.connection.execute("SELECT * FROM documents WHERE url = ?", (url,)).fetchone()
        return _row_to_document(row) if row else None

    def get_document_by_source(self, source_path: str | Path) -> DocumentRecord | None:
        row = self.connection.execute(
            "SELECT * FROM documents WHERE source_path = ?", (str(source_path),)
        ).fetchone()
        return _row_to_document(row) if row else None

    def list_documents(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        language: str | None = None,
    ) -> list[DocumentRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if language:
            clauses.append("language = ?")
            params.append(language)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])
        rows = self.connection.execute(
            f"SELECT * FROM documents {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [_row_to_document(row) for row in rows]

    def iter_documents(self, *, status: str | None = None) -> Iterator[DocumentRecord]:
        query = "SELECT * FROM documents"
        params: tuple[Any, ...] = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        for row in self.connection.execute(f"{query} ORDER BY ingested_at", params):
            yield _row_to_document(row)

    def delete_document(self, doc_id: str) -> bool:
        """Remove a document and its chunks. Blobs are left for the GC pass."""
        with self._lock:
            cursor = self.connection.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
            return cursor.rowcount > 0

    # -- chunks -----------------------------------------------------------

    def replace_chunks(self, doc_id: str, chunks: Iterable[Chunk]) -> int:
        """Atomically swap a document's chunks for a new set."""
        rows = [
            (
                chunk.chunk_id,
                doc_id,
                chunk.chunk_index,
                chunk.text,
                _dumps(chunk.heading_path),
                chunk.page_start,
                chunk.page_end,
                chunk.char_start,
                chunk.char_end,
                chunk.token_estimate,
            )
            for chunk in chunks
        ]
        with self.transaction() as connection:
            connection.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            connection.executemany(
                "INSERT INTO chunks (chunk_id, doc_id, chunk_index, text, heading_path,"
                " page_start, page_end, char_start, char_end, token_estimate)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            connection.execute(
                "UPDATE documents SET chunk_count = ? WHERE doc_id = ?", (len(rows), doc_id)
            )
        return len(rows)

    def get_chunks(self, doc_id: str) -> list[Chunk]:
        rows = self.connection.execute(
            "SELECT * FROM chunks WHERE doc_id = ? ORDER BY chunk_index", (doc_id,)
        ).fetchall()
        return [_row_to_chunk(row) for row in rows]

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        row = self.connection.execute(
            "SELECT * FROM chunks WHERE chunk_id = ?", (chunk_id,)
        ).fetchone()
        return _row_to_chunk(row) if row else None

    def get_chunks_by_ids(self, chunk_ids: list[str]) -> dict[str, Chunk]:
        """Batch lookup, used to hydrate search results in one query."""
        if not chunk_ids:
            return {}
        placeholders = ",".join("?" for _ in chunk_ids)
        rows = self.connection.execute(
            f"SELECT * FROM chunks WHERE chunk_id IN ({placeholders})", chunk_ids
        ).fetchall()
        return {row["chunk_id"]: _row_to_chunk(row) for row in rows}

    def neighbour_chunks(self, doc_id: str, chunk_index: int, *, window: int = 1) -> list[Chunk]:
        """Chunks either side of one, for small-to-big context expansion."""
        rows = self.connection.execute(
            "SELECT * FROM chunks WHERE doc_id = ? AND chunk_index BETWEEN ? AND ?"
            " ORDER BY chunk_index",
            (doc_id, chunk_index - window, chunk_index + window),
        ).fetchall()
        return [_row_to_chunk(row) for row in rows]

    # -- links ------------------------------------------------------------

    def replace_links(self, doc_id: str, links: Iterable[tuple[str, str]]) -> int:
        rows = [(doc_id, url, anchor) for url, anchor in links]
        with self.transaction() as connection:
            connection.execute("DELETE FROM links WHERE from_doc_id = ?", (doc_id,))
            connection.executemany(
                "INSERT INTO links (from_doc_id, to_url, anchor_text) VALUES (?, ?, ?)", rows
            )
        return len(rows)

    def outbound_links(self, doc_id: str) -> list[tuple[str, str]]:
        rows = self.connection.execute(
            "SELECT to_url, anchor_text FROM links WHERE from_doc_id = ?", (doc_id,)
        ).fetchall()
        return [(row["to_url"], row["anchor_text"] or "") for row in rows]

    def inbound_links(self, url: str) -> list[str]:
        rows = self.connection.execute(
            "SELECT DISTINCT from_doc_id FROM links WHERE to_url = ?", (url,)
        ).fetchall()
        return [row["from_doc_id"] for row in rows]

    # -- housekeeping -----------------------------------------------------

    def stats(self) -> dict[str, Any]:
        connection = self.connection
        documents = connection.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
        chunks = connection.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        characters = (
            connection.execute(
                "SELECT COALESCE(SUM(char_count), 0) AS n FROM documents"
            ).fetchone()["n"]
            or 0
        )
        by_doctype = {
            row["doctype"]: row["n"]
            for row in connection.execute(
                "SELECT doctype, COUNT(*) AS n FROM documents GROUP BY doctype ORDER BY n DESC"
            )
        }
        by_language = {
            row["language"] or "und": row["n"]
            for row in connection.execute(
                "SELECT language, COUNT(*) AS n FROM documents GROUP BY language ORDER BY n DESC"
            )
        }
        by_status = {
            row["status"]: row["n"]
            for row in connection.execute(
                "SELECT status, COUNT(*) AS n FROM documents GROUP BY status"
            )
        }
        return {
            "documents": documents,
            "chunks": chunks,
            "characters": characters,
            "by_doctype": by_doctype,
            "by_language": by_language,
            "by_status": by_status,
            "data_dir": str(self.data_dir),
            "db_bytes": self.db_path.stat().st_size if self.db_path.exists() else 0,
        }

    def collect_garbage(self) -> int:
        """Delete blobs no document references any more. Returns the count."""
        referenced = {
            row["blob_path"] for row in self.connection.execute("SELECT blob_path FROM documents")
        }
        removed = 0
        if not self.objects_dir.exists():
            return 0
        for path in self.objects_dir.rglob("*.md"):
            relative = path.relative_to(self.objects_dir).as_posix()
            if relative not in referenced:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
        return removed
