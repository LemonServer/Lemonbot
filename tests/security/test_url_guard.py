from __future__ import annotations

import pytest

from lemonbot.domain import ToolContext
from lemonbot.tools.browser import BrowserReadTool, _chromium_launch_arguments
from lemonbot.tools.safe_http import PinnedHTTPSDownload
from lemonbot.tools.url_guard import ResolvedURL, UnsafeURLError, validate_public_https


async def resolver(_host: str, _port: int) -> set[str]:
    return {"93.184.216.34"}


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "file:///etc/passwd",
        "data:text/plain,secret",
        "https://user:pass@example.com/",
        "https://example.com:8443/",
        "https://localhost/",
        "https://example.com,MAP%20*%20127.0.0.1/",
        "https://example.com%00.evil/",
    ],
)
async def test_rejects_unsafe_url_shapes(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        await validate_public_https(url, resolver=resolver)


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "192.168.1.1"],
)
async def test_rejects_non_public_dns_results(address: str) -> None:
    async def private_resolver(_host: str, _port: int) -> set[str]:
        return {address}

    with pytest.raises(UnsafeURLError):
        await validate_public_https("https://example.com/", resolver=private_resolver)


async def test_normalizes_and_removes_fragment() -> None:
    result = await validate_public_https(
        "https://EXAMPLE.com/path?q=1#browser-only", resolver=resolver
    )
    assert result.normalized_url == "https://example.com/path?q=1"
    assert result.addresses == frozenset({"93.184.216.34"})


def test_chromium_is_pinned_to_validated_dns_and_has_no_proxy() -> None:
    target = ResolvedURL(
        normalized_url="https://example.com/",
        hostname="example.com",
        addresses=frozenset({"93.184.216.35", "93.184.216.34"}),
    )

    arguments = _chromium_launch_arguments(target)

    assert "--no-proxy-server" in arguments
    assert "--disable-quic" in arguments
    assert (
        "--host-resolver-rules=MAP example.com 93.184.216.34, MAP * ~NOTFOUND"
        in arguments
    )


class _FakeLocator:
    async def inner_text(self, *, timeout: int) -> str:  # noqa: ASYNC109 - Playwright API fake
        assert timeout == 5000
        return "safe body"


class _FakePage:
    url = "https://example.com/"
    content: str | None = None

    async def set_content(self, content: str, **_kwargs: object) -> None:
        self.content = content

    async def title(self) -> str:
        return "Safe title"

    def locator(self, selector: str) -> _FakeLocator:
        assert selector == "body"
        return _FakeLocator()


class _FakeContext:
    def __init__(self, options: dict[str, object]) -> None:
        self.options = options
        self.websocket_handler = None

    async def route_web_socket(self, pattern: str, handler: object) -> None:
        assert pattern == "**/*"
        self.websocket_handler = handler

    async def route(self, pattern: str, _handler: object) -> None:
        assert pattern == "**/*"

    async def new_page(self) -> _FakePage:
        return _FakePage()

    async def close(self) -> None:
        return None


class _FakeBrowser:
    def __init__(self) -> None:
        self.context: _FakeContext | None = None

    async def new_context(self, **options: object) -> _FakeContext:
        self.context = _FakeContext(options)
        return self.context

    async def close(self) -> None:
        return None


class _FakeChromium:
    def __init__(self) -> None:
        self.launch_options: dict[str, object] | None = None
        self.browser = _FakeBrowser()

    async def launch(self, **options: object) -> _FakeBrowser:
        self.launch_options = options
        return self.browser


class _FakePlaywright:
    def __init__(self) -> None:
        self.chromium = _FakeChromium()

    async def __aenter__(self) -> _FakePlaywright:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


async def test_browser_disables_script_and_non_http_transports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lemonbot.tools.browser as module

    async def fetch(*_args: object, **_kwargs: object) -> PinnedHTTPSDownload:
        return PinnedHTTPSDownload(
            content=b"<html><title>Safe title</title><body>safe body</body></html>",
            content_type="text/html",
            filename=None,
        )

    monkeypatch.setattr(module, "pinned_https_get", fetch)
    playwright = _FakePlaywright()
    tool = BrowserReadTool(
        enabled=True,
        resolver=resolver,
        playwright_factory=lambda: playwright,
    )
    context = ToolContext(
        profile="lab",
        channel="test",
        chat_id="chat",
        event_id="event",
        principal_id="sender",
        granted_scopes=frozenset({"browser.read_public"}),
    )

    result = await tool.invoke(context, {"url": "https://example.com/"})

    assert result.ok
    assert playwright.chromium.launch_options is not None
    assert "--no-proxy-server" in playwright.chromium.launch_options["args"]
    assert playwright.chromium.browser.context is not None
    assert playwright.chromium.browser.context.options["java_script_enabled"] is False
    assert playwright.chromium.browser.context.websocket_handler is not None
