"""ASGI middleware for rate limiting."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send


class TokenBucketRateLimiter:
    def __init__(self, *, limit_per_minute: int) -> None:
        self.limit_per_minute = max(1, limit_per_minute)
        self._buckets: dict[str, list[float]] = defaultdict(list)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        window_start = now - 60.0
        timestamps = [ts for ts in self._buckets[key] if ts >= window_start]
        if len(timestamps) >= self.limit_per_minute:
            self._buckets[key] = timestamps
            return False
        timestamps.append(now)
        self._buckets[key] = timestamps
        return True


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp, *, limiter: TokenBucketRateLimiter) -> None:
        self.app = app
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path == "/health" or path.startswith("/admin"):
            await self.app(scope, receive, send)
            return

        headers: dict[str, str] = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        auth_header = headers.get("authorization", "")
        key = auth_header if auth_header else scope.get("client", ("", 0))[0]

        if not self.limiter.allow(key):
            await self._send_json(send, status_code=429, payload={"error": "rate_limit_exceeded"})
            return

        await self.app(scope, receive, send)

    async def _send_json(self, send: Send, *, status_code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
