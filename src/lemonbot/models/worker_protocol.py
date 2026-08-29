"""Strict, secret-free IPC models for the isolated text-model worker."""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lemonbot.domain.models import ModelRequest, ModelResponse
from lemonbot.models.config import DEEPSEEK_BASE_URL, ProviderConfig

MODEL_INIT = "model.init"
MODEL_READY = "model.ready"
MODEL_GENERATE = "model.generate"
MODEL_RESULT = "model.result"
MODEL_VERIFY = "model.verify"
MODEL_VERIFY_RESULT = "model.verify_result"
MODEL_ERROR = "model.error"
WORKER_SHUTDOWN = "worker.shutdown"
WORKER_STOPPED = "worker.stopped"

_SECRET_NAME = re.compile(r"[a-z0-9_-]{1,128}")
ModelId = Annotated[str, Field(min_length=1, max_length=256)]


class WorkerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelWorkerConfig(WorkerPayload):
    """Configuration safe to serialize; it contains a lookup name, never a key."""

    profile: Literal["prod", "lab"]
    provider: ProviderConfig
    verify_models_on_startup: bool = True

    @model_validator(mode="after")
    def constrain_provider_boundary(self) -> ModelWorkerConfig:
        if self.provider.provider not in {"deepseek", "openai_compatible"}:
            raise ValueError("model worker supports only DeepSeek or OpenAI-compatible providers")
        parsed = urlsplit(self.provider.base_url)
        host = parsed.hostname or ""
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.casefold() == "localhost"
        if self.provider.provider == "deepseek":
            if self.provider.base_url != DEEPSEEK_BASE_URL:
                raise ValueError("DeepSeek worker requires the official API endpoint")
            if self.provider.secret_name is None:
                raise ValueError("DeepSeek worker requires a Secret Service lookup name")
        if parsed.scheme == "http" and not loopback:
            raise ValueError("plaintext OpenAI-compatible workers are loopback-only")
        if not loopback and self.provider.secret_name is None:
            raise ValueError("remote model workers require a Secret Service lookup name")
        if (
            self.provider.secret_name is not None
            and _SECRET_NAME.fullmatch(self.provider.secret_name) is None
        ):
            raise ValueError("credential lookup name is invalid")
        if self.provider.secret_name is not None and self.provider.secret_name.startswith("sk-"):
            raise ValueError("credential lookup name appears to contain an API key")
        return self


class ModelWorkerReady(WorkerPayload):
    protocol_version: Literal[1] = 1


class EmptyWorkerRequest(WorkerPayload):
    pass


class ModelGenerateRequest(WorkerPayload):
    request: ModelRequest


class ModelGenerateResult(WorkerPayload):
    response: ModelResponse


class ModelVerifyResult(WorkerPayload):
    model_ids: tuple[ModelId, ...] = Field(max_length=10_000)


WorkerErrorCode = Literal[
    "credential_unavailable",
    "invalid_request",
    "provider_transport",
    "provider_protocol",
    "unsupported",
    "internal",
]


class ModelWorkerError(WorkerPayload):
    code: WorkerErrorCode
    provider_call_started: bool


def validate_payload[PayloadT: WorkerPayload](
    model: type[PayloadT], payload: dict[str, Any]
) -> PayloadT:
    """Validate through JSON strict mode so IPC values cannot rely on coercion."""

    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("worker payload is not strict JSON") from exc
    return model.model_validate_json(encoded, strict=True)
