"""FastMCP server exposing web fetch tools."""

from __future__ import annotations

import logging
import sys
from typing import Literal

from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_fetch_server import __version__
from mcp_fetch_server.admin import AdminPanel, start_admin_background
from mcp_fetch_server.auth import StaticTokenVerifier
from mcp_fetch_server.completions import register_completions
from mcp_fetch_server.config import settings
from mcp_fetch_server.fetch_service import fetch_and_record
from mcp_fetch_server.fetcher import (
    FetchError,
    fetch_metadata,
    format_metadata,
)
from mcp_fetch_server.prompts import register_prompts
from mcp_fetch_server.resources import register_resources
from mcp_fetch_server.security import SecurityError
from mcp_fetch_server.tools_extra import register_extra_tools

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stderr,
    )


def create_mcp_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    require_auth: bool = False,
    transport: Literal["stdio", "streamable-http"] = "stdio",
) -> FastMCP:
    _configure_logging()

    auth_settings: AuthSettings | None = None
    token_verifier = None

    if require_auth:
        if not settings.mcp_auth_token:
            raise ValueError("MCP_AUTH_TOKEN must be set for streamable-http transport")
        resource_url = AnyHttpUrl(f"http://{host}:{port}/mcp")
        auth_settings = AuthSettings(
            issuer_url=AnyHttpUrl(f"http://{host}:{port}"),
            resource_server_url=resource_url,
        )
        token_verifier = StaticTokenVerifier(settings.mcp_auth_token)

    mcp = FastMCP(
        "web-fetch",
        instructions=(
            "An all-in-one web research server. Core tools: fetch_url (page content as "
            "markdown, chunked via start_index), fetch_metadata_tool (HEAD info), "
            "batch_fetch (many URLs at once), web_search (DuckDuckGo, no API key), "
            "extract_links, and summarize_url (uses client-side sampling). Local file "
            "tools (read_file/write_file/list_dir) are sandboxed to FETCH_LOCAL_FILES_ROOT "
            "and any client-exposed roots. Resources expose config://settings, "
            "history://recent, and fetch-cache://{encoded_url}. Prompts offer ready-made "
            "research workflows (research_topic, summarize_page, extract_key_facts, "
            "compare_sources)."
        ),
        host=host,
        port=port,
        log_level=settings.log_level.upper(),  # type: ignore[arg-type]
        auth=auth_settings,
        token_verifier=token_verifier,
    )

    read_only = ToolAnnotations(readOnlyHint=True)

    @mcp.custom_route("/health", methods=["GET"])
    async def health_check(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__})

    @mcp.tool(
        annotations=read_only,
        description=(
            "Fetch a public HTTP/HTTPS URL and return page content as markdown. "
            "Use start_index to read long pages in chunks."
        ),
    )
    async def fetch_url(
        url: str,
        max_length: int = settings.default_max_length,
        start_index: int = 0,
        raw: bool = False,
        ignore_robots_txt: bool = False,
    ) -> str:
        """Fetch a public HTTP/HTTPS URL and return page content as markdown."""
        try:
            result = await fetch_and_record(
                url,
                max_length=max_length,
                start_index=start_index,
                raw=raw,
                ignore_robots_txt=ignore_robots_txt,
            )
            return result.content
        except SecurityError as exc:
            raise ToolError(f"Security check failed: {exc}") from exc
        except FetchError as exc:
            raise ToolError(str(exc)) from exc
        except Exception as exc:
            logger.exception("Unexpected fetch_url failure")
            raise ToolError(f"Fetch failed: {exc}") from exc

    @mcp.tool(
        annotations=read_only,
        description="Return HTTP metadata for a URL using a HEAD request.",
    )
    async def fetch_metadata_tool(url: str) -> str:
        """Return HTTP metadata for a URL using a HEAD request."""
        try:
            metadata = await fetch_metadata(url)
            return format_metadata(metadata)
        except SecurityError as exc:
            raise ToolError(f"Security check failed: {exc}") from exc
        except FetchError as exc:
            raise ToolError(str(exc)) from exc
        except Exception as exc:
            logger.exception("Unexpected fetch_metadata failure")
            raise ToolError(f"Metadata fetch failed: {exc}") from exc

    register_extra_tools(mcp)
    register_resources(mcp)
    register_prompts(mcp)
    register_completions(mcp)

    if settings.admin_enabled and transport == "streamable-http":
        AdminPanel(mcp=mcp, transport=transport).register_routes(mcp)

    return mcp


def run_server(
    transport: Literal["stdio", "streamable-http"] = "stdio",
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    require_auth = transport == "streamable-http"
    mcp = create_mcp_server(
        host=host,
        port=port,
        require_auth=require_auth,
        transport=transport,
    )

    if transport == "streamable-http":
        import anyio
        import uvicorn

        from mcp_fetch_server.middleware import RateLimitMiddleware, TokenBucketRateLimiter

        async def _serve() -> None:
            app = mcp.streamable_http_app()
            limiter = TokenBucketRateLimiter(limit_per_minute=settings.rate_limit_per_minute)
            wrapped_app = RateLimitMiddleware(app, limiter=limiter)
            config = uvicorn.Config(
                wrapped_app,
                host=host,
                port=port,
                log_level=settings.log_level.lower(),
            )
            server = uvicorn.Server(config)
            if settings.admin_enabled:
                logger.info("Admin GUI at http://%s:%s/admin", host, port)
            logger.info("Starting streamable-http server at http://%s:%s/mcp", host, port)
            await server.serve()

        anyio.run(_serve)
        return

    if settings.admin_enabled:
        start_admin_background(mcp=mcp, transport=transport)
        logger.info(
            "Admin GUI at http://%s:%s/admin",
            settings.admin_host,
            settings.admin_port,
        )

    mcp.run(transport="stdio")
