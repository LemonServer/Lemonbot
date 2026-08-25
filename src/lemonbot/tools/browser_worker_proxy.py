"""Core-side Tool proxy for the isolated browser worker."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from lemonbot.domain import ToolContext, ToolManifest, ToolResult
from lemonbot.ipc import Envelope, IPCError, read_frame, write_frame
from lemonbot.supervisor import WorkerProcess, WorkerSupervisor
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


class IsolatedBrowserReadTool:
    def __init__(
        self,
        *,
        config: BrowserWorkerConfig,
        supervisor: WorkerSupervisor,
        worker: WorkerProcess,
    ) -> None:
        self._config = config
        self._supervisor = supervisor
        self._worker = worker
        self._lock = asyncio.Lock()
        self._closed = False
        self._stderr_task = asyncio.create_task(self._discard_stderr())
        self._manifest = BrowserReadTool(
            enabled=True,
            max_text_chars=config.max_text_chars,
            timeout_seconds=config.timeout_seconds,
        ).manifest()

    @classmethod
    async def create(
        cls,
        *,
        config: BrowserWorkerConfig,
        supervisor: WorkerSupervisor | None = None,
        python_executable: Path | None = None,
        cwd: Path | None = None,
    ) -> IsolatedBrowserReadTool:
        selected_supervisor = supervisor or WorkerSupervisor()
        worker = await selected_supervisor.spawn(
            f"browser-{uuid4().hex}",
            (python_executable or Path(sys.executable)).resolve(strict=True),
            "-I",
            "-m",
            "lemonbot.tools.browser_worker",
            cwd=(cwd or Path.cwd()).resolve(strict=True),
            memory_limit_bytes=1536 * 1024 * 1024,
            # Python launcher/interpreter plus Chromium's bounded process tree.
            max_processes=16,
        )
        proxy = cls(config=config, supervisor=selected_supervisor, worker=worker)
        try:
            response = await proxy._rpc(
                BROWSER_INIT,
                config.model_dump(mode="json"),
                expected=BROWSER_READY,
                timeout_seconds=10,
            )
            validate_browser_payload(BrowserWorkerReady, response.payload)
        except BaseException:
            await asyncio.shield(proxy._terminate())
            raise RuntimeError("browser worker initialization failed") from None
        return proxy

    def manifest(self) -> ToolManifest:
        return self._manifest

    async def _discard_stderr(self) -> None:
        stderr = self._worker.process.stderr
        if stderr is None:
            return
        try:
            while await stderr.read(64 * 1024):
                pass
        except Exception:
            return

    async def _terminate(self) -> None:
        if self._closed:
            return
        self._closed = True
        stdin = self._worker.process.stdin
        if stdin is not None:
            stdin.close()
        await self._supervisor.stop(self._worker.name, grace_period_seconds=1)
        current = asyncio.current_task()
        if current is not self._stderr_task:
            try:
                await asyncio.wait_for(self._stderr_task, timeout=1)
            except (TimeoutError, asyncio.CancelledError):
                self._stderr_task.cancel()

    async def _rpc(
        self,
        message_type: str,
        payload: dict[str, Any],
        *,
        expected: str,
        timeout_seconds: float,
    ) -> Envelope:
        if self._closed:
            raise RuntimeError("browser worker is closed")
        process = self._worker.process
        if process.returncode is not None or process.stdin is None or process.stdout is None:
            await self._terminate()
            raise RuntimeError("browser worker exited")
        request = Envelope(message_type=message_type, payload=payload)
        try:
            async with asyncio.timeout(timeout_seconds):
                await write_frame(process.stdin, request)
                response = await read_frame(process.stdout)
        except asyncio.CancelledError:
            await asyncio.shield(self._terminate())
            raise
        except (TimeoutError, IPCError, OSError, BrokenPipeError):
            await self._terminate()
            raise RuntimeError("browser worker IPC failed closed") from None
        if response.request_id != request.request_id:
            await self._terminate()
            raise RuntimeError("browser worker response correlation failed")
        if response.message_type == BROWSER_ERROR:
            validate_browser_payload(BrowserWorkerError, response.payload)
            raise RuntimeError("browser worker rejected the request")
        if response.message_type != expected:
            await self._terminate()
            raise RuntimeError("browser worker response type was invalid")
        return response

    async def invoke(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        async with self._lock:
            if self._closed:
                return ToolResult(ok=False, error_code="browser_worker_unavailable")
            request = BrowserInvokeRequest(context=context, arguments=arguments)
            try:
                response = await self._rpc(
                    BROWSER_INVOKE,
                    request.model_dump(mode="json"),
                    expected=BROWSER_RESULT,
                    timeout_seconds=self._config.timeout_seconds + 15,
                )
                return validate_browser_payload(BrowserInvokeResult, response.payload).result
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._terminate()
                return ToolResult(ok=False, error_code="browser_worker_unavailable")

    async def aclose(self) -> None:
        async with self._lock:
            if self._closed:
                return
            try:
                payload = EmptyBrowserRequest().model_dump(mode="json")
                await self._rpc(
                    BROWSER_SHUTDOWN,
                    payload,
                    expected=BROWSER_STOPPED,
                    timeout_seconds=3,
                )
            except Exception:
                await self._terminate()
            finally:
                await self._terminate()
