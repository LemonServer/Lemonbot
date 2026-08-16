"""Stdio entry point for isolated image decoding, OCR, and Zhipu vision."""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from pydantic import ValidationError

from lemonbot.ipc import Envelope, IPCError, read_frame_sync, write_frame_sync
from lemonbot.models.budget import BudgetError, BudgetLimits, BudgetManager, ModelPrice
from lemonbot.models.secrets import SecretNotFoundError
from lemonbot.models.vision import (
    SanitizedImage,
    VisionError,
    VisionRequest,
    VisionResult,
    ZhipuVisionAdapter,
)
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
    EmptyVisionWorkerRequest,
    VisionCommit,
    VisionFileRequest,
    VisionPrepared,
    VisionWorkerConfig,
    VisionWorkerError,
    VisionWorkerErrorCode,
    VisionWorkerReady,
    VisionWorkerResult,
    validate_vision_payload,
)
from lemonbot.security.model_secrets import AsyncSecretStoreAdapter
from lemonbot.security.secrets import (
    NamespacedSecretStore,
    SecretStoreError,
    WindowsCredentialStore,
)
from lemonbot.tools.object_store import ObjectStoreError
from lemonbot.tools.vision import ImagePreprocessor, ImageRejected, PreparedImage, RapidOCRReader


@dataclass(frozen=True, slots=True)
class _PendingVision:
    operation_id: str
    prepared: PreparedImage
    request: VisionRequest
    ocr_available: bool


class VisionWorkerRuntime:
    """Own all image bytes, OCR state, credentials, and provider transport."""

    def __init__(
        self,
        config: VisionWorkerConfig,
        *,
        adapter: ZhipuVisionAdapter | None = None,
    ) -> None:
        self._config = config
        self._objects_root = self._validate_objects_root(Path(config.objects_root))
        self._preprocessor = ImagePreprocessor(
            max_file_bytes=config.max_file_bytes,
            max_pixels=config.max_pixels,
            max_dimension=config.max_dimension,
        )
        self._ocr = RapidOCRReader()
        if adapter is None:
            credentials = NamespacedSecretStore(WindowsCredentialStore(), config.profile)
            secret_store = AsyncSecretStoreAdapter(credentials)
            zero = ModelPrice(Decimal(0), Decimal(0))
            budget = BudgetManager(
                limits=BudgetLimits(daily=Decimal(1), monthly=Decimal(1)),
                prices={(config.provider.provider, config.provider.model): zero},
            )
            adapter = ZhipuVisionAdapter(
                secret_store=secret_store,
                budget=budget,
                config=config.provider,
            )
        self._adapter = adapter

    @staticmethod
    def _is_link(path: Path) -> bool:
        return path.is_symlink() or path.is_junction()

    @classmethod
    def _validate_objects_root(cls, path: Path) -> Path:
        if not path.is_absolute() or not path.exists() or not path.is_dir():
            raise ValueError("vision objects root is unavailable")
        if cls._is_link(path):
            raise ValueError("vision objects root cannot be a link or junction")
        return path.resolve(strict=True)

    @staticmethod
    def _detected_media_type(raw: bytes) -> str:
        if len(raw) >= 3 and raw[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            return "image/webp"
        raise ImageRejected("image file header is not JPEG, PNG or WebP")

    def _read_validated_object(self, request: VisionFileRequest) -> bytes:
        configured = Path(self._config.objects_root)
        candidate = Path(request.object_path)
        expected = configured / request.expected_sha256[:2] / request.expected_sha256
        if candidate != expected:
            raise ImageRejected("object path is not the expected content-addressed path")
        shard = expected.parent
        for component in (configured, shard, candidate):
            if self._is_link(component):
                raise ImageRejected("links and junctions are forbidden in image object paths")
        if not shard.is_dir() or not candidate.is_file():
            raise ImageRejected("image object path is not a regular content-addressed file")
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self._objects_root)
        except (OSError, ValueError) as exc:
            raise ImageRejected("image object escapes the configured object root") from exc
        if resolved != self._objects_root / request.expected_sha256[:2] / request.expected_sha256:
            raise ImageRejected("resolved image object does not match its content address")

        digest = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        with candidate.open("rb") as stream:
            if os.fstat(stream.fileno()).st_size != request.expected_size:
                raise ImageRejected("image object size does not match attachment metadata")
            while chunk := stream.read(1024 * 1024):
                total += len(chunk)
                if total > self._config.max_file_bytes or total > request.expected_size:
                    raise ImageRejected("image object exceeds its bounded size")
                digest.update(chunk)
                chunks.append(chunk)
        if total != request.expected_size or digest.hexdigest() != request.expected_sha256:
            raise ImageRejected("image object failed content-address verification")
        raw = b"".join(chunks)
        detected = self._detected_media_type(raw)
        if request.declared_media_type.startswith("image/") and (
            request.declared_media_type != detected
        ):
            raise ImageRejected("declared image media type does not match the file header")
        return raw

    def _prepare_sync(self, request: VisionFileRequest) -> _PendingVision:
        raw = self._read_validated_object(request)
        prepared = self._preprocessor.prepare(raw)
        ocr_available = self._config.ocr_enabled and self._ocr.available()
        ocr_text = ""
        if ocr_available:
            try:
                ocr_text = self._ocr.read(prepared)[:100_000]
            except Exception:
                # OCR is an optional local enhancement. Its failure must not invent text
                # or prevent an otherwise safe semantic-vision request.
                ocr_available = False
                ocr_text = ""
        sanitized = SanitizedImage.from_bytes(
            prepared.content,
            media_type="image/jpeg",
            width=prepared.width,
            height=prepared.height,
        )
        return _PendingVision(
            operation_id=uuid4().hex,
            prepared=prepared,
            request=VisionRequest(
                image=sanitized,
                prompt=request.prompt,
                ocr_text=ocr_text,
                correlation_id=request.correlation_id,
            ),
            ocr_available=ocr_available,
        )

    async def prepare(self, request: VisionFileRequest) -> tuple[VisionPrepared, _PendingVision]:
        pending = await asyncio.to_thread(self._prepare_sync, request)
        estimate = (
            self._config.provider.image_token_reserve
            + len(pending.request.prompt.encode("utf-8"))
            + len(pending.request.ocr_text.encode("utf-8"))
            + 512
        )
        prepared = VisionPrepared(
            operation_id=pending.operation_id,
            estimated_prompt_tokens=estimate,
            sanitized_sha256=pending.prepared.sha256,
            width=pending.prepared.width,
            height=pending.prepared.height,
            ocr_available=pending.ocr_available,
            ocr_characters=len(pending.request.ocr_text),
        )
        return prepared, pending

    @staticmethod
    def _fallback(
        pending: _PendingVision,
        *,
        provider_started: bool,
        must_close: bool,
    ) -> VisionWorkerResult:
        if pending.ocr_available:
            description = (
                "视觉语义分析当前不可用。以下仅为本地 OCR 结果，"
                "不能据此推断图片的其他内容。"
            )
            limitation = "semantic vision unavailable; OCR-only fallback"
        else:
            description = (
                "视觉语义分析当前不可用，且本地 OCR 引擎不可用；"
                "无法可靠描述图片内容。"
            )
            limitation = "semantic vision and local OCR unavailable"
        return VisionWorkerResult(
            operation_id=pending.operation_id,
            result=VisionResult(
                description=description,
                ocr_text=pending.request.ocr_text,
                semantic_available=False,
                limitation=limitation,
            ),
            sanitized_sha256=pending.prepared.sha256,
            width=pending.prepared.width,
            height=pending.prepared.height,
            provider_call_started=provider_started,
            worker_must_close=must_close,
        )

    async def commit(self, pending: _PendingVision) -> VisionWorkerResult:
        try:
            result = await self._adapter.analyze(pending.request)
        except (SecretNotFoundError, SecretStoreError, BudgetError):
            return self._fallback(
                pending,
                provider_started=False,
                must_close=False,
            )
        except VisionError as exc:
            return self._fallback(
                pending,
                provider_started=exc.provider_call_started,
                # Provider transport/protocol failures, including timeouts, poison
                # this worker even when connection setup proves no request was sent.
                must_close=True,
            )
        return VisionWorkerResult(
            operation_id=pending.operation_id,
            result=result,
            sanitized_sha256=pending.prepared.sha256,
            width=pending.prepared.width,
            height=pending.prepared.height,
            provider_call_started=True,
        )

    async def aclose(self) -> None:
        await self._adapter.aclose()


RuntimeFactory = Callable[[VisionWorkerConfig], Awaitable[VisionWorkerRuntime]]


async def _build_runtime(config: VisionWorkerConfig) -> VisionWorkerRuntime:
    return VisionWorkerRuntime(config)


class VisionWorkerService:
    """Single-client, two-phase worker service with one in-memory pending image."""

    def __init__(
        self,
        reader: BinaryIO,
        writer: BinaryIO,
        *,
        runtime_factory: RuntimeFactory = _build_runtime,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._runtime_factory = runtime_factory
        self._runtime: VisionWorkerRuntime | None = None
        self._pending: _PendingVision | None = None

    async def _read(self) -> Envelope:
        return await asyncio.to_thread(read_frame_sync, self._reader)

    async def _write(self, envelope: Envelope) -> None:
        await asyncio.to_thread(write_frame_sync, self._writer, envelope)

    async def _reply(self, request: Envelope, message_type: str, payload: object) -> None:
        if not hasattr(payload, "model_dump"):
            raise TypeError("vision worker replies require a validated payload model")
        await self._write(
            Envelope(
                request_id=request.request_id,
                message_type=message_type,
                payload=payload.model_dump(mode="json"),
            )
        )

    async def _error(
        self,
        request: Envelope,
        *,
        code: VisionWorkerErrorCode,
        provider_call_started: bool,
    ) -> None:
        await self._reply(
            request,
            VISION_ERROR,
            VisionWorkerError(code=code, provider_call_started=provider_call_started),
        )

    async def _initialize(self, envelope: Envelope) -> bool:
        if envelope.message_type != VISION_INIT:
            await self._error(
                envelope,
                code="invalid_request",
                provider_call_started=False,
            )
            return False
        try:
            config = validate_vision_payload(VisionWorkerConfig, envelope.payload)
            self._runtime = await self._runtime_factory(config)
        except (ValidationError, ValueError, OSError, SecretStoreError):
            await self._error(
                envelope,
                code="invalid_request",
                provider_call_started=False,
            )
            return False
        except Exception:
            await self._error(
                envelope,
                code="internal",
                provider_call_started=False,
            )
            return False
        await self._reply(envelope, VISION_READY, VisionWorkerReady())
        return True

    async def _analyze(self, envelope: Envelope) -> None:
        assert self._runtime is not None
        try:
            request = validate_vision_payload(VisionFileRequest, envelope.payload)
            if self._pending is not None:
                raise ValueError("a prepared image is already awaiting commit")
            prepared, self._pending = await self._runtime.prepare(request)
        except (ImageRejected, ObjectStoreError, OSError):
            await self._error(
                envelope,
                code="image_rejected",
                provider_call_started=False,
            )
            return
        except (ValidationError, ValueError):
            await self._error(
                envelope,
                code="invalid_request",
                provider_call_started=False,
            )
            return
        except Exception:
            await self._error(
                envelope,
                code="internal",
                provider_call_started=False,
            )
            return
        await self._reply(envelope, VISION_PREPARED, prepared)

    async def _commit(self, envelope: Envelope) -> bool:
        assert self._runtime is not None
        try:
            commit = validate_vision_payload(VisionCommit, envelope.payload)
        except (ValidationError, ValueError):
            await self._error(
                envelope,
                code="invalid_request",
                provider_call_started=False,
            )
            return False
        pending, self._pending = self._pending, None
        if pending is None or commit.operation_id != pending.operation_id:
            await self._error(
                envelope,
                code="invalid_request",
                provider_call_started=False,
            )
            return False
        try:
            result = await self._runtime.commit(pending)
        except Exception:
            await self._error(
                envelope,
                code="provider_failure",
                provider_call_started=True,
            )
            return False
        await self._reply(envelope, VISION_RESULT, result)
        return not result.worker_must_close

    async def run(self) -> int:
        try:
            first = await self._read()
        except IPCError:
            return 2
        if not await self._initialize(first):
            await self._close_runtime()
            return 2
        while True:
            try:
                envelope = await self._read()
            except IPCError:
                await self._close_runtime()
                return 0
            if envelope.message_type == VISION_ANALYZE:
                await self._analyze(envelope)
                continue
            if envelope.message_type == VISION_COMMIT:
                if await self._commit(envelope):
                    continue
                await self._close_runtime()
                return 2
            if envelope.message_type == VISION_SHUTDOWN:
                try:
                    validate_vision_payload(EmptyVisionWorkerRequest, envelope.payload)
                except (ValidationError, ValueError):
                    await self._error(
                        envelope,
                        code="invalid_request",
                        provider_call_started=False,
                    )
                    await self._close_runtime()
                    return 2
                self._pending = None
                await self._reply(envelope, VISION_STOPPED, VisionWorkerReady())
                await self._close_runtime()
                return 0
            await self._error(
                envelope,
                code="invalid_request",
                provider_call_started=False,
            )
            await self._close_runtime()
            return 2

    async def _close_runtime(self) -> None:
        runtime, self._runtime = self._runtime, None
        self._pending = None
        if runtime is not None:
            try:
                await runtime.aclose()
            except Exception:
                return


def _silence_stderr() -> None:
    descriptor = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(descriptor, 2)
    finally:
        if descriptor != 2:
            os.close(descriptor)


def main() -> int:
    _silence_stderr()
    try:
        return asyncio.run(VisionWorkerService(sys.stdin.buffer, sys.stdout.buffer).run())
    except BaseException:
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
