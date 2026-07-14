"""Tests for the in-memory fetch history/cache backing the new resources."""

from __future__ import annotations

from mcp_fetch_server.history import FetchHistory, HistoryEntry


def _entry(url: str, *, status: int = 200) -> HistoryEntry:
    return HistoryEntry(
        url=url,
        status_code=status,
        content_type="text/html",
        content_length=10,
        fetched_at=0.0,
    )


def test_recent_returns_most_recent_first() -> None:
    history = FetchHistory(max_entries=10, max_cache_bytes=1000)
    history.record(_entry("https://a.example/"))
    history.record(_entry("https://b.example/"))
    history.record(_entry("https://c.example/"))

    recent = history.recent()
    assert [e.url for e in recent] == [
        "https://c.example/",
        "https://b.example/",
        "https://a.example/",
    ]


def test_entries_evicted_beyond_max_entries() -> None:
    history = FetchHistory(max_entries=2, max_cache_bytes=1000)
    history.record(_entry("https://a.example/"))
    history.record(_entry("https://b.example/"))
    history.record(_entry("https://c.example/"))

    recent = history.recent()
    assert [e.url for e in recent] == ["https://c.example/", "https://b.example/"]


def test_cache_stores_and_retrieves_content() -> None:
    history = FetchHistory(max_entries=10, max_cache_bytes=1000)
    history.record(_entry("https://a.example/"), content="hello world")

    assert history.get_cached_content("https://a.example/") == "hello world"
    assert "https://a.example/" in history.cached_urls()


def test_cache_evicts_oldest_when_over_byte_budget() -> None:
    history = FetchHistory(max_entries=10, max_cache_bytes=15)
    history.record(_entry("https://a.example/"), content="1234567890")
    history.record(_entry("https://b.example/"), content="1234567890")

    # Cache budget (15 bytes) can't hold both 10-byte entries; oldest evicted.
    assert history.get_cached_content("https://a.example/") is None
    assert history.get_cached_content("https://b.example/") == "1234567890"


def test_uncached_url_returns_none() -> None:
    history = FetchHistory()
    assert history.get_cached_content("https://never-fetched.example/") is None
