"""The ingestion pipeline: files in, catalogued and chunked documents out.

Parsing is CPU-bound and runs in a thread pool; catalog writes are serialised
through the calling coroutine so SQLite only ever sees one writer.

Ingestion is incremental. Each source file is hashed, and a file whose bytes
have not changed since the last run is skipped, so re-running over a large
corpus costs a directory walk rather than a re-parse. ``--reingest`` forces
the work anyway.

At this stage a document is parsed, stored and chunked. Enrichment and
embedding are separate passes, so a corpus can be built and inspected before
any GPU time is spent on it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from mcp_fetch_server.rag.catalog import (
    Catalog,
    DocumentRecord,
    make_doc_id,
    mint_url,
)
from mcp_fetch_server.rag.chunking import chunk_document
from mcp_fetch_server.rag.loaders import LoaderError, is_supported, load_document

logger = logging.getLogger(__name__)

IngestStatus = Literal["ingested", "skipped", "failed"]

# Directories that are never worth walking into.
_SKIP_DIRECTORIES = frozenset(
    {".git", ".svn", ".hg", "__pycache__", "node_modules", ".venv", "venv", ".tox", ".mypy_cache"}
)


@dataclass(slots=True)
class IngestResult:
    path: Path
    status: IngestStatus
    doc_id: str | None = None
    url: str | None = None
    title: str | None = None
    chunks: int = 0
    characters: int = 0
    error: str | None = None
    duration: float = 0.0


@dataclass(slots=True)
class IngestSummary:
    results: list[IngestResult] = field(default_factory=list)
    duration: float = 0.0

    @property
    def ingested(self) -> int:
        return sum(1 for result in self.results if result.status == "ingested")

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
    def failures(self) -> list[IngestResult]:
        return [result for result in self.results if result.status == "failed"]

    def as_dict(self) -> dict[str, object]:
        return {
            "ingested": self.ingested,
            "skipped": self.skipped,
            "failed": self.failed,
            "chunks": self.total_chunks,
            "duration_seconds": round(self.duration, 2),
            "documents": [
                {
                    "path": str(result.path),
                    "status": result.status,
                    "url": result.url,
                    "title": result.title,
                    "chunks": result.chunks,
                    "error": result.error,
                    "duration_seconds": round(result.duration, 3),
                }
                for result in self.results
            ],
        }

    def render(self) -> str:
        lines = [
            f"Ingested {self.ingested}, skipped {self.skipped}, failed {self.failed} "
            f"({self.total_chunks} chunks) in {self.duration:.1f}s"
        ]
        if self.failures:
            lines.append("")
            lines.append("Failures:")
            lines.extend(f"  {result.path.name}: {result.error}" for result in self.failures)
        return "\n".join(lines)


def discover_files(paths: Iterable[Path | str]) -> list[Path]:
    """Expand files and directories into a sorted list of supported files."""
    found: list[Path] = []
    seen: set[Path] = set()

    for entry in paths:
        path = Path(entry).expanduser()
        if path.is_file():
            if is_supported(path):
                resolved = path.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    found.append(resolved)
            continue
        if not path.is_dir():
            logger.warning("Skipping missing path: %s", path)
            continue

        for root, directories, filenames in os.walk(path):
            directories[:] = [
                name
                for name in directories
                if name not in _SKIP_DIRECTORIES and not name.startswith(".")
            ]
            for filename in filenames:
                candidate = Path(root) / filename
                if not is_supported(candidate):
                    continue
                resolved = candidate.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    found.append(resolved)

    return sorted(found)


def _hash_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(slots=True)
class _ParsedDocument:
    """What the worker thread produces, ready for a catalog write."""

    path: Path
    file_hash: str
    url: str
    doc_id: str
    title: str
    markdown: str
    doctype: str
    language: str | None
    page_count: int | None
    byte_size: int
    metadata: dict[str, object]
    chunks: list
    links: list[tuple[str, str]]


def _parse(path: Path, file_hash: str, base_url: str | None = None) -> _ParsedDocument:
    """Load, render and chunk one file. Runs in a worker thread.

    Arguments are positional because ``run_in_executor`` cannot pass keywords.
    """
    document = load_document(path)
    title = document.best_title()
    url = mint_url(title, path, base_url=base_url)
    doc_id = make_doc_id(url)
    markdown = document.to_markdown()
    chunks = chunk_document(document, doc_id)

    raw_links = document.metadata.pop("links", []) or []
    links = [(str(href), str(anchor)) for href, anchor in raw_links]

    return _ParsedDocument(
        path=path,
        file_hash=file_hash,
        url=url,
        doc_id=doc_id,
        title=title,
        markdown=markdown,
        doctype=document.doctype,
        language=document.language,
        page_count=document.page_count,
        byte_size=int(document.metadata.get("byte_size") or path.stat().st_size),
        metadata=dict(document.metadata),
        chunks=chunks,
        links=links,
    )


def _store(catalog: Catalog, parsed: _ParsedDocument) -> int:
    """Write a parsed document into the catalog. Runs on the calling task."""
    _, blob_path = catalog.store_blob(parsed.markdown)

    existing = catalog.get_document(parsed.doc_id)
    record = DocumentRecord(
        doc_id=parsed.doc_id,
        url=parsed.url,
        content_hash=parsed.file_hash,
        doctype=parsed.doctype,
        blob_path=blob_path,
        source_path=str(parsed.path),
        title=parsed.title,
        language=parsed.language,
        byte_size=parsed.byte_size,
        page_count=parsed.page_count,
        char_count=len(parsed.markdown),
        chunk_count=len(parsed.chunks),
        status="ingested",
        ingested_at=existing.ingested_at if existing else time.time(),
        meta=parsed.metadata,
    )
    # Re-ingesting replaces the text, so any enrichment and embeddings derived
    # from the old text are stale. Clear them rather than leave a summary that
    # describes a document that no longer exists.
    if existing and existing.content_hash != parsed.file_hash:
        record.summary = None
        record.enriched_at = None
        record.embedded_at = None
    elif existing:
        record.summary = existing.summary
        record.categories = existing.categories
        record.tags = existing.tags
        record.entities = existing.entities
        record.questions = existing.questions
        record.published_at = existing.published_at
        record.enriched_at = existing.enriched_at

    catalog.upsert_document(record)
    catalog.replace_chunks(parsed.doc_id, parsed.chunks)
    if parsed.links:
        catalog.replace_links(parsed.doc_id, parsed.links)
    return len(parsed.chunks)


async def ingest_paths(
    paths: Sequence[Path | str],
    *,
    catalog: Catalog | None = None,
    reingest: bool = False,
    workers: int | None = None,
    base_url: str | None = None,
    limit: int | None = None,
    on_result: Callable[[IngestResult], None] | None = None,
) -> IngestSummary:
    """Ingest every supported file under ``paths``."""
    started = time.perf_counter()
    files = discover_files(paths)
    if limit is not None:
        files = files[:limit]

    summary = IngestSummary()
    if not files:
        summary.duration = time.perf_counter() - started
        return summary

    owns_catalog = catalog is None
    active = catalog or Catalog()
    active.open()

    worker_count = workers or min(16, (os.cpu_count() or 4))
    semaphore = asyncio.Semaphore(max(1, worker_count))
    loop = asyncio.get_running_loop()
    results_lock = asyncio.Lock()

    async def process(path: Path) -> None:
        file_started = time.perf_counter()
        result = IngestResult(path=path, status="failed")
        try:
            async with semaphore:
                file_hash = await loop.run_in_executor(None, _hash_file, path)

                if not reingest:
                    existing = active.get_document_by_source(path)
                    if existing is not None and existing.content_hash == file_hash:
                        result.status = "skipped"
                        result.doc_id = existing.doc_id
                        result.url = existing.url
                        result.title = existing.title
                        result.chunks = existing.chunk_count
                        return

                parsed = await loop.run_in_executor(None, _parse, path, file_hash, base_url)

            # Catalog writes stay on the event loop: one writer, no races.
            chunk_count = _store(active, parsed)
            result.status = "ingested"
            result.doc_id = parsed.doc_id
            result.url = parsed.url
            result.title = parsed.title
            result.chunks = chunk_count
            result.characters = len(parsed.markdown)
        except LoaderError as exc:
            result.error = str(exc)
        except Exception as exc:  # one bad file must not stop the run
            logger.exception("Unexpected failure ingesting %s", path)
            result.error = f"{type(exc).__name__}: {exc}"
        finally:
            result.duration = time.perf_counter() - file_started
            async with results_lock:
                summary.results.append(result)
            if on_result is not None:
                on_result(result)

    try:
        await asyncio.gather(*(process(path) for path in files))
    finally:
        if owns_catalog:
            active.close()

    summary.results.sort(key=lambda item: str(item.path))
    summary.duration = time.perf_counter() - started
    return summary
