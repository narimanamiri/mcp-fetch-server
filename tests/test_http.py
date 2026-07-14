"""Tests for HTTP transport, auth, and health endpoint."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_fetch_server.middleware import RateLimitMiddleware, TokenBucketRateLimiter
from mcp_fetch_server.server import create_mcp_server


@pytest.mark.asyncio
async def test_health_endpoint() -> None:
    mcp = create_mcp_server(require_auth=False)
    app = mcp.streamable_http_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_mcp_endpoint_requires_auth_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_AUTH_TOKEN", "secret-token")
    from mcp_fetch_server.config import Settings

    monkeypatch.setattr("mcp_fetch_server.server.settings", Settings())

    mcp = create_mcp_server(host="127.0.0.1", port=8000, require_auth=True)
    app = mcp.streamable_http_app()

    init_payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthorized = await client.post("/mcp", json=init_payload)

    assert unauthorized.status_code == 401


@pytest.mark.asyncio
async def test_rate_limit_middleware_returns_429() -> None:
    async def ok_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    limiter = TokenBucketRateLimiter(limit_per_minute=1)
    middleware = RateLimitMiddleware(ok_app, limiter=limiter)

    scope = {
        "type": "http",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "method": "GET",
        "path": "/",
    }

    messages: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await middleware(scope, receive, send)
    await middleware(scope, receive, send)

    assert messages[0]["status"] == 200
    assert messages[2]["status"] == 429
