"""Fetch helper shared by all fetch-based tools, recording into history/cache."""

from __future__ import annotations

import time

from mcp_fetch_server.fetcher import FetchError, FetchResult, fetch_url_content
from mcp_fetch_server.history import HistoryEntry, history
from mcp_fetch_server.security import SecurityError

__all__ = ["fetch_and_record"]


async def fetch_and_record(
    url: str,
    *,
    max_length: int,
    start_index: int,
    raw: bool,
    ignore_robots_txt: bool,
    cache_content: bool = True,
) -> FetchResult:
    """Fetch a URL via fetch_url_content and record the outcome in history.

    Only successful fetches starting at start_index=0 are cached (so later
    chunked reads of the same page don't repeatedly overwrite the cache
    with partial content).
    """
    try:
        result = await fetch_url_content(
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
