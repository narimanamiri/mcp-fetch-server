"""HTTP fetching with manual redirect validation and size limits."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from mcp_fetch_server.config import settings
from mcp_fetch_server.converters import chunk_content, html_to_markdown
from mcp_fetch_server.http_headers import request_headers
from mcp_fetch_server.security import (
    ValidatedUrl,
    extract_redirect_url,
    robots_cache,
    validate_url,
)

_RETRYABLE_ERRORS = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.NetworkError,
)

# When HEAD is unsupported, these status codes trigger a GET metadata probe.
_HEAD_FALLBACK_STATUSES = {400, 403, 405, 501}

_ERROR_BODY_LIMIT = 8192


class FetchError(Exception):
    """Raised when fetching fails for non-security reasons."""


@dataclass(slots=True)
class FetchMetadata:
    url: str
    status_code: int
    content_type: str | None
    content_length: int | None
    error_detail: str | None = None


@dataclass(slots=True)
class FetchResult:
    url: str
    status_code: int
    content_type: str | None
    content: str


def _network_error_message(url: str, exc: Exception) -> str:
    detail = str(exc).strip()
    if detail:
        return f"Network error fetching {url}: {detail}"
    return f"Network error fetching {url}: {type(exc).__name__}"


async def _read_limited_response(
    response: httpx.Response,
    *,
    max_bytes: int | None = None,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    limit = max_bytes if max_bytes is not None else settings.max_response_bytes

    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise FetchError(f"Response exceeds maximum allowed size of {limit} bytes")
        chunks.append(chunk)

    return b"".join(chunks)


async def _request_once(
    client: httpx.AsyncClient,
    method: str,
    url: str,
) -> httpx.Response:
    return await client.request(
        method,
        url,
        headers=request_headers(),
        follow_redirects=False,
    )


async def _request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
) -> httpx.Response:
    last_error: Exception | None = None
    attempts = max(1, settings.request_retries)

    for attempt in range(attempts):
        try:
            return await _request_once(client, method, url)
        except _RETRYABLE_ERRORS as exc:
            last_error = exc
            if attempt < attempts - 1:
                await asyncio.sleep(settings.retry_backoff_seconds * (attempt + 1))
                continue
            raise FetchError(_network_error_message(url, exc)) from exc

    raise FetchError(_network_error_message(url, last_error or RuntimeError("unknown")))


async def _request_with_redirect_validation(
    client: httpx.AsyncClient,
    url: str,
    *,
    method: str = "GET",
) -> tuple[httpx.Response, str]:
    current_url = url

    for _ in range(settings.max_redirects + 1):
        await validate_url(current_url)
        response = await _request_with_retry(client, method, current_url)

        if response.status_code in {301, 302, 303, 307, 308}:
            redirect_url = extract_redirect_url(response, current_url)
            await response.aclose()
            if not redirect_url:
                raise FetchError("Redirect response missing Location header")
            current_url = redirect_url
            continue

        return response, current_url

    raise FetchError(f"Too many redirects (max {settings.max_redirects})")


async def _ensure_robots_allowed(
    client: httpx.AsyncClient,
    url: str,
    *,
    ignore_robots_txt: bool,
) -> ValidatedUrl:
    validated = await validate_url(url)
    await robots_cache.check_allowed(client, validated, ignore_robots_txt=ignore_robots_txt)
    return validated


def _decode_body(response: httpx.Response, body: bytes) -> str:
    encoding = response.encoding or "utf-8"
    return body.decode(encoding, errors="replace")


def _bot_block_hint(text: str) -> str | None:
    lower = text.lower()
    if "robot policy" in lower or "crawling us" in lower:
        return (
            "Site blocked automated access (bot policy). "
            "Try a different URL or contact the site for API access."
        )
    if "access denied" in lower or "forbidden" in lower:
        return "Site returned access denied for this client."
    return None


def _http_error_message(
    status_code: int,
    final_url: str,
    body: bytes,
    response: httpx.Response,
) -> str:
    snippet = _decode_body(response, body).strip().replace("\n", " ")
    hint = _bot_block_hint(snippet)
    if hint:
        return f"HTTP {status_code} for {final_url}: {hint}"
    if len(snippet) > 200:
        snippet = snippet[:200] + "..."
    if snippet:
        return f"HTTP {status_code} for {final_url}: {snippet}"
    return f"HTTP {status_code} for {final_url}"


async def _metadata_from_response(
    response: httpx.Response,
    final_url: str,
) -> FetchMetadata:
    content_length_header = response.headers.get("content-length")
    content_length = int(content_length_header) if content_length_header else None
    error_detail = None

    if response.status_code >= 400:
        try:
            error_body = await _read_limited_response(response, max_bytes=1024)
            error_detail = _bot_block_hint(_decode_body(response, error_body))
            if not error_detail:
                snippet = _decode_body(response, error_body).strip().replace("\n", " ")
                if snippet:
                    error_detail = snippet[:200]
        except FetchError:
            error_detail = None

    return FetchMetadata(
        url=final_url,
        status_code=response.status_code,
        content_type=response.headers.get("content-type"),
        content_length=content_length,
        error_detail=error_detail,
    )


async def fetch_metadata(url: str) -> FetchMetadata:
    timeout = httpx.Timeout(settings.request_timeout_seconds)

    async with httpx.AsyncClient(timeout=timeout) as client:
        await _ensure_robots_allowed(client, url, ignore_robots_txt=False)

        response, final_url = await _request_with_redirect_validation(
            client,
            url,
            method="HEAD",
        )

        if response.status_code in _HEAD_FALLBACK_STATUSES:
            await response.aclose()
            response, final_url = await _request_with_redirect_validation(
                client,
                url,
                method="GET",
            )
            metadata = await _metadata_from_response(response, final_url)
            await response.aclose()
            return metadata

        metadata = await _metadata_from_response(response, final_url)
        await response.aclose()
        return metadata


async def fetch_url_content(
    url: str,
    *,
    max_length: int,
    start_index: int,
    raw: bool,
    ignore_robots_txt: bool,
) -> FetchResult:
    timeout = httpx.Timeout(settings.request_timeout_seconds)

    async with httpx.AsyncClient(timeout=timeout) as client:
        await _ensure_robots_allowed(client, url, ignore_robots_txt=ignore_robots_txt)

        response, final_url = await _request_with_redirect_validation(client, url)

        # Re-check robots.txt if redirect landed on a different host.
        if ignore_robots_txt is False:
            initial_host = (await validate_url(url)).host
            final_host = (await validate_url(final_url)).host
            if final_host != initial_host:
                await _ensure_robots_allowed(
                    client,
                    final_url,
                    ignore_robots_txt=ignore_robots_txt,
                )

        if response.status_code >= 400:
            error_body = await _read_limited_response(
                response,
                max_bytes=_ERROR_BODY_LIMIT,
            )
            raise FetchError(
                _http_error_message(response.status_code, final_url, error_body, response)
            )

        body = await _read_limited_response(response)

        content_type = response.headers.get("content-type", "")
        text = _decode_body(response, body)

        if "html" in content_type.lower() or text.lstrip().startswith("<"):
            converted = html_to_markdown(text, raw=raw)
        else:
            converted = text

        chunked = chunk_content(converted, start_index=start_index, max_length=max_length)

        return FetchResult(
            url=final_url,
            status_code=response.status_code,
            content_type=content_type or None,
            content=chunked,
        )


def format_metadata(metadata: FetchMetadata) -> str:
    parts = [
        f"URL: {metadata.url}",
        f"Status: {metadata.status_code}",
        f"Content-Type: {metadata.content_type or 'unknown'}",
    ]
    if metadata.content_length is not None:
        parts.append(f"Content-Length: {metadata.content_length}")
    if metadata.error_detail:
        parts.append(f"Error: {metadata.error_detail}")
    return "\n".join(parts)
