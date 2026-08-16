"""Small HTTPS-only downloader with DNS pinning and bounded responses."""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import re
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from email.message import Message
from pathlib import PurePosixPath
from urllib.parse import quote, urlsplit

from lemonbot.tools.url_guard import Resolver, system_resolver, validate_public_https

_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,63}$")
_WINDOWS_UNSAFE = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


class PinnedHTTPSFetchError(RuntimeError):
    """The HTTPS response could not be obtained within the security policy."""


@dataclass(frozen=True, slots=True)
class PinnedHTTPSDownload:
    content: bytes
    content_type: str | None
    filename: str | None


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection whose socket address cannot be re-resolved."""

    def __init__(
        self,
        hostname: str,
        pinned_address: str,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(hostname, port=443, timeout=timeout, context=context)
        self._pinned_address = pinned_address
        self._timeout_seconds = timeout
        self._tls_context = context

    def connect(self) -> None:
        address = ipaddress.ip_address(self._pinned_address)
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        raw_socket = socket.socket(family, socket.SOCK_STREAM)
        raw_socket.settimeout(self._timeout_seconds)
        try:
            if address.version == 6:
                raw_socket.connect((address.compressed, 443, 0, 0))
            else:
                raw_socket.connect((address.compressed, 443))
            # SNI and hostname verification deliberately use the original DNS
            # name, while the TCP peer remains the already-validated IP.
            self.sock = self._tls_context.wrap_socket(raw_socket, server_hostname=self.host)
        except BaseException:
            raw_socket.close()
            raise


ConnectionFactory = Callable[
    [str, str, float, ssl.SSLContext], http.client.HTTPSConnection
]


def _default_connection_factory(
    hostname: str,
    pinned_address: str,
    timeout: float,
    context: ssl.SSLContext,
) -> http.client.HTTPSConnection:
    return _PinnedHTTPSConnection(hostname, pinned_address, timeout, context)


def _tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.set_alpn_protocols(["http/1.1"])
    return context


def _request_target(url: str) -> str:
    parts = urlsplit(url)
    path = quote(
        parts.path or "/",
        safe="/%:@!$&'()*+,;=-._~",
    )
    if parts.query:
        query = quote(parts.query, safe="/%?:@!$&'()*+,;=-._~")
        return f"{path}?{query}"
    return path


def _safe_content_type(value: str | None) -> str | None:
    if value is None:
        return None
    media_type = value.split(";", 1)[0].strip().lower()
    return media_type if _MEDIA_TYPE.fullmatch(media_type) else None


def _safe_filename(value: str | None) -> str | None:
    if value is None or len(value) > 8_192:
        return None
    message = Message()
    message["Content-Disposition"] = value
    filename = message.get_filename()
    if not filename:
        return None
    leaf = PurePosixPath(filename.replace("\\", "/")).name
    leaf = _WINDOWS_UNSAFE.sub("_", leaf).strip(" .")[:255]
    if not leaf:
        return None
    if leaf.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        leaf = f"_{leaf}"[:255]
    return leaf


def _read_response(
    connection: http.client.HTTPSConnection,
    *,
    request_target: str,
    maximum_bytes: int,
) -> PinnedHTTPSDownload:
    connection.request(
        "GET",
        request_target,
        headers={
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Connection": "close",
            "User-Agent": "Lemonbot/0.1",
        },
    )
    response = connection.getresponse()
    try:
        if response.status != 200:
            if 300 <= response.status < 400:
                raise PinnedHTTPSFetchError("HTTPS redirects are forbidden")
            raise PinnedHTTPSFetchError(
                f"HTTPS server returned an unexpected status ({response.status})"
            )
        encoding = response.getheader("Content-Encoding")
        if encoding is not None and encoding.strip().casefold() not in {"", "identity"}:
            raise PinnedHTTPSFetchError("encoded HTTPS responses are forbidden")
        lengths = [
            value.strip()
            for name, value in response.getheaders()
            if name.casefold() == "content-length"
        ]
        transfer_encoding = response.getheader("Transfer-Encoding")
        if transfer_encoding is not None and (
            transfer_encoding.strip().casefold() != "chunked" or bool(lengths)
        ):
            raise PinnedHTTPSFetchError("HTTPS transfer framing is ambiguous or unsupported")
        if len(lengths) > 1 or (
            lengths and (not lengths[0].isdigit() or int(lengths[0]) > maximum_bytes)
        ):
            raise PinnedHTTPSFetchError("HTTPS response length is invalid or exceeds the limit")

        content = bytearray()
        while True:
            remaining = maximum_bytes + 1 - len(content)
            chunk = response.read(min(64 * 1024, remaining))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > maximum_bytes:
                raise PinnedHTTPSFetchError("HTTPS response exceeds the byte limit")
        if lengths and len(content) != int(lengths[0]):
            raise PinnedHTTPSFetchError("HTTPS response ended before its declared length")
        return PinnedHTTPSDownload(
            content=bytes(content),
            content_type=_safe_content_type(response.getheader("Content-Type")),
            filename=_safe_filename(response.getheader("Content-Disposition")),
        )
    finally:
        response.close()


def _fetch_sync(
    *,
    hostname: str,
    pinned_address: str,
    normalized_url: str,
    maximum_bytes: int,
    timeout_seconds: float,
    connection_factory: ConnectionFactory,
) -> PinnedHTTPSDownload:
    context = _tls_context()
    connection = connection_factory(hostname, pinned_address, timeout_seconds, context)
    try:
        return _read_response(
            connection,
            request_target=_request_target(normalized_url),
            maximum_bytes=maximum_bytes,
        )
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        raise PinnedHTTPSFetchError("pinned HTTPS request failed") from exc
    finally:
        connection.close()


async def pinned_https_get(
    url: str,
    *,
    maximum_bytes: int,
    timeout_seconds: float = 15,
    resolver: Resolver = system_resolver,
    _connection_factory: ConnectionFactory = _default_connection_factory,
) -> PinnedHTTPSDownload:
    """Download one non-redirecting HTTPS response from a DNS-pinned public IP.

    Only TCP port 443 and HTTP GET are supported.  The connection does not use
    environment proxies, and the default TLS context verifies the original
    hostname even though the socket connects to the selected validated IP.
    """

    if maximum_bytes < 1 or maximum_bytes > 50 * 1024 * 1024:
        raise ValueError("maximum_bytes must be between 1 byte and 50 MiB")
    if timeout_seconds <= 0 or timeout_seconds > 30:
        raise ValueError("timeout_seconds must be between 0 and 30 seconds")
    async with asyncio.timeout(timeout_seconds):
        target = await validate_public_https(url, resolver=resolver)
        addresses = sorted(
            (ipaddress.ip_address(value) for value in target.addresses),
            key=lambda address: (address.version, address.compressed),
        )
        if not addresses:  # pragma: no cover - enforced by validate_public_https
            raise PinnedHTTPSFetchError("hostname resolved to no addresses")
        pinned_address = addresses[0].compressed
        return await asyncio.to_thread(
            _fetch_sync,
            hostname=target.hostname,
            pinned_address=pinned_address,
            normalized_url=target.normalized_url,
            maximum_bytes=maximum_bytes,
            timeout_seconds=timeout_seconds,
            connection_factory=_connection_factory,
        )
