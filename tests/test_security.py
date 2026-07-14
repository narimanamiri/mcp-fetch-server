"""Tests for URL validation and SSRF protections."""

from __future__ import annotations

import ipaddress
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from mcp_fetch_server.security import RobotsCache, SecurityError, ValidatedUrl, validate_url


@pytest.mark.asyncio
async def test_rejects_unsupported_scheme() -> None:
    with pytest.raises(SecurityError, match="Unsupported URL scheme"):
        await validate_url("file:///etc/passwd")


@pytest.mark.asyncio
async def test_rejects_loopback_ip() -> None:
    with pytest.raises(SecurityError, match="Blocked address"):
        await validate_url("http://127.0.0.1/")


@pytest.mark.asyncio
async def test_rejects_metadata_ip() -> None:
    with pytest.raises(SecurityError, match="Blocked address"):
        await validate_url("http://169.254.169.254/latest/meta-data/")


@pytest.mark.asyncio
async def test_rejects_private_resolved_ip() -> None:
    with patch(
        "mcp_fetch_server.security.resolve_host",
        new=AsyncMock(return_value=[ipaddress.ip_address("10.0.0.5")]),
    ):
        with pytest.raises(SecurityError, match="Blocked address"):
            await validate_url("https://evil.example.com/")


@pytest.mark.asyncio
async def test_allows_public_resolved_ip() -> None:
    with patch(
        "mcp_fetch_server.security.resolve_host",
        new=AsyncMock(return_value=[ipaddress.ip_address("93.184.216.34")]),
    ):
        validated = await validate_url("https://example.com/docs")
        assert validated.host == "example.com"
        assert validated.path == "/docs"


@pytest.mark.asyncio
async def test_rejects_domain_not_in_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FETCH_ALLOWED_DOMAINS", "example.com")
    from mcp_fetch_server.config import Settings

    monkeypatch.setattr("mcp_fetch_server.security.settings", Settings())

    with pytest.raises(SecurityError, match="not in allowlist"):
        await validate_url("https://other.com/")


@respx.mock
@pytest.mark.asyncio
async def test_robots_fetch_does_not_use_callers_client() -> None:
    # Regression: reusing the caller's connection pool for the robots.txt
    # probe could leave a stale/broken keep-alive connection behind that
    # then failed with httpx.ReadError on the caller's next request
    # (observed against Cloudflare-fronted sites like example.com).
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private")
    )

    class ExplodingClient:
        async def get(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("robots.txt fetch must not use the caller's client")

    cache = RobotsCache()
    validated = ValidatedUrl(
        url="https://example.com/",
        scheme="https",
        host="example.com",
        path="/",
    )

    parser = await cache.fetch_robots(ExplodingClient(), validated)  # type: ignore[arg-type]

    assert parser is not None
    assert parser.can_fetch("https://example.com/", "any-agent") is True
    assert parser.can_fetch("https://example.com/private", "any-agent") is False
