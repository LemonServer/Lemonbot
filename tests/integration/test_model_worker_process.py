from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from lemonbot.domain.models import MessageRole, ModelMessage, ModelRequest
from lemonbot.ipc import Envelope, read_frame, write_frame
from lemonbot.models import (
    BudgetLimits,
    BudgetManager,
    IsolatedModelBackend,
    ModelPrice,
    ModelWorkerConfig,
    ModelWorkerUnavailable,
    ProviderConfig,
)
from lemonbot.models.worker_protocol import MODEL_ERROR, MODEL_INIT
from lemonbot.supervisor import WorkerSupervisor

_RUNTIME_WORKER_CWD = Path("src/lemonbot").resolve()


class _ProviderServer:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str], dict[str, Any] | None]] = []
        self.chat_received = asyncio.Event()
        self.release_chat = asyncio.Event()
        self.block_chat = False

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            lines = header.decode("ascii").split("\r\n")
            method, path, _version = lines[0].split(" ", 2)
            headers = {
                key.strip().casefold(): value.strip()
                for line in lines[1:]
                if ":" in line
                for key, value in (line.split(":", 1),)
            }
            length = int(headers.get("content-length", "0"))
            body_raw = await reader.readexactly(length) if length else b""
            body = json.loads(body_raw) if body_raw else None
            self.requests.append((path, headers, body))
            if path.endswith("/models"):
                payload = {"data": [{"id": "flash"}, {"id": "pro"}]}
            elif path.endswith("/chat/completions") and method == "POST":
                self.chat_received.set()
                if self.block_chat:
                    await self.release_chat.wait()
                payload = {
                    "choices": [
                        {
                            "message": {"content": "isolated reply"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 3},
                }
            else:
                payload = {"error": "unexpected test route"}
            encoded = json.dumps(payload).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                + f"Content-Length: {len(encoded)}\r\n".encode()
                + b"Content-Type: application/json\r\nConnection: close\r\n\r\n"
                + encoded
            )
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            return
        finally:
            writer.close()
            await writer.wait_closed()


async def _serve_provider(
    body: Callable[[_ProviderServer, str], Awaitable[None]],
) -> None:
    provider = _ProviderServer()
    server = await asyncio.start_server(provider.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        await body(provider, f"http://127.0.0.1:{port}/v1")
    finally:
        provider.release_chat.set()
        server.close()
        await server.wait_closed()


def _config(base_url: str, *, verify: bool) -> ModelWorkerConfig:
    return ModelWorkerConfig(
        profile="lab",
        provider=ProviderConfig(
            provider="openai_compatible",
            base_url=base_url,
            secret_name=None,
            flash_model="flash",
            pro_model="pro",
            timeout_seconds=5,
            context_tokens=32_768,
        ),
        verify_models_on_startup=verify,
    )


def _budget() -> BudgetManager:
    price = ModelPrice(Decimal(1), Decimal(1))
    return BudgetManager(
        limits=BudgetLimits(daily=Decimal(10), monthly=Decimal(100)),
        prices={
            ("openai_compatible", "flash"): price,
            ("openai_compatible", "pro"): price,
        },
    )


def _request(text: str = "hello") -> ModelRequest:
    return ModelRequest(
        messages=(ModelMessage(role=MessageRole.USER, content=text),),
        max_tokens=100,
    )


@pytest.mark.integration
async def test_real_model_worker_calls_provider_without_core_credential() -> None:
    async def scenario(provider: _ProviderServer, base_url: str) -> None:
        supervisor = WorkerSupervisor()
        model: IsolatedModelBackend | None = None
        try:
            model = await IsolatedModelBackend.create(
                config=_config(base_url, verify=True),
                budget=_budget(),
                supervisor=supervisor,
                cwd=_RUNTIME_WORKER_CWD,
                rpc_timeout_seconds=15,
            )
            response = await model.generate(_request())
            assert response.content == "isolated reply"
            assert response.model == "flash"
            assert response.prompt_tokens == 11
            assert [item[0] for item in provider.requests] == [
                "/v1/models",
                "/v1/chat/completions",
            ]
            assert all(
                "authorization" not in headers
                for _path, headers, _body in provider.requests
            )
            assert provider.requests[-1][2]["model"] == "flash"
        finally:
            if model is not None:
                await model.aclose()
            await supervisor.stop_all()

    await _serve_provider(scenario)


@pytest.mark.integration
async def test_worker_crash_during_provider_call_is_charged_unknown_and_not_restarted() -> None:
    async def scenario(provider: _ProviderServer, base_url: str) -> None:
        provider.block_chat = True
        supervisor = WorkerSupervisor()
        budget = _budget()
        model = await IsolatedModelBackend.create(
            config=_config(base_url, verify=False),
            budget=budget,
            supervisor=supervisor,
            cwd=_RUNTIME_WORKER_CWD,
            rpc_timeout_seconds=15,
        )
        try:
            call = asyncio.create_task(model.generate(_request("crash after delivery")))
            await asyncio.wait_for(provider.chat_received.wait(), timeout=10)
            model._worker.process.kill()
            with pytest.raises(ModelWorkerUnavailable):
                await call
            first = await budget.snapshot()
            assert first.daily_reserved == 0
            assert first.daily_spent > 0

            with pytest.raises(ModelWorkerUnavailable):
                await model.generate(_request("must not restart"))
            second = await budget.snapshot()
            assert second == first
        finally:
            provider.release_chat.set()
            await model.aclose()
            await supervisor.stop_all()

    await _serve_provider(scenario)


@pytest.mark.integration
async def test_real_worker_rejects_secret_payload_without_stdout_or_stderr_echo() -> None:
    supervisor = WorkerSupervisor()
    worker = await supervisor.spawn(
        "secret-rejection-worker",
        Path(sys.executable),
        "-I",
        "-m",
        "lemonbot.models.worker",
        cwd=_RUNTIME_WORKER_CWD,
        memory_limit_bytes=512 * 1024 * 1024,
        max_processes=2,
    )
    canary = "sk-canary-must-never-return"
    try:
        assert worker.process.stdin is not None
        assert worker.process.stdout is not None
        assert worker.process.stderr is not None
        payload = _config("http://127.0.0.1:11434/v1", verify=False).model_dump(
            mode="json"
        )
        payload["api_key"] = canary
        await write_frame(
            worker.process.stdin,
            Envelope(message_type=MODEL_INIT, payload=payload),
        )
        response = await asyncio.wait_for(read_frame(worker.process.stdout), timeout=10)
        assert response.message_type == MODEL_ERROR
        assert canary not in response.model_dump_json()
        await asyncio.wait_for(worker.process.wait(), timeout=10)
        stderr = await worker.process.stderr.read()
        assert canary.encode() not in stderr
        assert stderr == b""
    finally:
        await supervisor.stop_all()
