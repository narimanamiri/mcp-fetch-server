"""Tests for the folder watcher."""

from __future__ import annotations

import asyncio

import pytest

from mcp_fetch_server.__main__ import main as cli_main
from mcp_fetch_server.config import settings
from mcp_fetch_server.rag.catalog import Catalog
from mcp_fetch_server.rag.watch import (
    WatchEvent,
    WatchSummary,
    describe_targets,
    find_deleted,
    prune_document,
    scan_once,
    watch_paths,
)


@pytest.fixture
def catalog(tmp_path):
    with Catalog(tmp_path / "corpus") as instance:
        yield instance


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "watched"
    root.mkdir()
    (root / "first.md").write_text(
        "# First Doc\n\n## Intro\n\nDense retrieval embeds text into vectors.\n",
        encoding="utf-8",
    )
    return root


async def scan(folder, catalog, **kwargs):
    """Scan without embedding, so tests need no model or vector store."""
    kwargs.setdefault("embed", False)
    return await scan_once([folder], catalog=catalog, **kwargs)


# ------------------------------------------------------------- scanning


async def test_first_scan_ingests(folder, catalog):
    summary = await scan(folder, catalog)
    assert summary.ingested == 1
    assert summary.errors == 0
    assert catalog.stats()["documents"] == 1


async def test_unchanged_rescan_is_a_noop(folder, catalog):
    await scan(folder, catalog)
    summary = await scan(folder, catalog)
    assert summary.ingested == 0
    assert catalog.stats()["documents"] == 1


async def test_new_file_is_picked_up(folder, catalog):
    await scan(folder, catalog)
    (folder / "second.md").write_text("# Second\n\nBM25 ranks by term frequency.\n", "utf-8")

    summary = await scan(folder, catalog)
    assert summary.ingested == 1
    assert catalog.stats()["documents"] == 2


async def test_changed_file_is_reingested(folder, catalog):
    await scan(folder, catalog)
    original = catalog.list_documents()[0].content_hash

    (folder / "first.md").write_text("# First Doc\n\nEntirely new content here.\n", "utf-8")
    summary = await scan(folder, catalog)

    assert summary.ingested == 1
    assert catalog.list_documents()[0].content_hash != original
    assert catalog.stats()["documents"] == 1


async def test_a_broken_file_is_an_error_not_a_crash(folder, catalog):
    (folder / "empty.md").write_text("   \n\n  ", encoding="utf-8")
    summary = await scan(folder, catalog)
    assert summary.errors == 1
    assert summary.ingested == 1  # the good file still went in


async def test_events_are_emitted(folder, catalog):
    events: list[WatchEvent] = []
    await scan(folder, catalog, on_event=events.append)
    kinds = {event.kind for event in events}
    assert "ingested" in kinds
    assert any("first.md" in event.detail for event in events)


# -------------------------------------------------------------- deletion


async def test_deleted_file_is_kept_without_prune(folder, catalog):
    """Removing a file is not obviously an instruction to drop the document,
    and the mistake is not cheap to undo."""
    await scan(folder, catalog)
    (folder / "first.md").unlink()

    summary = await scan(folder, catalog)
    assert summary.removed == 0
    assert catalog.stats()["documents"] == 1


async def test_prune_removes_the_document(folder, catalog):
    await scan(folder, catalog)
    (folder / "first.md").unlink()

    summary = await scan(folder, catalog, prune=True)
    assert summary.removed == 1
    assert catalog.stats()["documents"] == 0
    assert catalog.stats()["chunks"] == 0


def test_find_deleted_only_looks_inside_watched_roots(tmp_path, catalog):
    """Pointing the watcher at one folder must never prune documents that were
    ingested from somewhere else."""
    from mcp_fetch_server.rag.catalog import DocumentRecord, make_doc_id

    watched = tmp_path / "watched"
    watched.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    for name, directory in (("inside", watched), ("outside", elsewhere)):
        url = f"https://local.archive/doc/{name}"
        digest, blob_path = catalog.store_blob(f"# {name}")
        catalog.upsert_document(
            DocumentRecord(
                doc_id=make_doc_id(url),
                url=url,
                content_hash=digest,
                doctype="markdown",
                blob_path=blob_path,
                source_path=str(directory / f"{name}.md"),  # neither exists
                title=name,
            )
        )

    missing = find_deleted(catalog, [watched])
    assert len(missing) == 1
    assert missing[0][1].name == "inside.md"


def test_find_deleted_ignores_documents_with_no_source(catalog):
    from mcp_fetch_server.rag.catalog import DocumentRecord, make_doc_id

    url = "https://example.com/page"
    digest, blob_path = catalog.store_blob("# web page")
    catalog.upsert_document(
        DocumentRecord(
            doc_id=make_doc_id(url),
            url=url,
            content_hash=digest,
            doctype="html",
            blob_path=blob_path,
            source_path=None,
            title="Web page",
        )
    )
    assert find_deleted(catalog, ["/anything"]) == []


def test_find_deleted_with_no_roots(catalog):
    assert find_deleted(catalog, []) == []


def test_prune_document_survives_a_missing_store(folder, catalog):
    """A vector store that is down must not block removing the catalog row."""

    class BrokenStore:
        def delete_document(self, doc_id):
            from mcp_fetch_server.rag.store import StoreError

            raise StoreError("index unreachable")

    asyncio.run(scan(folder, catalog))
    doc_id = catalog.list_documents()[0].doc_id

    prune_document(doc_id, catalog, BrokenStore())
    assert catalog.get_document(doc_id) is None


# ----------------------------------------------------------------- loop


async def test_watch_stops_after_max_scans(folder, catalog):
    summary = await watch_paths(
        [folder], catalog=catalog, embed=False, interval=2.0, max_scans=2
    )
    assert summary.scans == 2


async def test_watch_once_runs_a_single_scan(folder, catalog):
    summary = await watch_paths([folder], catalog=catalog, embed=False, once=True)
    assert summary.scans == 1
    assert summary.ingested == 1


async def test_stop_event_ends_the_loop_promptly(folder, catalog):
    """The watcher must wake on stop rather than sleeping out the interval."""
    stop = asyncio.Event()

    async def stop_soon():
        await asyncio.sleep(0.1)
        stop.set()

    loop = asyncio.get_running_loop()
    started = loop.time()
    _, summary = await asyncio.gather(
        stop_soon(),
        watch_paths(
            [folder], catalog=catalog, embed=False, interval=3600.0, stop_event=stop
        ),
    )
    assert summary.scans >= 1
    assert loop.time() - started < 5.0


async def test_watch_on_an_empty_folder(tmp_path, catalog):
    empty = tmp_path / "nothing"
    empty.mkdir()
    summary = await watch_paths([empty], catalog=catalog, embed=False, once=True)
    assert summary.ingested == 0
    assert summary.errors == 0


# --------------------------------------------------------------- summary


def test_summary_renders_and_serialises():
    summary = WatchSummary(scans=3, ingested=2, removed=1, embedded=2, errors=0)
    assert "3 scan(s)" in summary.render()
    payload = summary.as_dict()
    assert payload["ingested"] == 2
    assert payload["removed"] == 1


def test_event_render_uses_a_marker():
    assert WatchEvent(kind="ingested", detail="a.md").render().startswith("+")
    assert WatchEvent(kind="removed", detail="b.md").render().startswith("-")
    assert WatchEvent(kind="error", detail="boom").render().startswith("!")


def test_describe_targets_counts_files(folder):
    described = describe_targets([folder])
    assert "1 supported file" in described


# ------------------------------------------------------------------- cli


def test_cli_watch_once(folder, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(settings, "corpus_data_dir", str(tmp_path / "clidata"))
    assert cli_main(["watch", str(folder), "--once", "--no-embed"]) == 0
    output = capsys.readouterr().out
    assert "first.md" in output
    assert "1 scan(s)" in output


def test_cli_watch_reports_prune_mode(folder, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(settings, "corpus_data_dir", str(tmp_path / "prunedata"))
    assert cli_main(["watch", str(folder), "--once", "--no-embed", "--prune"]) == 0
    assert "Pruning is on" in capsys.readouterr().out


def test_cli_watch_returns_one_on_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(settings, "corpus_data_dir", str(tmp_path / "faildata"))
    root = tmp_path / "bad"
    root.mkdir()
    (root / "blank.md").write_text("  ", encoding="utf-8")
    assert cli_main(["watch", str(root), "--once", "--no-embed"]) == 1


def test_cli_watch_handles_interrupt_as_a_normal_exit(folder, tmp_path, monkeypatch, capsys):
    """Ctrl+C is how a watcher is meant to end, not a failure."""
    monkeypatch.setattr(settings, "corpus_data_dir", str(tmp_path / "intdata"))

    async def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("mcp_fetch_server.rag.watch.watch_paths", interrupted)
    assert cli_main(["watch", str(folder), "--no-embed"]) == 0
    assert "Stopped." in capsys.readouterr().out
