from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit, urlunsplit


class UnsafeURLError(ValueError):
    pass


Resolver = Callable[[str, int], Awaitable[set[str]]]

_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


@dataclass(frozen=True, slots=True)
class ResolvedURL:
    normalized_url: str
    hostname: str
    addresses: frozenset[str]


async def system_resolver(hostname: str, port: int) -> set[str]:
    loop = asyncio.get_running_loop()
    records = await loop.run_in_executor(
        None,
        lambda: socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM),
    )
    return {str(record[4][0]).split("%", 1)[0] for record in records}


def _normalize(parts: SplitResult, ascii_host: str) -> str:
    netloc = ascii_host
    if ":" in ascii_host:
        netloc = f"[{ascii_host}]"
    return urlunsplit(("https", netloc, parts.path or "/", parts.query, ""))


def _public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise UnsafeURLError("DNS resolver returned an invalid address") from exc
    return address.is_global


async def validate_public_https(
    url: str,
    *,
    resolver: Resolver = system_resolver,
) -> ResolvedURL:
    if len(url) > 4096:
        raise UnsafeURLError("URL is too long")
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise UnsafeURLError("only HTTPS URLs are allowed")
    if parts.username is not None or parts.password is not None:
        raise UnsafeURLError("credentials in URLs are forbidden")
    if not parts.hostname:
        raise UnsafeURLError("URL has no hostname")
    try:
        port = parts.port or 443
    except ValueError as exc:
        raise UnsafeURLError("URL contains an invalid port") from exc
    if port != 443:
        raise UnsafeURLError("only TCP port 443 is allowed")
    try:
        ascii_host = parts.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise UnsafeURLError("hostname is not valid IDNA") from exc
    if not ascii_host or ascii_host == "localhost" or ascii_host.endswith(".localhost"):
        raise UnsafeURLError("localhost is forbidden")

    try:
        literal = ipaddress.ip_address(ascii_host.strip("[]"))
    except ValueError:
        literal = None
    if literal is None and (
        len(ascii_host) > 253
        or any(not _DNS_LABEL.fullmatch(label) for label in ascii_host.split("."))
    ):
        raise UnsafeURLError("hostname contains unsafe characters")
    if literal is not None:
        addresses = {str(literal)}
    else:
        try:
            addresses = await resolver(ascii_host, port)
        except (OSError, socket.gaierror) as exc:
            raise UnsafeURLError("hostname could not be resolved") from exc
    if not addresses:
        raise UnsafeURLError("hostname resolved to no addresses")
    if not all(_public_address(address) for address in addresses):
        raise UnsafeURLError(
            "private, reserved, loopback and link-local destinations are forbidden"
        )
    return ResolvedURL(
        normalized_url=_normalize(parts, ascii_host),
        hostname=ascii_host,
        addresses=frozenset(addresses),
    )
