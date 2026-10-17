"""Core-side bounded, query-prioritized proxy; never imports NumPy or TensorRT."""

from __future__ import annotations

import asyncio
import itertools
import json
import math
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel

from lemonbot.ipc import Envelope, IPCError, read_frame, write_frame
from lemonbot.memory.unified.models import EmbeddingRequest, EmbeddingResult
from lemonbot.supervisor import WorkerProcess, WorkerSupervisor

from .artifacts import EmbeddingError
from .protocol import (
    NONFATAL_CODES,
    ChunkRequest,
    ChunkResult,
    EmbeddingConfig,
    Ready,
    WorkerError,
    WorkerStats,
)
from .text import TextChunk

type QueueItem = tuple[int, int, str, dict[str, Any], str, asyncio.Future[Envelope]]


def validate[T: BaseModel](model: type[T], payload: dict[str, Any]) -> T:
    return model.model_validate_json(json.dumps(payload, allow_nan=False), strict=True)


class IsolatedEmbeddingWorker:
    def __init__(
        self,
        config: EmbeddingConfig,
        worker: WorkerProcess,
        supervisor: WorkerSupervisor,
        timeout: float,
    ) -> None:
        self.config = config
        self._worker = worker
        self._supervisor = supervisor
        self._timeout = timeout
        self._closed = self._poisoned = False
        self._queue: asyncio.PriorityQueue[QueueItem] = asyncio.PriorityQueue(maxsize=16)
        self._sequence = itertools.count()
        self._inflight: asyncio.Future[Envelope] | None = None
        self._processor: asyncio.Task[None] | None = None
        self._stderr = asyncio.create_task(self._discard_stderr())
        self.ready: Ready

    @classmethod
    async def create(
        cls,
        *,
        config: EmbeddingConfig,
        python_executable: Path,
        timeout_seconds: float = 30,
        supervisor: WorkerSupervisor | None = None,
    ) -> IsolatedEmbeddingWorker:
        if not 0 < timeout_seconds <= 300:
            raise ValueError("embedding RPC timeout must be in (0, 300]")
        owner = supervisor or WorkerSupervisor()
        worker = await owner.spawn(
            "embedding-" + uuid4().hex,
            python_executable,
            "-I",
            "-m",
            "lemonbot.embeddings.worker",
            cwd=Path(__file__).resolve().parent,
        )
        proxy = cls(config, worker, owner, timeout_seconds)
        try:
            response = await proxy._rpc(
                "embedding.init", config.model_dump(mode="json"), "embedding.ready"
            )
            proxy.ready = validate(Ready, response.payload)
            if (proxy.ready.backend, proxy.ready.precision) != (config.backend, config.precision):
                raise EmbeddingError("embedding_worker_identity_mismatch")
            proxy._processor = asyncio.create_task(proxy._process())
            return proxy
        except BaseException:
            await asyncio.shield(proxy.close())
            raise

    async def _discard_stderr(self) -> None:
        if self._worker.process.stderr is not None:
            while await self._worker.process.stderr.read(64 * 1024):
                pass

    async def _poison(self) -> None:
        self._poisoned = True
        await self._supervisor.stop(self._worker.name, grace_period_seconds=0.5)

    async def _rpc(self, kind: str, payload: dict[str, Any], expected: str) -> Envelope:
        message = Envelope(message_type=kind, payload=payload)
        reader, writer = self._worker.process.stdout, self._worker.process.stdin
        if reader is None or writer is None:
            raise EmbeddingError("embedding_worker_unavailable")
        try:
            async with asyncio.timeout(self._timeout):
                await write_frame(writer, message)
                response = await read_frame(reader)
            if response.request_id != message.request_id:
                await self._poison()
                raise EmbeddingError("embedding_worker_response_mismatch")
            if response.message_type == "embedding.error":
                error = validate(WorkerError, response.payload)
                if error.fatal or error.code not in NONFATAL_CODES:
                    await self._poison()
                raise EmbeddingError(error.code)
            if response.message_type != expected:
                await self._poison()
                raise EmbeddingError("embedding_worker_response_mismatch")
            return response
        except EmbeddingError:
            # Nonfatal input rejections leave the current worker usable.
            raise
        except (TimeoutError, IPCError, OSError, ValueError):
            await self._poison()
            raise EmbeddingError("embedding_worker_unavailable") from None
        except asyncio.CancelledError:
            await asyncio.shield(self._poison())
            raise

    async def _process(self) -> None:
        try:
            while not self._closed and not self._poisoned:
                _, _, kind, payload, expected, future = await self._queue.get()
                if future.cancelled():
                    self._queue.task_done()
                    continue
                self._inflight = future
                try:
                    result = await self._rpc(kind, payload, expected)
                    if not future.done():
                        future.set_result(result)
                except asyncio.CancelledError:
                    if not future.done():
                        future.set_exception(EmbeddingError("embedding_worker_unavailable"))
                    raise
                except Exception as error:
                    if not future.done():
                        future.set_exception(error)
                finally:
                    self._inflight = None
                    self._queue.task_done()
        finally:
            self._fail_pending()

    def _fail_pending(self) -> None:
        while not self._queue.empty():
            *_, future = self._queue.get_nowait()
            self._queue.task_done()
            if not future.done():
                future.set_exception(EmbeddingError("embedding_worker_unavailable"))

    async def _submit(
        self, kind: str, payload: dict[str, Any], expected: str, priority: int
    ) -> Envelope:
        if self._closed or self._poisoned:
            raise EmbeddingError("embedding_worker_unavailable")
        future: asyncio.Future[Envelope] = asyncio.get_running_loop().create_future()
        try:
            self._queue.put_nowait(
                (priority, next(self._sequence), kind, payload, expected, future)
            )
        except asyncio.QueueFull:
            raise EmbeddingError("embedding_worker_busy") from None
        try:
            return await future
        except asyncio.CancelledError:
            if self._inflight is future:
                await asyncio.shield(self._poison())
            raise

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        response = await self._submit(
            "embedding.embed",
            request.model_dump(mode="json"),
            "embedding.result",
            0 if request.purpose == "query" else 1,
        )
        try:
            result = validate(EmbeddingResult, response.payload)
            if (
                result.request_id != request.request_id
                or len(result.vectors) != len(request.texts)
                or (result.model_version, result.backend, result.precision)
                != (self.ready.model_version, self.ready.backend, self.ready.precision)
                or any(abs(math.sqrt(sum(x * x for x in v)) - 1) > 1e-4 for v in result.vectors)
            ):
                raise ValueError
            return result
        except ValueError:
            await self._poison()
            raise EmbeddingError("embedding_worker_result_invalid") from None

    async def chunks(
        self, text: str, purpose: Literal["query", "document"]
    ) -> tuple[TextChunk, ...]:
        request = ChunkRequest(text=text, purpose=purpose)
        response = await self._submit(
            "embedding.chunk",
            request.model_dump(mode="json"),
            "embedding.chunks",
            0 if purpose == "query" else 1,
        )
        try:
            chunks = validate(ChunkResult, response.payload).chunks
            offset = 0
            for chunk in chunks:
                if chunk.start != offset or chunk.text != text[chunk.start : chunk.end]:
                    raise ValueError
                offset = chunk.end
            if offset != len(text):
                raise ValueError
            return chunks
        except ValueError:
            await self._poison()
            raise EmbeddingError("embedding_worker_result_invalid") from None

    async def stats(self) -> WorkerStats:
        response = await self._submit("embedding.stats", {}, "embedding.stats", 0)
        try:
            return validate(WorkerStats, response.payload)
        except ValueError:
            await self._poison()
            raise EmbeddingError("embedding_worker_result_invalid") from None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._processor is not None:
            self._processor.cancel()
            await asyncio.gather(self._processor, return_exceptions=True)
        if not self._poisoned and hasattr(self, "ready"):
            try:
                await self._rpc("embedding.shutdown", {}, "embedding.stopped")
                # Acknowledge first, then allow the native runtime to release resources.
                async with asyncio.timeout(0.5):
                    await self._worker.process.wait()
            except (EmbeddingError, TimeoutError):
                pass
        await self._supervisor.stop(self._worker.name, grace_period_seconds=0.5)
        self._fail_pending()
        await asyncio.gather(self._stderr, return_exceptions=True)
