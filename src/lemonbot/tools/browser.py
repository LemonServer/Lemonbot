from __future__ import annotations

import html
import ipaddress
from collections.abc import Callable
from typing import Any

from jsonschema import validate  # type: ignore[import-untyped]

from lemonbot.tools.base import (
    DataClass,
    RiskLevel,
    ToolContext,
    ToolManifest,
    ToolResult,
)
from lemonbot.tools.safe_http import PinnedHTTPSFetchError, pinned_https_get
from lemonbot.tools.url_guard import (
    ResolvedURL,
    Resolver,
    UnsafeURLError,
    system_resolver,
    validate_public_https,
)


def _chromium_launch_arguments(target: ResolvedURL) -> tuple[str, ...]:
    """Pin Chromium to one already-validated address and disable proxy DNS bypasses.

    Request interception alone has a DNS time-of-check/time-of-use gap: Chromium
    would normally resolve the hostname again after the Python-side check.  A
    host-resolver rule closes that gap.  The wildcard failure rule also makes
    same-origin-only browsing fail closed if request interception is bypassed.
    """

    addresses = sorted(
        (ipaddress.ip_address(value) for value in target.addresses),
        key=lambda address: (address.version, address.compressed),
    )
    if not addresses:  # pragma: no cover - ResolvedURL is created by the URL guard
        raise UnsafeURLError("hostname resolved to no addresses")
    selected = addresses[0]
    replacement = selected.compressed
    if selected.version == 6:
        replacement = f"[{replacement}]"
    hostname = target.hostname
    if ":" in hostname:
        hostname = f"[{hostname}]"
    return (
        "--no-proxy-server",
        "--disable-quic",
        f"--host-resolver-rules=MAP {hostname} {replacement}, MAP * ~NOTFOUND",
    )


class BrowserReadTool:
    def __init__(
        self,
        *,
        enabled: bool,
        max_text_chars: int = 50_000,
        timeout_seconds: float = 30,
        resolver: Resolver = system_resolver,
        playwright_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._enabled = enabled
        self._max_text_chars = max_text_chars
        self._timeout_seconds = timeout_seconds
        self._max_document_bytes = min(
            8 * 1024 * 1024,
            max(256 * 1024, max_text_chars * 8),
        )
        self._resolver = resolver
        self._playwright_factory = playwright_factory

    def manifest(self) -> ToolManifest:
        return ToolManifest(
            name="browser.read",
            description=(
                "Read visible text from one public HTTPS page without using personal cookies."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"url": {"type": "string", "maxLength": 4096}},
                "required": ["url"],
            },
            action_kind="browse_public_https",
            risk_level=RiskLevel.MEDIUM.value,
            side_effect=False,
            idempotent=True,
            required_scopes=frozenset({"browser.read_public"}),
            allowed_data=frozenset({DataClass.PUBLIC}),
            timeout_seconds=self._timeout_seconds,
            max_output_bytes=self._max_text_chars * 4,
        )

    async def invoke(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        manifest = self.manifest()
        validate(instance=arguments, schema=manifest.input_schema)
        if not self._enabled:
            return ToolResult(ok=False, error_code="browser_disabled")
        if not manifest.required_scopes.issubset(context.granted_scopes):
            return ToolResult(ok=False, error_code="missing_scope")
        try:
            target = await validate_public_https(arguments["url"], resolver=self._resolver)
        except UnsafeURLError as exc:
            return ToolResult(ok=False, error_code="unsafe_url", content=str(exc))
        try:
            document = await pinned_https_get(
                target.normalized_url,
                maximum_bytes=self._max_document_bytes,
                timeout_seconds=min(self._timeout_seconds, 30),
                resolver=self._resolver,
            )
        except (PinnedHTTPSFetchError, TimeoutError) as exc:
            return ToolResult(ok=False, error_code="document_fetch_failed", content=str(exc))
        if document.content_type not in {None, "text/html", "application/xhtml+xml", "text/plain"}:
            return ToolResult(ok=False, error_code="unsupported_document_type")
        decoded = document.content.decode("utf-8-sig", errors="replace")
        if document.content_type == "text/plain":
            decoded = f"<pre>{html.escape(decoded)}</pre>"

        if self._playwright_factory is None:
            from playwright.async_api import async_playwright

            factory = async_playwright
        else:
            factory = self._playwright_factory

        async with factory() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=list(_chromium_launch_arguments(target)),
            )
            context_browser = await browser.new_context(
                accept_downloads=False,
                service_workers="block",
                # The first release is intentionally a document reader.  Disabling
                # page script prevents WebRTC, WebSocket, beacon and other browser
                # transports from escaping the GET/HEAD request policy.
                java_script_enabled=False,
            )

            async def block_web_socket(web_socket: Any) -> None:
                await web_socket.close(code=1008, reason="network policy")

            await context_browser.route_web_socket("**/*", block_web_socket)

            async def route_request(route: Any) -> None:
                # The only network request was the bounded, DNS-pinned GET
                # above.  Playwright is now an offline renderer; block meta
                # refreshes, frames, styles, images and any future transport.
                await route.abort("blockedbyclient")

            await context_browser.route("**/*", route_request)
            page = await context_browser.new_page()
            try:
                await page.set_content(
                    decoded,
                    wait_until="domcontentloaded",
                    timeout=int(self._timeout_seconds * 1000),
                )
                title = (await page.title())[:1_000]
                text = await page.locator("body").inner_text(timeout=5000)
                truncated = len(text) > self._max_text_chars
                text = text[: self._max_text_chars]
                return ToolResult(
                    ok=True,
                    content=f"Title: {title}\nURL: {target.normalized_url}\n\n{text}",
                    facts=(
                        {
                            "source": target.normalized_url,
                            "trust": "untrusted_web_content",
                        },
                    ),
                    truncated=truncated,
                )
            finally:
                await context_browser.close()
                await browser.close()
