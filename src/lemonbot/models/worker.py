"""Stdio entry point for the isolated DeepSeek/OpenAI-compatible worker."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import BinaryIO, Protocol

from pydantic import ValidationError

from lemonbot.domain.models import ModelRequest, ModelResponse, Unsupported
from lemonbot.ipc import Envelope, IPCError, read_frame_sync, write_frame_sync
from lemonbot.models.budget import BudgetLimits, BudgetManager, ModelPrice
from lemonbot.models.gateway import (
    ModelProtocolError,
    ModelTransportError,
    OpenAICompatibleBackend,
)
from lemonbot.models.schema import ToolCallValidationError, ToolSchemaError
from lemonbot.models.secrets import MappingSecretStore, SecretNotFoundError, SecretStore
from lemonbot.models.worker_protocol import (
    MODEL_ERROR,
    MODEL_GENERATE,
    MODEL_INIT,
    MODEL_READY,
    MODEL_RESULT,
    MODEL_VERIFY,
    MODEL_VERIFY_RESULT,
    WORKER_SHUTDOWN,
    WORKER_STOPPED,
    EmptyWorkerRequest,
    ModelGenerateRequest,
    ModelGenerateResult,
    ModelVerifyResult,
    ModelWorkerConfig,
    ModelWorkerError,
    ModelWorkerReady,
    WorkerErrorCode,
    validate_payload,
)
from lemonbot.security.model_secrets import AsyncSecretStoreAdapter
from lemonbot.security.secrets import (
    NamespacedSecretStore,
    SecretStoreError,
    WindowsCredentialStore,
)


class _WorkerBackend(Protocol):
    async def generate(self, request: ModelRequest) -> ModelResponse: ...

    async def verify_models(self) -> tuple[str, ...]: ...

    async def aclose(self) -> None: ...


BackendFactory = Callable[[ModelWorkerConfig], Awaitable[_WorkerBackend]]


async def _build_backend(config: ModelWorkerConfig) -> _WorkerBackend:
    provider = config.provider
    secret_store: SecretStore
    if provider.secret_name is None:
        secret_store = MappingSecretStore({})
    else:
        credential_store = NamespacedSecretStore(WindowsCredentialStore(), config.profile)
        secret_store = AsyncSecretStoreAdapter(credential_store)
    zero = ModelPrice(Decimal(0), Decimal(0))
    budget = BudgetManager(
        limits=BudgetLimits(daily=Decimal(1), monthly=Decimal(1)),
        prices={
            (provider.provider, provider.flash_model): zero,
            (provider.provider, provider.pro_model): zero,
        },
    )
    return OpenAICompatibleBackend(
        config=provider,
        secret_store=secret_store,
        budget=budget,
    )


class ModelWorkerService:
    """Single-client worker service; malformed protocol state terminates it."""

    def __init__(
        self,
        reader: BinaryIO,
        writer: BinaryIO,
        *,
        backend_factory: BackendFactory = _build_backend,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._backend_factory = backend_factory
        self._backend: _WorkerBackend | None = None

    async def _read(self) -> Envelope:
        return await asyncio.to_thread(read_frame_sync, self._reader)

    async def _write(self, envelope: Envelope) -> None:
        await asyncio.to_thread(write_frame_sync, self._writer, envelope)

    async def _reply(self, request: Envelope, message_type: str, payload: object) -> None:
        if not hasattr(payload, "model_dump"):
            raise TypeError("worker replies require a validated payload model")
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
        code: WorkerErrorCode,
        provider_call_started: bool,
    ) -> None:
        await self._reply(
            request,
            MODEL_ERROR,
            ModelWorkerError(
                code=code,
                provider_call_started=provider_call_started,
            ),
        )

    async def _initialize(self, envelope: Envelope) -> bool:
        if envelope.message_type != MODEL_INIT:
            await self._error(
                envelope,
                code="invalid_request",
                provider_call_started=False,
            )
            return False
        try:
            config = validate_payload(ModelWorkerConfig, envelope.payload)
            backend = await self._backend_factory(config)
            self._backend = backend
            if config.verify_models_on_startup:
                await backend.verify_models()
        except (SecretNotFoundError, SecretStoreError):
            await self._error(
                envelope,
                code="credential_unavailable",
                provider_call_started=False,
            )
            return False
        except (ValidationError, ValueError, ToolSchemaError):
            await self._error(
                envelope,
                code="invalid_request",
                provider_call_started=False,
            )
            return False
        except ModelTransportError:
            await self._error(
                envelope,
                code="provider_transport",
                provider_call_started=True,
            )
            return False
        except ModelProtocolError:
            await self._error(
                envelope,
                code="provider_protocol",
                provider_call_started=True,
            )
            return False
        except Exception:
            await self._error(
                envelope,
                code="internal",
                provider_call_started=True,
            )
            return False
        await self._reply(envelope, MODEL_READY, ModelWorkerReady())
        return True

    async def _generate(self, envelope: Envelope) -> None:
        assert self._backend is not None
        try:
            payload = validate_payload(ModelGenerateRequest, envelope.payload)
        except (ValidationError, ValueError):
            await self._error(
                envelope,
                code="invalid_request",
                provider_call_started=False,
            )
            return
        try:
            response = await self._backend.generate(payload.request)
        except (SecretNotFoundError, SecretStoreError):
            await self._error(
                envelope,
                code="credential_unavailable",
                provider_call_started=False,
            )
        except ModelTransportError:
            await self._error(
                envelope,
                code="provider_transport",
                provider_call_started=True,
            )
        except (ModelProtocolError, ToolCallValidationError, ToolSchemaError):
            await self._error(
                envelope,
                code="provider_protocol",
                provider_call_started=True,
            )
        except Unsupported:
            await self._error(
                envelope,
                code="unsupported",
                provider_call_started=False,
            )
        except Exception:
            await self._error(
                envelope,
                code="internal",
                provider_call_started=True,
            )
        else:
            await self._reply(
                envelope,
                MODEL_RESULT,
                ModelGenerateResult(response=response),
            )

    async def run(self) -> int:
        try:
            first = await self._read()
        except IPCError:
            return 2
        if not await self._initialize(first):
            await self._close_backend()
            return 2
        while True:
            try:
                envelope = await self._read()
            except IPCError:
                await self._close_backend()
                return 0
            if envelope.message_type == MODEL_GENERATE:
                await self._generate(envelope)
                continue
            if envelope.message_type == MODEL_VERIFY:
                assert self._backend is not None
                try:
                    validate_payload(EmptyWorkerRequest, envelope.payload)
                    models = await self._backend.verify_models()
                except (ValidationError, ValueError):
                    await self._error(
                        envelope,
                        code="invalid_request",
                        provider_call_started=False,
                    )
                except (SecretNotFoundError, SecretStoreError):
                    await self._error(
                        envelope,
                        code="credential_unavailable",
                        provider_call_started=False,
                    )
                except ModelTransportError:
                    await self._error(
                        envelope,
                        code="provider_transport",
                        provider_call_started=True,
                    )
                except ModelProtocolError:
                    await self._error(
                        envelope,
                        code="provider_protocol",
                        provider_call_started=True,
                    )
                except Exception:
                    await self._error(
                        envelope,
                        code="internal",
                        provider_call_started=True,
                    )
                else:
                    await self._reply(
                        envelope,
                        MODEL_VERIFY_RESULT,
                        ModelVerifyResult(model_ids=models),
                    )
                continue
            if envelope.message_type == WORKER_SHUTDOWN:
                try:
                    validate_payload(EmptyWorkerRequest, envelope.payload)
                except (ValidationError, ValueError):
                    await self._error(
                        envelope,
                        code="invalid_request",
                        provider_call_started=False,
                    )
                    await self._close_backend()
                    return 2
                await self._reply(envelope, WORKER_STOPPED, ModelWorkerReady())
                await self._close_backend()
                return 0
            await self._error(
                envelope,
                code="invalid_request",
                provider_call_started=False,
            )
            await self._close_backend()
            return 2

    async def _close_backend(self) -> None:
        backend, self._backend = self._backend, None
        if backend is not None:
            try:
                await backend.aclose()
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
        reader = sys.stdin.buffer
        writer = sys.stdout.buffer
        return asyncio.run(ModelWorkerService(reader, writer).run())
    except BaseException:
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
