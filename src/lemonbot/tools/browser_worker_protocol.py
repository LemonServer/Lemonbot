"""Strict, secret-free IPC models for the isolated browser reader."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lemonbot.domain import ToolContext, ToolResult

BROWSER_INIT = "browser.init"
BROWSER_READY = "browser.ready"
BROWSER_INVOKE = "browser.invoke"
BROWSER_RESULT = "browser.result"
BROWSER_ERROR = "browser.error"
BROWSER_SHUTDOWN = "browser.shutdown"
BROWSER_STOPPED = "browser.stopped"


class BrowserWorkerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BrowserWorkerConfig(BrowserWorkerPayload):
    max_text_chars: int = Field(default=50_000, ge=1_000, le=200_000)
    timeout_seconds: float = Field(default=30, gt=0, le=120)


class BrowserWorkerReady(BrowserWorkerPayload):
    protocol_version: int = Field(default=1, ge=1, le=1)


class BrowserInvokeRequest(BrowserWorkerPayload):
    context: ToolContext
    arguments: dict[str, Any]


class BrowserInvokeResult(BrowserWorkerPayload):
    result: ToolResult


class BrowserWorkerError(BrowserWorkerPayload):
    code: str = Field(pattern=r"^[a-z_]{1,64}$")


class EmptyBrowserRequest(BrowserWorkerPayload):
    pass


def validate_browser_payload[PayloadT: BrowserWorkerPayload](
    model: type[PayloadT], payload: dict[str, Any]
) -> PayloadT:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("browser worker payload is not strict JSON") from exc
    return model.model_validate_json(encoded, strict=True)
