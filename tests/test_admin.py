"""Tests for the management web GUI (admin dashboard and API)."""

from __future__ import annotations

import time

import pytest
from httpx import ASGITransport, AsyncClient

from mcp_fetch_server.admin import AdminPanel
from mcp_fetch_server.history import HistoryEntry, history
from mcp_fetch_server.server import create_mcp_server


@pytest.fixture(autouse=True)
def _admin_test_env(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    history.clear()
    if request.node.get_closest_marker("admin_auth"):
        return
    monkeypatch.setenv("MCP_AUTH_TOKEN", "")
    from mcp_fetch_server.config import Settings

    monkeypatch.setattr("mcp_fetch_server.admin.settings", Settings())


@pytest.mark.asyncio
async def test_admin_dashboard_returns_html() -> None:
    panel = AdminPanel(transport="stdio")
    app = panel.create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "MCP Fetch Server" in response.text


@pytest.mark.asyncio
async def test_admin_status_api() -> None:
    panel = AdminPanel(transport="stdio", started_at=time.time() - 120)
    app = panel.create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/api/status")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["transport"] == "stdio"
    assert data["uptime_seconds"] >= 100
    assert data["auth_required"] is False


@pytest.mark.asyncio
async def test_admin_history_lists_recent_fetches() -> None:
    history.record(
        HistoryEntry(
            url="https://example.com/",
            status_code=200,
            content_type="text/html",
            content_length=100,
            fetched_at=time.time(),
        ),
        content="# Example",
    )

    panel = AdminPanel(transport="stdio")
    app = panel.create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/api/history")

    assert response.status_code == 200
    entries = response.json()
    assert len(entries) == 1
    assert entries[0]["url"] == "https://example.com/"
    assert entries[0]["cached"] is True


@pytest.mark.asyncio
async def test_admin_cache_content_endpoint() -> None:
    history.record(
        HistoryEntry(
            url="https://example.com/page",
            status_code=200,
            content_type="text/html",
            content_length=10,
            fetched_at=time.time(),
        ),
        content="cached body",
    )

    panel = AdminPanel(transport="stdio")
    app = panel.create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/admin/api/cache/content",
            params={"url": "https://example.com/page"},
        )

    assert response.status_code == 200
    assert response.json()["content"] == "cached body"


@pytest.mark.asyncio
async def test_admin_clear_history() -> None:
    history.record(
        HistoryEntry(
            url="https://example.com/",
            status_code=200,
            content_type=None,
            content_length=None,
            fetched_at=time.time(),
        ),
        content="data",
    )

    panel = AdminPanel(transport="stdio")
    app = panel.create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        cleared = await client.post("/admin/api/history/clear")
        history_after = await client.get("/admin/api/history")

    assert cleared.status_code == 200
    assert cleared.json()["ok"] is True
    assert history_after.json() == []


@pytest.mark.admin_auth
@pytest.mark.asyncio
async def test_admin_requires_auth_when_token_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_AUTH_TOKEN", "secret-token")
    from mcp_fetch_server.config import Settings

    monkeypatch.setattr("mcp_fetch_server.admin.settings", Settings())
    monkeypatch.setattr("mcp_fetch_server.server.settings", Settings())

    panel = AdminPanel(transport="stdio")
    app = panel.create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthorized = await client.get("/admin/api/status")
        authorized = await client.get(
            "/admin/api/status",
            headers={"Authorization": "Bearer secret-token"},
        )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200


@pytest.mark.asyncio
async def test_admin_tools_list_on_streamable_http_app() -> None:
    mcp = create_mcp_server(require_auth=False, transport="streamable-http")
    app = mcp.streamable_http_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        dashboard = await client.get("/admin")
        tools = await client.get("/admin/api/tools")

    assert dashboard.status_code == 200
    assert tools.status_code == 200
    names = {tool["name"] for tool in tools.json()}
    assert "fetch_url" in names
    assert "web_search" in names
