from __future__ import annotations

import io

import pytest
from pydantic import ValidationError

from lemonbot.domain.models import MessageRole, ModelMessage, ModelRequest, ModelResponse
from lemonbot.ipc import Envelope, read_frame_sync, write_frame_sync
from lemonbot.models.config import ProviderConfig
from lemonbot.models.gateway import ModelGatewayError
from lemonbot.models.worker import ModelWorkerService
from lemonbot.models.worker_protocol import (
    MODEL_ERROR,
    MODEL_GENERATE,
    MODEL_INIT,
    MODEL_READY,
    MODEL_RESULT,
    WORKER_SHUTDOWN,
    WORKER_STOPPED,
    ModelGenerateRequest,
    ModelGenerateResult,
    ModelWorkerConfig,
    ModelWorkerError,
    validate_payload,
)
from lemonbot.models.worker_proxy import IsolatedModelError


def _local_config() -> ModelWorkerConfig:
    return ModelWorkerConfig(
        profile="lab",
        provider=ProviderConfig(
            provider="openai_compatible",
            base_url="http://127.0.0.1:11434/v1",
            secret_name=None,
            flash_model="flash",
            pro_model="pro",
            timeout_seconds=5,
            context_tokens=32_768,
        ),
        verify_models_on_startup=False,
    )


def test_isolated_worker_errors_share_the_gateway_fail_closed_boundary() -> None:
    assert issubclass(IsolatedModelError, ModelGatewayError)


class _FakeBackend:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.requests: list[ModelRequest] = []
        self.closed = False

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ModelResponse(
            content="worker reply",
            model="flash",
            prompt_tokens=10,
            completion_tokens=2,
        )

    async def verify_models(self) -> tuple[str, ...]:
        return ("flash", "pro")

    async def aclose(self) -> None:
        self.closed = True


def _request() -> ModelRequest:
    return ModelRequest(
        messages=(ModelMessage(role=MessageRole.USER, content="hello"),),
    )


def _worker_input(config: ModelWorkerConfig, request: ModelRequest) -> io.BytesIO:
    buffer = io.BytesIO()
    write_frame_sync(
        buffer,
        Envelope(message_type=MODEL_INIT, payload=config.model_dump(mode="json")),
    )
    write_frame_sync(
        buffer,
        Envelope(
            message_type=MODEL_GENERATE,
            payload=ModelGenerateRequest(request=request).model_dump(mode="json"),
        ),
    )
    write_frame_sync(buffer, Envelope(message_type=WORKER_SHUTDOWN, payload={}))
    buffer.seek(0)
    return buffer


async def test_worker_service_round_trips_only_validated_models() -> None:
    backend = _FakeBackend()

    async def factory(_config: ModelWorkerConfig) -> _FakeBackend:
        return backend

    output = io.BytesIO()
    service = ModelWorkerService(
        _worker_input(_local_config(), _request()),
        output,
        backend_factory=factory,
    )

    assert await service.run() == 0
    output.seek(0)
    ready = read_frame_sync(output)
    result = read_frame_sync(output)
    stopped = read_frame_sync(output)

    assert ready.message_type == MODEL_READY
    assert result.message_type == MODEL_RESULT
    assert result.payload["response"]["content"] == "worker reply"
    assert stopped.message_type == WORKER_STOPPED
    assert backend.requests == [_request()]
    assert backend.closed


async def test_worker_error_frame_never_echoes_exception_or_request_text() -> None:
    canary = "sk-never-serialize-this-canary"
    backend = _FakeBackend(error=RuntimeError(canary))

    async def factory(_config: ModelWorkerConfig) -> _FakeBackend:
        return backend

    request = ModelRequest(
        messages=(ModelMessage(role=MessageRole.USER, content="private request canary"),)
    )
    output = io.BytesIO()
    service = ModelWorkerService(
        _worker_input(_local_config(), request),
        output,
        backend_factory=factory,
    )

    assert await service.run() == 0
    raw = output.getvalue()
    assert canary.encode() not in raw
    assert b"private request canary" not in raw
    output.seek(0)
    assert read_frame_sync(output).message_type == MODEL_READY
    error_frame = read_frame_sync(output)
    assert error_frame.message_type == MODEL_ERROR
    error = validate_payload(ModelWorkerError, error_frame.payload)
    assert error.code == "internal"
    assert error.provider_call_started


def test_worker_config_rejects_serialized_keys_and_unsafe_endpoints() -> None:
    config = _local_config().model_dump(mode="json")
    config["api_key"] = "must-not-cross-ipc"
    with pytest.raises(ValidationError):
        validate_payload(ModelWorkerConfig, config)

    config = _local_config().model_dump(mode="json")
    config["provider"]["base_url"] = "http://192.0.2.10/v1"
    with pytest.raises(ValidationError, match="loopback-only"):
        validate_payload(ModelWorkerConfig, config)

    deepseek = ProviderConfig.deepseek().model_copy(update={"secret_name": "sk-real-looking"})
    with pytest.raises(ValueError, match="appears to contain"):
        ModelWorkerConfig(profile="prod", provider=deepseek)


def test_worker_config_payload_contains_lookup_name_but_no_credential_value() -> None:
    config = ModelWorkerConfig(profile="prod", provider=ProviderConfig.deepseek())
    serialized = config.model_dump_json()

    assert "deepseek_api_key" in serialized
    assert "Authorization" not in serialized
    assert "Bearer " not in serialized


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("content", 123),
        ("prompt_tokens", "10"),
        ("completion_tokens", True),
    ],
)
def test_worker_response_payload_does_not_coerce_types(field: str, value: object) -> None:
    response: dict[str, object] = {
        "content": "ok",
        "tool_calls": [],
        "model": "flash",
        "prompt_tokens": 10,
        "completion_tokens": 2,
    }
    response[field] = value

    with pytest.raises(ValidationError):
        validate_payload(ModelGenerateResult, {"response": response})
