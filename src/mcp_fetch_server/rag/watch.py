"""Keep the corpus in step with a folder.

Ingestion is already incremental: a file whose bytes have not changed is
skipped, so re-running it over a large corpus costs a directory walk. That
makes a watcher a loop around the existing pipeline rather than anything
clever, and it means a missed tick is harmless — the next scan catches up.

Polling is used rather than filesystem events on purpose. Events are
unreliable across network shares and the Windows/WSL boundary, they arrive
before a large file has finished being written, and they need a debounce layer
that ends up re-implementing the hash check ingestion already does.

By default a scan ingests and then embeds, because a document that is in the
catalog but not in the index is not findable, which is the whole point of
watching a folder.

Deletion is opt-in (``prune``). Removing a file from disk is not obviously an
instruction to drop it from the corpus, and the mistake is not cheap to undo
if the source is gone.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from mcp_fetch_server.rag.catalog import Catalog
from mcp_fetch_server.rag.ingest import discover_files, ingest_paths
from mcp_fetch_server.rag.llm import LLMError
from mcp_fetch_server.rag.store import StoreError, VectorStore

logger = logging.getLogger(__name__)

EventKind = Literal["scan", "ingested", "removed", "embedded", "error"]

MIN_INTERVAL = 2.0


@dataclass(slots=True)
class WatchEvent:
    kind: EventKind
    detail: str
    path: Path | None = None

    def render(self) -> str:
        marker = {
            "scan": ".",
            "ingested": "+",
            "removed": "-",
            "embedded": "*",
            "error": "!",
        }[self.kind]
        return f"{marker} {self.detail}"


@dataclass(slots=True)
class WatchSummary:
    scans: int = 0
    ingested: int = 0
    removed: int = 0
    embedded: int = 0
    errors: int = 0
    started_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scans": self.scans,
            "ingested": self.ingested,
            "removed": self.removed,
            "embedded": self.embedded,
            "errors": self.errors,
            "uptime_seconds": round(time.time() - self.started_at, 1),
        }

    def render(self) -> str:
        return (
            f"{self.scans} scan(s): ingested {self.ingested}, removed {self.removed}, "
            f"embedded {self.embedded}, errors {self.errors} "
            f"over {time.time() - self.started_at:.0f}s"
        )


def _roots(paths: Sequence[Path | str]) -> list[Path]:
    resolved: list[Path] = []
    for entry in paths:
        path = Path(entry).expanduser()
        try:
            resolved.append(path.resolve())
        except OSError:
            continue
    return resolved


def _is_under(path: Path, roots: Sequence[Path]) -> bool:
    for root in roots:
        if path == root:
            return True
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def find_deleted(
    catalog: Catalog, paths: Sequence[Path | str]
) -> list[tuple[str, Path]]:
    """Documents whose source file has disappeared from a watched folder.

    Only documents under the watched roots are considered, so pointing the
    watcher at one folder can never prune documents ingested from elsewhere.
    """
    roots = _roots(paths)
    if not roots:
        return []

    missing: list[tuple[str, Path]] = []
    for record in catalog.iter_documents():
        if not record.source_path:
            continue
        try:
            source = Path(record.source_path).resolve()
        except OSError:
            continue
        if not _is_under(source, roots):
            continue
        if not source.exists():
            missing.append((record.doc_id, source))
    return missing


def prune_document(doc_id: str, catalog: Catalog, store: VectorStore | None) -> None:
    """Drop a document from the catalog and, where possible, the index.

    The index is cleared first: a vector left behind with no catalog row is a
    stale hit that retrieval would have to filter out, whereas a catalog row
    with no vector is merely unsearchable.
    """
    if store is not None:
        try:
            store.delete_document(doc_id)
        except StoreError as exc:
            logger.warning("Could not remove vectors for %s: %s", doc_id, exc)
    catalog.delete_document(doc_id)


async def scan_once(
    paths: Sequence[Path | str],
    *,
    catalog: Catalog,
    store: VectorStore | None = None,
    embed: bool = True,
    prune: bool = False,
    summary: WatchSummary | None = None,
    on_event: Callable[[WatchEvent], None] | None = None,
) -> WatchSummary:
    """One pass: ingest changes, optionally prune deletions, then embed."""
    active = summary or WatchSummary()
    active.scans += 1

    def emit(kind: EventKind, detail: str, path: Path | None = None) -> None:
        if on_event is not None:
            on_event(WatchEvent(kind=kind, detail=detail, path=path))

    # -- new and changed files ------------------------------------------
    try:
        result = await ingest_paths(paths, catalog=catalog)
    except Exception as exc:
        logger.exception("Watch scan failed during ingestion")
        active.errors += 1
        emit("error", f"ingestion failed: {exc}")
        return active

    for outcome in result.results:
        if outcome.status == "ingested":
            active.ingested += 1
            emit("ingested", f"{outcome.path.name}: {outcome.chunks} chunks", outcome.path)
        elif outcome.status == "failed":
            active.errors += 1
            emit("error", f"{outcome.path.name}: {outcome.error}", outcome.path)

    # -- deletions -------------------------------------------------------
    if prune:
        for doc_id, source in find_deleted(catalog, paths):
            try:
                prune_document(doc_id, catalog, store)
            except Exception as exc:
                active.errors += 1
                emit("error", f"could not remove {source.name}: {exc}", source)
                continue
            active.removed += 1
            emit("removed", source.name, source)

    # -- index -----------------------------------------------------------
    if embed:
        from mcp_fetch_server.rag.embed import run_embedding

        try:
            embed_summary = await run_embedding(catalog=catalog, store=store)
        except (LLMError, StoreError) as exc:
            active.errors += 1
            emit("error", f"embedding unavailable: {exc}")
        except Exception as exc:
            logger.exception("Watch scan failed during embedding")
            active.errors += 1
            emit("error", f"embedding failed: {exc}")
        else:
            for outcome in embed_summary.results:
                if outcome.status == "embedded":
                    active.embedded += 1
                    emit("embedded", f"{outcome.title or outcome.doc_id}: {outcome.chunks} chunks")
                elif outcome.status == "failed":
                    active.errors += 1
                    emit("error", f"{outcome.title or outcome.doc_id}: {outcome.error}")

    return active


async def watch_paths(
    paths: Sequence[Path | str],
    *,
    interval: float = 30.0,
    catalog: Catalog | None = None,
    store: VectorStore | None = None,
    embed: bool = True,
    prune: bool = False,
    once: bool = False,
    max_scans: int | None = None,
    on_event: Callable[[WatchEvent], None] | None = None,
    stop_event: asyncio.Event | None = None,
) -> WatchSummary:
    """Watch folders and keep the corpus in step until stopped."""
    delay = max(MIN_INTERVAL, interval)
    summary = WatchSummary()

    active_catalog = catalog or Catalog().open()
    owns_catalog = catalog is None
    active_store = store
    owns_store = False
    if active_store is None and embed:
        active_store = VectorStore()
        owns_store = True

    try:
        while True:
            await scan_once(
                paths,
                catalog=active_catalog,
                store=active_store,
                embed=embed,
                prune=prune,
                summary=summary,
                on_event=on_event,
            )

            if once or (max_scans is not None and summary.scans >= max_scans):
                break
            if stop_event is not None and stop_event.is_set():
                break

            if stop_event is not None:
                try:
                    # Wake immediately on stop rather than sleeping out the
                    # whole interval first.
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                    break
                except TimeoutError:
                    pass
            else:
                await asyncio.sleep(delay)
    finally:
        if owns_store and active_store is not None:
            active_store.close()
        if owns_catalog:
            active_catalog.close()

    return summary


def describe_targets(paths: Sequence[Path | str]) -> str:
    """Human-readable summary of what is about to be watched."""
    files = discover_files(paths)
    listed = ", ".join(str(Path(path)) for path in paths)
    return f"{listed} ({len(files)} supported file(s) found)"
