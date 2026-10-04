"""Tests for the in-server corpus management tools.

The security property under test: corpus_ingest reads the filesystem, so it
must honour the same sandbox as read_file. Without that it would be an
unsandboxed arbitrary-read tool on a server that deliberately confines file
access, and the sandbox is off by default.
"""

from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from mcp_fetch_server.config import settings
from mcp_fetch_server.rag.catalog import Catalog, DocumentRecord, make_doc_id
from mcp_fetch_server.rag.taxonomy import Category, Taxonomy
from mcp_fetch_server.server import create_mcp_server

MANAGEMENT_TOOLS = {"corpus_ingest", "corpus_reindex", "corpus_classify"}


def text_of(result) -> str:
    content, _ = result
    return " ".join(block.text for block in content if hasattr(block, "text"))


@pytest.fixture
def corpus_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_data_dir", str(tmp_path / "data"))
    return tmp_path


@pytest.fixture
def documents(tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "guide.md").write_text(
        "# Guide\n\n## Dense\n\nDense retrievers embed text into vectors.\n",
        encoding="utf-8",
    )
    return folder


# ------------------------------------------------------------ registration


async def test_management_tools_are_registered_as_mutating():
    """A client has to be able to tell these apart from the read-only ones."""
    mcp = create_mcp_server()
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert MANAGEMENT_TOOLS <= set(tools)

    for name in MANAGEMENT_TOOLS:
        assert tools[name].annotations.readOnlyHint is False, name


async def test_destructive_operations_are_not_exposed():
    """Dropping the collection, pruning deleted documents and re-proposing the
    taxonomy stay on the CLI, where they already have confirmations. A tool
    call is the wrong place to discard a corpus."""
    mcp = create_mcp_server()
    names = {tool.name for tool in await mcp.list_tools()}
    for forbidden in ("corpus_drop", "corpus_prune", "corpus_recreate", "taxonomy_bootstrap"):
        assert forbidden not in names


# ---------------------------------------------------------------- sandbox


async def test_ingest_refuses_when_no_sandbox_is_configured(corpus_dir, documents, monkeypatch):
    """The file sandbox is off by default, and corpus ingestion must not be a
    way around that."""
    monkeypatch.setattr(settings, "local_files_root", "")
    mcp = create_mcp_server()

    with pytest.raises(ToolError) as caught:
        await mcp.call_tool("corpus_ingest", {"path": str(documents)})

    message = str(caught.value)
    assert "FETCH_LOCAL_FILES_ROOT" in message
    assert "read_file" in message


async def test_ingest_refuses_a_path_outside_the_sandbox(corpus_dir, documents, monkeypatch):
    """Path traversal out of the allowed root must be rejected, not resolved."""
    allowed = corpus_dir / "allowed"
    allowed.mkdir()
    monkeypatch.setattr(settings, "local_files_root", str(allowed))
    mcp = create_mcp_server()

    with pytest.raises(ToolError):
        await mcp.call_tool("corpus_ingest", {"path": str(documents)})

    with pytest.raises(ToolError):
        await mcp.call_tool("corpus_ingest", {"path": "../../etc"})


async def test_ingest_reports_a_missing_path_inside_the_sandbox(corpus_dir, monkeypatch):
    allowed = corpus_dir / "allowed"
    allowed.mkdir()
    monkeypatch.setattr(settings, "local_files_root", str(allowed))
    mcp = create_mcp_server()

    with pytest.raises(ToolError, match="No such path"):
        await mcp.call_tool("corpus_ingest", {"path": "absent"})


async def test_ingest_accepts_a_path_inside_the_sandbox(corpus_dir, monkeypatch):
    """Catalogues the document even though indexing needs a model that is not
    running here: the tool reports the indexing failure rather than losing the
    parse."""
    allowed = corpus_dir / "allowed"
    allowed.mkdir()
    (allowed / "note.md").write_text("# Note\n\nSome content about retrieval.\n", "utf-8")
    monkeypatch.setattr(settings, "local_files_root", str(allowed))
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:9")  # nothing listening
    mcp = create_mcp_server()

    try:
        text = text_of(await mcp.call_tool("corpus_ingest", {"path": "."}))
    except ToolError as exc:
        # Indexing is expected to fail without a model; the message must say so.
        assert "index" in str(exc).lower() or "model" in str(exc).lower()
    else:
        assert "Ingested 1" in text

    # Either way the document must be in the catalog: parsing succeeded.
    with Catalog() as catalog:
        assert catalog.stats()["documents"] == 1


# ---------------------------------------------------------------- reindex


async def test_reindex_on_an_empty_corpus_explains_itself(corpus_dir):
    mcp = create_mcp_server()
    text = text_of(await mcp.call_tool("corpus_reindex", {}))
    assert "empty" in text
    assert "corpus_ingest" in text


async def test_reindex_reports_an_unavailable_model(corpus_dir, monkeypatch):
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:9")
    url = "https://local.archive/doc/a-1"
    with Catalog() as catalog:
        digest, blob_path = catalog.store_blob("# A\n\nBody.")
        catalog.upsert_document(
            DocumentRecord(
                doc_id=make_doc_id(url), url=url, content_hash=digest,
                doctype="markdown", blob_path=blob_path, title="A",
            )
        )

    mcp = create_mcp_server()
    with pytest.raises(ToolError) as caught:
        await mcp.call_tool("corpus_reindex", {})
    assert "unavailable" in str(caught.value).lower()


# --------------------------------------------------------------- classify


async def test_classify_without_a_taxonomy_points_at_the_cli(corpus_dir):
    """Proposing a taxonomy is reviewed by hand, so it is deliberately not a
    tool call."""
    mcp = create_mcp_server()
    with pytest.raises(ToolError) as caught:
        await mcp.call_tool("corpus_classify", {})

    message = str(caught.value)
    assert "taxonomy bootstrap" in message


async def test_classify_with_nothing_to_do(corpus_dir):
    Taxonomy(categories=[Category(path="research", label="Research")]).save()
    mcp = create_mcp_server()
    text = text_of(await mcp.call_tool("corpus_classify", {}))
    assert "already classified" in text
