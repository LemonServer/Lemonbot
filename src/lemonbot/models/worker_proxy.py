"""Core-side ModelBackend proxy for the isolated provider worker."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Sequence
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

from lemonbot.domain.models import (
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    Unsupported,
)
from lemonbot.ipc import Envelope, IPCError, read_frame, write_frame
from lemonbot.models.budget import BudgetManager
from lemonbot.models.config import DeterministicRouter
from lemonbot.models.gateway import (
    ModelGatewayError,
    ModelProtocolError,
    OpenAICompatibleBackend,
)
from lemonbot.models.schema import ToolSchemaRegistry
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
    ModelGenerateRequest,
    ModelGenerateResult,
    ModelVerifyResult,
    ModelWorkerConfig,
    ModelWorkerError,
    ModelWorkerReady,
    validate_payload,
)
from lemonbot.supervisor import WorkerProcess, WorkerSupervisor


class IsolatedModelError(ModelGatewayError):
    """Base class for sanitized isolated-worker failures."""


class ModelWorkerUnavailable(IsolatedModelError):
    """The subprocess or its IPC channel is no longer trustworthy."""


class ModelWorkerRemoteError(IsolatedModelError):
    """The worker reported a sanitized provider or credential failure."""


class _RemoteFailure(Exception):
    def __init__(self, error: ModelWorkerError) -> None:
        super().__init__(error.code)
        self.error = error


class IsolatedModelBackend:
    """A strict FIFO proxy; it never reads or receives an API credential.

    Integration is intentionally explicit::

        model = await IsolatedModelBackend.create(
            config=ModelWorkerConfig(profile="prod", provider=provider_config),
            budget=persistent_budget,
        )

    The caller should register ``model.aclose`` with runtime shutdown.  A
    crashed, timed-out, cancelled, or protocol-invalid worker is permanently
    poisoned; this class never silently restarts or retries a provider call.
    """

    def __init__(
        self,
        *,
        config: ModelWorkerConfig,
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
        self._router = DeterministicRouter(config.provider)
        self._request_lock = asyncio.Lock()
        self._closed = False
        self._poisoned = False
        self._stderr_task = asyncio.create_task(self._discard_stderr())

    @classmethod
    async def create(
        cls,
        *,
        config: ModelWorkerConfig,
        budget: BudgetManager,
        supervisor: WorkerSupervisor | None = None,
        python_executable: Path | None = None,
        cwd: Path | None = None,
        memory_limit_bytes: int | None = 768 * 1024 * 1024,
        rpc_timeout_seconds: float | None = None,
    ) -> IsolatedModelBackend:
        selected_supervisor = supervisor or WorkerSupervisor()
        executable = (python_executable or Path(sys.executable)).resolve(strict=True)
        working_directory = (cwd or Path.cwd()).resolve(strict=True)
        timeout = rpc_timeout_seconds or config.provider.timeout_seconds + 15
        if timeout <= 0 or timeout > 900:
            raise ValueError("model worker RPC timeout must be in (0, 900] seconds")
        worker = await selected_supervisor.spawn(
            f"model-{uuid4().hex}",
            executable,
            "-I",
            "-m",
            "lemonbot.models.worker",
            cwd=working_directory,
            memory_limit_bytes=memory_limit_bytes,
            # uv's Windows venv launcher is a supervised trampoline.  The
            # launcher plus its interpreter consume both allowed job slots;
            # the worker still cannot create an additional child process.
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
                    MODEL_INIT,
                    config.model_dump(mode="json"),
                    expected=MODEL_READY,
                )
                validate_payload(ModelWorkerReady, response.payload)
        except _RemoteFailure as exc:
            await asyncio.shield(proxy._terminate())
            proxy._raise_remote(exc.error)
        except ValueError:
            await asyncio.shield(proxy._terminate())
            raise ModelWorkerUnavailable("model worker initialization failed") from None
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
            await self._supervisor.stop(
                self._worker.name,
                grace_period_seconds=1,
            )
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
            raise ModelWorkerUnavailable("model worker is closed")
        process = self._worker.process
        if process.returncode is not None or process.stdin is None or process.stdout is None:
            await self._terminate()
            raise ModelWorkerUnavailable("model worker exited")
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
            raise ModelWorkerUnavailable("model worker IPC failed closed") from None
        if response.request_id != request.request_id:
            await self._terminate()
            raise ModelWorkerUnavailable("model worker response correlation failed")
        if response.message_type == MODEL_ERROR:
            try:
                error = validate_payload(ModelWorkerError, response.payload)
            except ValueError:
                await self._terminate()
                raise ModelWorkerUnavailable("model worker error payload was invalid") from None
            raise _RemoteFailure(error)
        if response.message_type != expected:
            await self._terminate()
            raise ModelWorkerUnavailable("model worker response type was invalid")
        return response

    def capabilities(self) -> ModelCapabilities:
        provider = self._config.provider
        return ModelCapabilities(
            tools=True,
            json_output=True,
            thinking=provider.enable_thinking_on_pro,
            vision=False,
            embeddings=False,
            context_tokens=provider.context_tokens,
        )

    def count_tokens(self, messages: Sequence[object]) -> int:
        total = 256
        for message in messages:
            if isinstance(message, ModelMessage):
                try:
                    payload: object = OpenAICompatibleBackend._message_payload(message)
                except ModelProtocolError:
                    payload = message.model_dump(mode="json", exclude_none=False)
            elif hasattr(message, "model_dump"):
                payload = message.model_dump(mode="json", exclude_none=False)
            else:
                payload = {"value": repr(message)}
            total += (
                len(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=str,
                    ).encode("utf-8")
                )
                + 64
            )
        return total

    def _validate_and_estimate(self, request: ModelRequest) -> tuple[str, int]:
        if not request.messages:
            raise ModelProtocolError("model request requires at least one message")
        for message in request.messages:
            OpenAICompatibleBackend._message_payload(message)
        ToolSchemaRegistry(request.tools)
        prompt_tokens = self.count_tokens(request.messages)
        if request.tools:
            encoded = json.dumps(
                [tool.model_dump(mode="json") for tool in request.tools],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            prompt_tokens += len(encoded) + 64 * len(request.tools) + 256
        routed = self._router.route(request)
        if prompt_tokens + request.max_tokens > self._config.provider.context_tokens:
            raise ModelProtocolError("request exceeds the configured model context window")
        return routed.model, prompt_tokens

    @staticmethod
    def _raise_remote(error: ModelWorkerError) -> NoReturn:
        messages = {
            "credential_unavailable": "model worker credential is unavailable",
            "invalid_request": "model worker rejected the request",
            "provider_transport": "model provider transport failed",
            "provider_protocol": "model provider protocol failed",
            "unsupported": "model worker operation is unsupported",
            "internal": "model worker failed internally",
        }
        raise ModelWorkerRemoteError(messages[error.code])

    async def _release_budget(self, reservation_id: str) -> None:
        try:
            await self._budget.release(reservation_id)
        except Exception:
            await self._terminate()
            raise IsolatedModelError("model budget release failed closed") from None

    async def _charge_budget_unknown(self, reservation_id: str) -> None:
        try:
            await self._budget.settle_unknown(reservation_id)
        except Exception:
            await self._terminate()
            raise IsolatedModelError("model budget settlement failed closed") from None

    async def _settle_budget(self, reservation_id: str, response: ModelResponse) -> None:
        try:
            if response.prompt_tokens == 0 and response.completion_tokens == 0:
                await self._budget.settle_unknown(reservation_id)
            else:
                await self._budget.settle(
                    reservation_id,
                    prompt_tokens=response.prompt_tokens,
                    completion_tokens=response.completion_tokens,
                )
        except Exception:
            await self._terminate()
            raise IsolatedModelError("model budget settlement failed closed") from None

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

    @staticmethod
    def _validate_response(request: ModelRequest, response: ModelResponse, model: str) -> None:
        if response.model != model:
            raise ModelProtocolError("worker returned a non-routed model identity")
        ToolSchemaRegistry(request.tools).validate_many(response.tool_calls)
        if request.response_format == "json" and response.content is not None:

            def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
                result: dict[str, object] = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate key")
                    result[key] = value
                return result

            def reject_non_finite(_value: str) -> None:
                raise ValueError("non-finite number")

            try:
                value = json.loads(
                    response.content,
                    object_pairs_hook=reject_duplicates,
                    parse_constant=reject_non_finite,
                )
            except (json.JSONDecodeError, ValueError, RecursionError) as exc:
                raise ModelProtocolError("worker returned invalid JSON-mode content") from exc
            if not isinstance(value, dict):
                raise ModelProtocolError("JSON response mode requires an object")

    async def generate(self, request: ModelRequest) -> ModelResponse:
        async with self._request_lock:
            if self._closed or self._poisoned or self._worker.process.returncode is not None:
                await self._terminate()
                raise ModelWorkerUnavailable("model worker is unavailable")
            model, prompt_tokens = self._validate_and_estimate(request)
            reservation = await self._budget.reserve(
                provider=self._config.provider.provider,
                model=model,
                prompt_tokens=prompt_tokens,
                maximum_completion_tokens=request.max_tokens,
            )
            try:
                envelope = await self._rpc(
                    MODEL_GENERATE,
                    ModelGenerateRequest(request=request).model_dump(mode="json"),
                    expected=MODEL_RESULT,
                )
                result = validate_payload(ModelGenerateResult, envelope.payload)
                self._validate_response(request, result.response, model)
            except _RemoteFailure as exc:
                if exc.error.provider_call_started:
                    await self._critical_transition(
                        self._charge_budget_unknown(reservation.reservation_id)
                    )
                else:
                    await self._critical_transition(
                        self._release_budget(reservation.reservation_id)
                    )
                if exc.error.code in {"invalid_request", "internal"}:
                    await self._terminate()
                self._raise_remote(exc.error)
            except asyncio.CancelledError:
                await asyncio.shield(self._charge_budget_unknown(reservation.reservation_id))
                raise
            except (ModelProtocolError, ValueError):
                await self._critical_transition(
                    self._charge_budget_unknown(reservation.reservation_id)
                )
                await self._terminate()
                raise ModelProtocolError("isolated worker response failed validation") from None
            except BaseException:
                try:
                    await asyncio.shield(self._charge_budget_unknown(reservation.reservation_id))
                finally:
                    await asyncio.shield(self._terminate())
                raise
            response = result.response
            await self._critical_transition(
                self._settle_budget(reservation.reservation_id, response)
            )
            return response

    async def verify_models(self) -> tuple[str, ...]:
        async with self._request_lock:
            try:
                envelope = await self._rpc(MODEL_VERIFY, {}, expected=MODEL_VERIFY_RESULT)
                result = validate_payload(ModelVerifyResult, envelope.payload)
            except _RemoteFailure as exc:
                if exc.error.code in {"invalid_request", "internal"}:
                    await self._terminate()
                self._raise_remote(exc.error)
            except ValueError:
                await self._terminate()
                raise ModelWorkerUnavailable(
                    "model worker verification response was invalid"
                ) from None
            required = {
                self._config.provider.flash_model,
                self._config.provider.pro_model,
            }
            if not required.issubset(result.model_ids):
                await self._terminate()
                raise ModelWorkerUnavailable("worker model verification was inconsistent")
            return result.model_ids

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        del texts
        raise Unsupported("isolated text model worker has no embedding backend")

    async def aclose(self) -> None:
        async with self._request_lock:
            if self._closed:
                return
            try:
                stopped = await self._rpc(WORKER_SHUTDOWN, {}, expected=WORKER_STOPPED)
                validate_payload(ModelWorkerReady, stopped.payload)
            except (IsolatedModelError, _RemoteFailure):
                pass
            except ValueError:
                raise ModelWorkerUnavailable("model worker shutdown response was invalid") from None
            finally:
                await self._terminate()

    async def __aenter__(self) -> IsolatedModelBackend:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()
