"""Stdio entry point for isolated public-HTTPS page rendering."""

from __future__ import annotations

import asyncio
import sys
from typing import BinaryIO

from lemonbot.ipc import Envelope, IPCError, read_frame_sync, write_frame_sync
from lemonbot.tools.browser import BrowserReadTool
from lemonbot.tools.browser_worker_protocol import (
    BROWSER_ERROR,
    BROWSER_INIT,
    BROWSER_INVOKE,
    BROWSER_READY,
    BROWSER_RESULT,
    BROWSER_SHUTDOWN,
    BROWSER_STOPPED,
    BrowserInvokeRequest,
    BrowserInvokeResult,
    BrowserWorkerConfig,
    BrowserWorkerError,
    BrowserWorkerReady,
    EmptyBrowserRequest,
    validate_browser_payload,
)


def _reply(
    writer: BinaryIO,
    request: Envelope,
    message_type: str,
    payload: BrowserWorkerReady | BrowserInvokeResult | BrowserWorkerError,
) -> None:
    write_frame_sync(
        writer,
        Envelope(
            request_id=request.request_id,
            message_type=message_type,
            payload=payload.model_dump(mode="json"),
        ),
    )


def run_worker(reader: BinaryIO, writer: BinaryIO) -> int:
    tool: BrowserReadTool | None = None
    while True:
        try:
            request = read_frame_sync(reader)
            if request.message_type == BROWSER_INIT and tool is None:
                config = validate_browser_payload(BrowserWorkerConfig, request.payload)
                tool = BrowserReadTool(
                    enabled=True,
                    max_text_chars=config.max_text_chars,
                    timeout_seconds=config.timeout_seconds,
                )
                _reply(writer, request, BROWSER_READY, BrowserWorkerReady())
                continue
            if request.message_type == BROWSER_INVOKE and tool is not None:
                invocation = validate_browser_payload(BrowserInvokeRequest, request.payload)
                result = asyncio.run(tool.invoke(invocation.context, invocation.arguments))
                _reply(
                    writer,
                    request,
                    BROWSER_RESULT,
                    BrowserInvokeResult(result=result),
                )
                continue
            if request.message_type == BROWSER_SHUTDOWN:
                validate_browser_payload(EmptyBrowserRequest, request.payload)
                _reply(writer, request, BROWSER_STOPPED, BrowserWorkerReady())
                return 0
            _reply(
                writer,
                request,
                BROWSER_ERROR,
                BrowserWorkerError(code="invalid_state"),
            )
        except (IPCError, ValueError):
            return 2
        except BaseException:
            if "request" not in locals():
                return 3
            try:
                _reply(
                    writer,
                    request,
                    BROWSER_ERROR,
                    BrowserWorkerError(code="internal"),
                )
            except BaseException:
                return 3


def main() -> int:
    return run_worker(sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    raise SystemExit(main())
