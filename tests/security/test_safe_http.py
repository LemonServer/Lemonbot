from __future__ import annotations

import ssl
from collections.abc import Callable
from typing import Any

import pytest

from lemonbot.tools import PinnedHTTPSFetchError, pinned_https_get
from lemonbot.tools.safe_http import _PinnedHTTPSConnection


class FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        headers: tuple[tuple[str, str], ...] = (),
        chunks: tuple[bytes, ...] = (b"payload",),
    ) -> None:
        self.status = status
        self._headers = headers
        self._chunks = list(chunks)
        self.closed = False

    def getheader(self, name: str) -> str | None:
        values = [value for key, value in self._headers if key.casefold() == name.casefold()]
        return values[0] if values else None

    def getheaders(self) -> list[tuple[str, str]]:
        return list(self._headers)

    def read(self, amount: int) -> bytes:
        if not self._chunks:
            return b""
        value = self._chunks.pop(0)
        if len(value) <= amount:
            return value
        self._chunks.insert(0, value[amount:])
        return value[:amount]

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.request_call: tuple[str, str, dict[str, str]] | None = None
        self.closed = False

    def request(
        self,
        method: str,
        target: str,
        *,
        headers: dict[str, str],
    ) -> None:
        self.request_call = (method, target, headers)

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


def factory_for(
    response: FakeResponse,
    calls: list[tuple[str, str, float, ssl.SSLContext]],
) -> Callable[[str, str, float, ssl.SSLContext], Any]:
    def factory(
        hostname: str,
        address: str,
        timeout: float,
        context: ssl.SSLContext,
    ) -> FakeConnection:
        calls.append((hostname, address, timeout, context))
        return FakeConnection(response)

    return factory


class RecordingTLSContext(ssl.SSLContext):
    def __new__(cls) -> RecordingTLSContext:
        return super().__new__(cls, ssl.PROTOCOL_TLS_CLIENT)

    def __init__(self) -> None:
        self.server_hostname: str | None = None

    def wrap_socket(  # type: ignore[override]
        self,
        sock: Any,
        *,
        server_hostname: str | None = None,
        **_kwargs: Any,
    ) -> Any:
        self.server_hostname = server_hostname
        return sock


class RecordingSocket:
    def __init__(self) -> None:
        self.timeout: float | None = None
        self.address: tuple[Any, ...] | None = None
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def connect(self, address: tuple[Any, ...]) -> None:
        self.address = address

    def close(self) -> None:
        self.closed = True


def test_connection_uses_numeric_peer_but_original_hostname_for_sni(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lemonbot.tools.safe_http as module

    raw_socket = RecordingSocket()
    monkeypatch.setattr(module.socket, "socket", lambda *_args: raw_socket)
    context = RecordingTLSContext()
    connection = _PinnedHTTPSConnection(
        "media.example.com", "93.184.216.34", 5, context
    )

    connection.connect()

    assert raw_socket.address == ("93.184.216.34", 443)
    assert raw_socket.timeout == 5
    assert context.server_hostname == "media.example.com"


async def test_get_pins_first_validated_ip_and_keeps_tls_hostname_verification() -> None:
    resolutions = 0

    async def rebinding_resolver(_hostname: str, _port: int) -> set[str]:
        nonlocal resolutions
        resolutions += 1
        return {"93.184.216.34"} if resolutions == 1 else {"127.0.0.1"}

    response = FakeResponse(
        headers=(
            ("Content-Length", "7"),
            ("Content-Type", "image/png; charset=binary"),
            ("Content-Disposition", 'attachment; filename="..\\picture.png"'),
        )
    )
    calls: list[tuple[str, str, float, ssl.SSLContext]] = []
    result = await pinned_https_get(
        "https://example.com/media?q=1#ignored",
        maximum_bytes=32,
        resolver=rebinding_resolver,
        _connection_factory=factory_for(response, calls),
    )

    assert result.content == b"payload"
    assert result.content_type == "image/png"
    assert result.filename == "picture.png"
    assert resolutions == 1
    hostname, address, _timeout, context = calls[0]
    assert hostname == "example.com"
    assert address == "93.184.216.34"
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2


async def test_get_rejects_redirect_without_following_location() -> None:
    async def resolver(_hostname: str, _port: int) -> set[str]:
        return {"93.184.216.34"}

    response = FakeResponse(
        status=302,
        headers=(("Location", "https://127.0.0.1/private"),),
    )
    calls: list[tuple[str, str, float, ssl.SSLContext]] = []

    with pytest.raises(PinnedHTTPSFetchError, match="redirects are forbidden"):
        await pinned_https_get(
            "https://example.com/media",
            maximum_bytes=32,
            resolver=resolver,
            _connection_factory=factory_for(response, calls),
        )

    assert len(calls) == 1


@pytest.mark.parametrize(
    ("headers", "chunks"),
    [
        ((("Content-Length", "33"),), (b"ignored",)),
        ((), (b"a" * 20, b"b" * 20)),
        ((("Content-Length", "7"), ("Content-Length", "7")), (b"payload",)),
        ((("Content-Length", "8"),), (b"short",)),
        (
            (("Content-Length", "7"), ("Transfer-Encoding", "chunked")),
            (b"payload",),
        ),
    ],
)
async def test_get_enforces_unambiguous_hard_byte_limit(
    headers: tuple[tuple[str, str], ...],
    chunks: tuple[bytes, ...],
) -> None:
    async def resolver(_hostname: str, _port: int) -> set[str]:
        return {"93.184.216.34"}

    response = FakeResponse(headers=headers, chunks=chunks)
    calls: list[tuple[str, str, float, ssl.SSLContext]] = []

    with pytest.raises(PinnedHTTPSFetchError):
        await pinned_https_get(
            "https://example.com/media",
            maximum_bytes=32,
            resolver=resolver,
            _connection_factory=factory_for(response, calls),
        )
