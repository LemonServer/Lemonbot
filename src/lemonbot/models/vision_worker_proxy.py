"""Core-side proxy for the isolated image/OCR/Zhipu worker."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

from lemonbot.ipc import Envelope, IPCError, read_frame, write_frame
from lemonbot.models.budget import BudgetManager
from lemonbot.models.vision import VisionError
from lemonbot.models.vision_worker_protocol import (
    VISION_ANALYZE,
    VISION_COMMIT,
    VISION_ERROR,
    VISION_INIT,
    VISION_PREPARED,
    VISION_READY,
    VISION_RESULT,
    VISION_SHUTDOWN,
    VISION_STOPPED,
    VisionCommit,
    VisionFileRequest,
    VisionPrepared,
    VisionWorkerConfig,
    VisionWorkerError,
    VisionWorkerReady,
    VisionWorkerResult,
    validate_vision_payload,
)
from lemonbot.supervisor import WorkerProcess, WorkerSupervisor


class IsolatedVisionError(VisionError):
    """Base class for sanitized isolated-vision failures."""


class VisionWorkerUnavailable(IsolatedVisionError):
    """The subprocess or its IPC channel can no longer be trusted."""


class VisionAttachmentRejected(IsolatedVisionError):
    """The worker rejected an unsafe or inconsistent attachment object."""


class VisionWorkerRemoteError(IsolatedVisionError):
    """The worker reported a sanitized internal failure."""


class _RemoteFailure(Exception):
    def __init__(self, error: VisionWorkerError) -> None:
        super().__init__(error.code)
        self.error = error


class IsolatedVisionBackend:
    """Two-phase proxy that reserves spend only after local decode and OCR.

    This object never receives a Zhipu credential or image bytes. A timeout,
    cancellation, crash, invalid frame, or ambiguous provider outcome poisons
    the worker permanently; no request is retried or silently restarted.
    """

    def __init__(
        self,
        *,
        config: VisionWorkerConfig,
        budget: BudgetManager,
        supervisor: WorkerSupervisor,
        worker: WorkerProcess,
        rpc_timeout_seconds: float,
    ) -> None:
        self._config = config
        self._budget = budget
        self._supervisor = supervisor
        self._worker = worker
        self._rpc_timeout_seconds = rpc_timeout_seconds
        self._request_lock = asyncio.Lock()
        self._closed = False
        self._poisoned = False
        self._stderr_task = asyncio.create_task(self._discard_stderr())

    @classmethod
    async def create(
        cls,
        *,
        config: VisionWorkerConfig,
        budget: BudgetManager,
        supervisor: WorkerSupervisor | None = None,
        python_executable: Path | None = None,
        cwd: Path | None = None,
        memory_limit_bytes: int | None = 1536 * 1024 * 1024,
        rpc_timeout_seconds: float | None = None,
    ) -> IsolatedVisionBackend:
        selected_supervisor = supervisor or WorkerSupervisor()
        executable = (python_executable or Path(sys.executable)).resolve(strict=True)
        working_directory = (cwd or Path.cwd()).resolve(strict=True)
        timeout = rpc_timeout_seconds or config.provider.timeout_seconds + 30
        if timeout <= 0 or timeout > 900:
            raise ValueError("vision worker RPC timeout must be in (0, 900] seconds")
        worker = await selected_supervisor.spawn(
            f"vision-{uuid4().hex}",
            executable,
            "-I",
            "-m",
            "lemonbot.models.vision_worker",
            cwd=working_directory,
            memory_limit_bytes=memory_limit_bytes,
            # The Windows venv launcher and interpreter occupy both slots.
            max_processes=2,
        )
        proxy = cls(
            config=config,
            budget=budget,
            supervisor=selected_supervisor,
            worker=worker,
            rpc_timeout_seconds=timeout,
        )
        try:
            async with proxy._request_lock:
                response = await proxy._rpc(
                    VISION_INIT,
                    config.model_dump(mode="json"),
                    expected=VISION_READY,
                )
                validate_vision_payload(VisionWorkerReady, response.payload)
        except _RemoteFailure as exc:
            await asyncio.shield(proxy._terminate())
            proxy._raise_remote(exc.error)
        except ValueError:
            await asyncio.shield(proxy._terminate())
            raise VisionWorkerUnavailable("vision worker initialization failed") from None
        except BaseException:
            await asyncio.shield(proxy._terminate())
            raise
        return proxy

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
        if self._poisoned:
            return
        self._poisoned = True
        self._closed = True
        stdin = self._worker.process.stdin
        if stdin is not None:
            stdin.close()
        try:
            await self._supervisor.stop(self._worker.name, grace_period_seconds=1)
        except Exception:
            self._closed = True
        current = asyncio.current_task()
        if self._stderr_task is not current:
            try:
                await asyncio.wait_for(self._stderr_task, timeout=1)
            except (TimeoutError, asyncio.CancelledError):
                self._stderr_task.cancel()

    async def _rpc(
        self,
        message_type: str,
        payload: dict[str, object],
        *,
        expected: str,
    ) -> Envelope:
        if self._closed or self._poisoned:
            raise VisionWorkerUnavailable("vision worker is closed")
        process = self._worker.process
        if process.returncode is not None or process.stdin is None or process.stdout is None:
            await self._terminate()
            raise VisionWorkerUnavailable("vision worker exited")
        request = Envelope(message_type=message_type, payload=payload)
        try:
            async with asyncio.timeout(self._rpc_timeout_seconds):
                await write_frame(process.stdin, request)
                response = await read_frame(process.stdout)
        except asyncio.CancelledError:
            await asyncio.shield(self._terminate())
            raise
        except (TimeoutError, IPCError, OSError, BrokenPipeError):
            await self._terminate()
            raise VisionWorkerUnavailable("vision worker IPC failed closed") from None
        if response.request_id != request.request_id:
            await self._terminate()
            raise VisionWorkerUnavailable("vision worker response correlation failed")
        if response.message_type == VISION_ERROR:
            try:
                error = validate_vision_payload(VisionWorkerError, response.payload)
            except ValueError:
                await self._terminate()
                raise VisionWorkerUnavailable(
                    "vision worker error payload was invalid"
                ) from None
            raise _RemoteFailure(error)
        if response.message_type != expected:
            await self._terminate()
            raise VisionWorkerUnavailable("vision worker response type was invalid")
        return response

    @staticmethod
    def _raise_remote(error: VisionWorkerError) -> NoReturn:
        if error.code == "image_rejected":
            raise VisionAttachmentRejected(
                "vision worker rejected the attachment",
                provider_call_started=error.provider_call_started,
            )
        messages = {
            "invalid_request": "vision worker rejected the request",
            "provider_failure": "vision provider failed",
            "internal": "vision worker failed internally",
        }
        raise VisionWorkerRemoteError(
            messages[error.code],
            provider_call_started=error.provider_call_started,
        )

    def _validate_prepared(
        self, request: VisionFileRequest, prepared: VisionPrepared
    ) -> None:
        minimum = (
            self._config.provider.image_token_reserve
            + len(request.prompt.encode("utf-8"))
            + 512
        )
        maximum = minimum + 400_000
        if not minimum <= prepared.estimated_prompt_tokens <= maximum:
            raise ValueError("worker returned an invalid vision cost estimate")
        if prepared.width * prepared.height > self._config.max_pixels:
            raise ValueError("worker returned an over-limit sanitized image")
        if prepared.width > self._config.max_dimension or prepared.height > (
            self._config.max_dimension
        ):
            raise ValueError("worker returned an over-dimension sanitized image")

    @staticmethod
    def _validate_result(prepared: VisionPrepared, result: VisionWorkerResult) -> None:
        if (
            result.operation_id != prepared.operation_id
            or result.sanitized_sha256 != prepared.sanitized_sha256
            or result.width != prepared.width
            or result.height != prepared.height
        ):
            raise ValueError("vision result does not match the prepared image")
        if result.result.semantic_available:
            if not result.provider_call_started or result.result.model is None:
                raise ValueError("semantic result has no validated provider identity")
            if result.result.limitation is not None:
                raise ValueError("semantic result cannot claim a fallback limitation")
        elif not result.result.limitation or result.result.model is not None:
            raise ValueError("vision fallback is not explicit")
        if (
            not result.result.semantic_available
            and result.provider_call_started
            and not result.worker_must_close
        ):
            raise ValueError("ambiguous provider fallback must poison the worker")

    async def _release_budget(self, reservation_id: str) -> None:
        try:
            await self._budget.release(reservation_id)
        except Exception:
            await self._terminate()
            raise IsolatedVisionError("vision budget release failed closed") from None

    async def _charge_budget_unknown(self, reservation_id: str) -> None:
        try:
            await self._budget.settle_unknown(reservation_id)
        except Exception:
            await self._terminate()
            raise IsolatedVisionError("vision budget settlement failed closed") from None

    async def _settle_result(
        self,
        reservation_id: str,
        prepared: VisionPrepared,
        result: VisionWorkerResult,
    ) -> None:
        usage = result.result
        try:
            if not result.provider_call_started:
                await self._budget.release(reservation_id)
            elif (
                not usage.semantic_available
                or (usage.prompt_tokens == 0 and usage.completion_tokens == 0)
                or usage.prompt_tokens > prepared.estimated_prompt_tokens
                or usage.completion_tokens > self._config.provider.maximum_output_tokens
            ):
                await self._budget.settle_unknown(reservation_id)
            else:
                await self._budget.settle(
                    reservation_id,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                )
        except Exception:
            await self._terminate()
            raise IsolatedVisionError("vision budget settlement failed closed") from None

    async def _critical_transition[ResultT](
        self,
        operation: Awaitable[ResultT],
    ) -> ResultT:
        task = asyncio.ensure_future(operation)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(self._terminate())
            try:
                await asyncio.shield(task)
            except Exception:
                self._closed = True
            raise

    async def analyze_file(self, request: VisionFileRequest) -> VisionWorkerResult:
        async with self._request_lock:
            if (
                self._closed
                or self._poisoned
                or self._worker.process.returncode is not None
            ):
                await self._terminate()
                raise VisionWorkerUnavailable("vision worker is unavailable")
            try:
                envelope = await self._rpc(
                    VISION_ANALYZE,
                    request.model_dump(mode="json"),
                    expected=VISION_PREPARED,
                )
                prepared = validate_vision_payload(VisionPrepared, envelope.payload)
                self._validate_prepared(request, prepared)
            except _RemoteFailure as exc:
                if exc.error.code in {"invalid_request", "internal"}:
                    await self._terminate()
                self._raise_remote(exc.error)
            except (ValueError, VisionWorkerUnavailable):
                await self._terminate()
                raise VisionWorkerUnavailable(
                    "vision worker preparation failed validation"
                ) from None

            try:
                reservation = await self._budget.reserve(
                    provider=self._config.provider.provider,
                    model=self._config.provider.model,
                    prompt_tokens=prepared.estimated_prompt_tokens,
                    maximum_completion_tokens=self._config.provider.maximum_output_tokens,
                )
            except asyncio.CancelledError:
                await asyncio.shield(self._terminate())
                raise
            except BaseException:
                await asyncio.shield(self._terminate())
                raise

            try:
                envelope = await self._rpc(
                    VISION_COMMIT,
                    VisionCommit(operation_id=prepared.operation_id).model_dump(mode="json"),
                    expected=VISION_RESULT,
                )
                result = validate_vision_payload(VisionWorkerResult, envelope.payload)
                self._validate_result(prepared, result)
            except _RemoteFailure as exc:
                if exc.error.provider_call_started:
                    await self._critical_transition(
                        self._charge_budget_unknown(reservation.reservation_id)
                    )
                else:
                    await self._critical_transition(
                        self._release_budget(reservation.reservation_id)
                    )
                await self._terminate()
                self._raise_remote(exc.error)
            except asyncio.CancelledError:
                try:
                    await asyncio.shield(
                        self._charge_budget_unknown(reservation.reservation_id)
                    )
                finally:
                    await asyncio.shield(self._terminate())
                raise
            except BaseException:
                try:
                    await asyncio.shield(
                        self._charge_budget_unknown(reservation.reservation_id)
                    )
                finally:
                    await asyncio.shield(self._terminate())
                raise VisionWorkerUnavailable(
                    "vision worker result is unavailable or invalid"
                ) from None

            await self._critical_transition(
                self._settle_result(reservation.reservation_id, prepared, result)
            )
            if result.worker_must_close:
                await self._terminate()
            return result

    async def aclose(self) -> None:
        async with self._request_lock:
            if self._closed:
                return
            try:
                stopped = await self._rpc(
                    VISION_SHUTDOWN,
                    {},
                    expected=VISION_STOPPED,
                )
                validate_vision_payload(VisionWorkerReady, stopped.payload)
            except (IsolatedVisionError, _RemoteFailure):
                pass
            except ValueError:
                raise VisionWorkerUnavailable(
                    "vision worker shutdown response was invalid"
                ) from None
            finally:
                await self._terminate()

    async def __aenter__(self) -> IsolatedVisionBackend:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()
