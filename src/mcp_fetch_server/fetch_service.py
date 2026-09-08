"""Fetch helper shared by all fetch-based tools, recording into history/cache.

When an offline corpus is configured, this is where a request is served from
it instead of the network. The corpus is consulted first, and in ``offline``
mode the network is never reached at all.
"""

from __future__ import annotations

import logging
import time

from mcp_fetch_server.config import settings
from mcp_fetch_server.converters import chunk_content
from mcp_fetch_server.fetcher import FetchError, FetchResult, fetch_url_content
from mcp_fetch_server.history import HistoryEntry, history
from mcp_fetch_server.security import SecurityError

logger = logging.getLogger(__name__)

__all__ = ["fetch_and_record", "fetch_local"]


async def fetch_local(
    url: str,
    *,
    max_length: int,
    start_index: int,
    raw: bool,
) -> FetchResult | None:
    """Serve a URL from the local corpus, or None if it has no answer.

    Returns None rather than raising when the corpus is not configured or does
    not know the URL, so the caller can decide whether to fall through to the
    network.
    """
    if not settings.offline_enabled:
        return None

    try:
        from mcp_fetch_server.rag.offline import resolve
    except ImportError:
        # The corpus extra is not installed; behave as a plain fetch server.
        return None

    response = await resolve(url)
    if response is None:
        return None

    body = response.html() if raw else response.markdown()
    return FetchResult(
        url=response.url,
        status_code=response.status_code,
        content_type="text/html" if raw else response.content_type,
        content=chunk_content(body, start_index=start_index, max_length=max_length),
    )


async def fetch_and_record(
    url: str,
    *,
    max_length: int,
    start_index: int,
    raw: bool,
    ignore_robots_txt: bool,
    cache_content: bool = True,
) -> FetchResult:
    """Fetch a URL and record the outcome in history.

    Only successful fetches starting at start_index=0 are cached (so later
    chunked reads of the same page don't repeatedly overwrite the cache
    with partial content).
    """
    try:
        result = await _fetch(
            url,
            max_length=max_length,
            start_index=start_index,
            raw=raw,
            ignore_robots_txt=ignore_robots_txt,
        )
    except (SecurityError, FetchError) as exc:
        history.record(
            HistoryEntry(
                url=url,
                status_code=0,
                content_type=None,
                content_length=None,
                fetched_at=time.time(),
                error=str(exc),
            )
        )
        raise

    history.record(
        HistoryEntry(
            url=result.url,
            status_code=result.status_code,
            content_type=result.content_type,
            content_length=len(result.content),
            fetched_at=time.time(),
            error=None,
        ),
        content=result.content if (cache_content and start_index == 0) else None,
    )
    return result


async def _fetch(
    url: str,
    *,
    max_length: int,
    start_index: int,
    raw: bool,
    ignore_robots_txt: bool,
) -> FetchResult:
    """Resolve from the corpus first, then the network if the mode allows it."""
    local = await fetch_local(url, max_length=max_length, start_index=start_index, raw=raw)
    if local is not None:
        return local

    if settings.net_mode == "offline":
        raise FetchError(
            f"{url} is not in the local archive, and the server is in offline mode so "
            "the network was not used. Browse the archive from "
            f"{settings.site_base_url}/ or search it with web_search."
        )

    return await fetch_url_content(
        url,
        max_length=max_length,
        start_index=start_index,
        raw=raw,
        ignore_robots_txt=ignore_robots_txt,
    )
