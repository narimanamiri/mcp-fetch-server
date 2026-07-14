"""URL validation, SSRF protection, and robots.txt enforcement."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

import httpx
from protego import Protego

from mcp_fetch_server.config import settings
from mcp_fetch_server.http_headers import request_headers

if TYPE_CHECKING:
    pass

ALLOWED_SCHEMES = {"http", "https"}
BLOCKED_METADATA_IPS = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)


class SecurityError(Exception):
    """Raised when a URL fails security validation."""


@dataclass(slots=True)
class ValidatedUrl:
    url: str
    scheme: str
    host: str
    path: str


def _is_blocked_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if address in BLOCKED_METADATA_IPS:
        return True
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
    )


def _validate_resolved_addresses(
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address],
    host: str,
) -> None:
    if not addresses:
        raise SecurityError(f"Could not resolve host: {host}")
    for address in addresses:
        if _is_blocked_ip(address):
            raise SecurityError(f"Blocked address for host {host}: {address}")


async def resolve_host(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    loop = asyncio.get_running_loop()

    def _resolve() -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise SecurityError(f"Could not resolve host: {host}") from exc

        addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        for info in infos:
            addresses.append(ipaddress.ip_address(info[4][0]))
        return addresses

    return await loop.run_in_executor(None, _resolve)


def _check_domain_allowlist(host: str) -> None:
    allowed = settings.allowed_domain_set
    if not allowed:
        return

    normalized_host = host.lower().rstrip(".")
    for allowed_domain in allowed:
        if normalized_host == allowed_domain or normalized_host.endswith(f".{allowed_domain}"):
            return
    raise SecurityError(f"Host not in allowlist: {host}")


async def validate_url(url: str) -> ValidatedUrl:
    parsed = urlparse(url)

    if parsed.scheme not in ALLOWED_SCHEMES:
        raise SecurityError(f"Unsupported URL scheme: {parsed.scheme or '(none)'}")

    if not parsed.netloc:
        raise SecurityError("URL must include a host")

    host = parsed.hostname
    if not host:
        raise SecurityError("URL must include a hostname")

    if parsed.username or parsed.password:
        raise SecurityError("URLs with embedded credentials are not allowed")

    _check_domain_allowlist(host)

    try:
        if host.startswith("[") and host.endswith("]"):
            address = ipaddress.ip_address(host[1:-1])
            addresses = [address]
        else:
            try:
                address = ipaddress.ip_address(host)
                addresses = [address]
            except ValueError:
                addresses = await resolve_host(host)
    except ValueError as exc:
        raise SecurityError(f"Invalid host: {host}") from exc

    _validate_resolved_addresses(addresses, host)

    return ValidatedUrl(
        url=url,
        scheme=parsed.scheme,
        host=host,
        path=parsed.path or "/",
    )


def extract_redirect_url(response: httpx.Response, base_url: str) -> str | None:
    location = response.headers.get("location")
    if not location:
        return None
    return urljoin(base_url, location)


class RobotsCache:
    def __init__(self) -> None:
        self._cache: dict[str, Protego | None] = {}

    async def fetch_robots(
        self,
        client: httpx.AsyncClient,
        validated: ValidatedUrl,
    ) -> Protego | None:
        cache_key = f"{validated.scheme}://{validated.host}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        robots_url = f"{validated.scheme}://{validated.host}/robots.txt"
        # Use a dedicated client (own connection pool) rather than the
        # caller's client. Sharing a pooled keep-alive connection between
        # this preliminary request and the subsequent content request can
        # surface as an httpx.ReadError if the server (e.g. behind
        # Cloudflare) tears down the connection between requests.
        try:
            async with httpx.AsyncClient(timeout=10.0) as robots_client:
                response = await robots_client.get(
                    robots_url,
                    headers=request_headers(),
                    follow_redirects=False,
                )
            if response.status_code >= 400:
                parser = None
            else:
                parser = Protego.parse(response.text)
        except httpx.HTTPError:
            parser = None

        self._cache[cache_key] = parser
        return parser

    async def check_allowed(
        self,
        client: httpx.AsyncClient,
        validated: ValidatedUrl,
        *,
        ignore_robots_txt: bool,
    ) -> None:
        if ignore_robots_txt:
            return

        parser = await self.fetch_robots(client, validated)
        if parser is None:
            return

        if not parser.can_fetch(validated.url, settings.user_agent):
            raise SecurityError("Fetching disallowed by robots.txt for this URL")


robots_cache = RobotsCache()
