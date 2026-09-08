"""Tests that create_mcp_server registers all expected tools/resources/prompts."""

from __future__ import annotations

import pytest

from mcp_fetch_server.server import create_mcp_server


@pytest.mark.asyncio
async def test_all_tools_registered() -> None:
    mcp = create_mcp_server()
    tools = {tool.name for tool in await mcp.list_tools()}

    expected = {
        "fetch_url",
        "fetch_metadata_tool",
        "batch_fetch",
        "web_search",
        "extract_links",
        "summarize_url",
        "read_file",
        "write_file",
        "list_dir",
    }
    assert expected.issubset(tools)


@pytest.mark.asyncio
async def test_resources_and_templates_registered() -> None:
    mcp = create_mcp_server()
    resources = {str(resource.uri) for resource in await mcp.list_resources()}
    assert {"config://settings", "history://recent"}.issubset(resources)

    templates = {template.uriTemplate for template in await mcp.list_resource_templates()}
    assert "fetch-cache://{encoded_url}" in templates


@pytest.mark.asyncio
async def test_prompts_registered() -> None:
    mcp = create_mcp_server()
    prompts = {prompt.name for prompt in await mcp.list_prompts()}

    expected = {
        "fetch",
        "research_topic",
        "summarize_page",
        "extract_key_facts",
        "compare_sources",
    }
    assert expected.issubset(prompts)


def test_completion_handler_registered() -> None:
    mcp = create_mcp_server()
    # The low-level server exposes a request handler map; completion/complete
    # should be registered once register_completions() has run.
    handlers = mcp._mcp_server.request_handlers
    from mcp.types import CompleteRequest

    assert CompleteRequest in handlers


async def test_corpus_tools_are_registered():
    """The offline-corpus tools must appear alongside the web tools."""
    mcp = create_mcp_server()
    names = {tool.name for tool in await mcp.list_tools()}
    assert {"rag_search", "corpus_stats"} <= names

    templates = {template.uriTemplate for template in await mcp.list_resource_templates()}
    assert "corpus://doc/{doc_id}" in templates

    resources = {str(resource.uri) for resource in await mcp.list_resources()}
    assert {"corpus://stats", "corpus://taxonomy"} <= resources


async def test_server_starts_without_the_rag_extra(monkeypatch):
    """A missing qdrant-client must degrade to "no corpus tools", not stop the
    server from starting at all."""
    import builtins

    real_import = builtins.__import__

    def fail_on_rag_tools(name, *args, **kwargs):
        if name == "mcp_fetch_server.rag.tools":
            raise ImportError("No module named 'qdrant_client'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_on_rag_tools)

    mcp = create_mcp_server()
    names = {tool.name for tool in await mcp.list_tools()}
    assert "rag_search" not in names
    assert "fetch_url" in names
