"""Argument completion suggestions for prompts and resource templates."""

from __future__ import annotations

from urllib.parse import quote

from mcp.server.fastmcp import FastMCP
from mcp.types import (
    Completion,
    CompletionArgument,
    CompletionContext,
    PromptReference,
    ResourceTemplateReference,
)

from mcp_fetch_server.history import history

_DEPTH_SUGGESTIONS = ["1", "3", "5", "10"]
_URL_ARGUMENT_PROMPTS = {"summarize_page", "extract_key_facts", "fetch"}


def _url_completion(partial: str, *, encode: bool = False) -> Completion:
    urls = [u for u in history.cached_urls() if u.startswith(partial)]
    values = [quote(u, safe="") for u in urls] if encode else urls
    return Completion(values=values[:20], total=len(values), hasMore=len(values) > 20)


def register_completions(mcp: FastMCP) -> None:
    @mcp.completion()
    async def handle_completion(
        ref: PromptReference | ResourceTemplateReference,
        argument: CompletionArgument,
        context: CompletionContext | None,
    ) -> Completion | None:
        partial = argument.value or ""

        if isinstance(ref, PromptReference):
            if ref.name in _URL_ARGUMENT_PROMPTS and argument.name == "url":
                return _url_completion(partial)
            if ref.name == "research_topic" and argument.name == "depth":
                matches = [v for v in _DEPTH_SUGGESTIONS if v.startswith(partial)]
                return Completion(values=matches, total=len(matches), hasMore=False)
            return None

        if isinstance(ref, ResourceTemplateReference):
            if ref.uri.startswith("fetch-cache://") and argument.name == "encoded_url":
                return _url_completion(partial, encode=True)
            return None

        return None
