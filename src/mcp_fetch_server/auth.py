"""Authentication helpers for Streamable HTTP transport."""

from __future__ import annotations

from mcp.server.auth.provider import AccessToken


class StaticTokenVerifier:
    """Validate a static bearer token configured via MCP_AUTH_TOKEN."""

    def __init__(self, expected_token: str) -> None:
        self._expected_token = expected_token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or token != self._expected_token:
            return None
        return AccessToken(
            token=token,
            client_id="mcp-fetch-client",
            scopes=[],
        )
