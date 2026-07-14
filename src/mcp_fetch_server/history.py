"""In-memory fetch history and content cache, used to back MCP resources.

This is intentionally process-local and non-persistent: it exists so that
resources like ``history://recent`` and ``fetch-cache://{url}`` have
something meaningful to show without adding a database dependency.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from mcp_fetch_server.config import settings


@dataclass(slots=True)
class HistoryEntry:
    url: str
    status_code: int
    content_type: str | None
    content_length: int | None
    fetched_at: float
    error: str | None = None


class FetchHistory:
    """Bounded record of recent fetches plus a bounded content cache."""

    def __init__(
        self, *, max_entries: int | None = None, max_cache_bytes: int | None = None
    ) -> None:
        self._entries: OrderedDict[str, HistoryEntry] = OrderedDict()
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._max_entries = max_entries or settings.max_history_entries
        self._max_cache_bytes = max_cache_bytes or settings.max_cache_bytes
        self._cache_bytes = 0

    def record(self, entry: HistoryEntry, *, content: str | None = None) -> None:
        self._entries[entry.url] = entry
        self._entries.move_to_end(entry.url)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

        if content is not None:
            self._cache_content(entry.url, content)

    def _cache_content(self, url: str, content: str) -> None:
        if url in self._cache:
            self._cache_bytes -= len(self._cache.pop(url).encode("utf-8", errors="ignore"))

        size = len(content.encode("utf-8", errors="ignore"))
        self._cache[url] = content
        self._cache_bytes += size

        while self._cache_bytes > self._max_cache_bytes and self._cache:
            _, evicted = self._cache.popitem(last=False)
            self._cache_bytes -= len(evicted.encode("utf-8", errors="ignore"))

    def recent(self, limit: int = 20) -> list[HistoryEntry]:
        return list(reversed(list(self._entries.values())))[:limit]

    def get_cached_content(self, url: str) -> str | None:
        return self._cache.get(url)

    def cached_urls(self) -> list[str]:
        return list(reversed(list(self._cache.keys())))

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    @property
    def cache_count(self) -> int:
        return len(self._cache)

    @property
    def cache_bytes_used(self) -> int:
        return self._cache_bytes

    def clear(self) -> None:
        self._entries.clear()
        self._cache.clear()
        self._cache_bytes = 0


history = FetchHistory()
