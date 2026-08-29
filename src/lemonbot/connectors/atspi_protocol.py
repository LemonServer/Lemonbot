"""Strict messages exchanged with the read-only Linux AT-SPI worker."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AtspiMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AtspiTargetSpec(AtspiMessage):
    target_ref: str = Field(pattern=r"^[a-z0-9_-]{1,128}$")
    chat_kind: Literal["private", "group"]
    header_selector: tuple[int, ...] = Field(min_length=1, max_length=32)
    header_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    transcript_selector: tuple[int, ...] = Field(min_length=1, max_length=32)
    self_item_signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    inbound_item_signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    self_body_relative_path: tuple[int, ...] = Field(max_length=16)
    inbound_body_relative_path: tuple[int, ...] = Field(max_length=16)
    sender_relative_path: tuple[int, ...] | None = Field(default=None, max_length=16)
    sender_attribute_key: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_semantic_proof(self) -> AtspiTargetSpec:
        if self.self_item_signature == self.inbound_item_signature:
            raise ValueError("self and inbound signatures must differ")
        if self.chat_kind == "group" and (
            self.sender_relative_path is None or not self.sender_attribute_key
        ):
            raise ValueError("group targets require a stable sender attribute")
        if self.sender_attribute_key in {"name", "description", "display-name"}:
            raise ValueError("display text cannot be used as sender identity")
        return self


class AtspiInit(AtspiMessage):
    schema_version: Literal[1] = 1
    expected_pids: tuple[int, ...] = Field(min_length=1, max_length=16)
    account_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    ui_signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    targets: tuple[AtspiTargetSpec, ...] = Field(min_length=1, max_length=32)
    debounce_ms: int = Field(default=500, ge=100, le=5_000)
    reconcile_seconds: float = Field(default=15, ge=5, le=300)


class AtspiReady(AtspiMessage):
    schema_version: Literal[1] = 1
    worker_version: str = Field(min_length=1, max_length=64)
    matched_pids: tuple[int, ...] = Field(min_length=1, max_length=16)


class AtspiTranscriptItem(AtspiMessage):
    direction: Literal["inbound", "self"]
    sender_ref: str | None = Field(default=None, max_length=128)
    text: str = Field(min_length=1, max_length=100_000)
    occurred_at: datetime | None = None
    structure_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("occurred_at")
    @classmethod
    def require_aware_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        return value


class AtspiSnapshot(AtspiMessage):
    schema_version: Literal[1] = 1
    target_ref: str = Field(pattern=r"^[a-z0-9_-]{1,128}$")
    chat_kind: Literal["private", "group"]
    header_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation: int = Field(ge=0)
    items: tuple[AtspiTranscriptItem, ...] = Field(max_length=500)


class AtspiHealth(AtspiMessage):
    healthy: bool
    detail_code: str = Field(min_length=1, max_length=128)
    active_target_ref: str | None = Field(default=None, max_length=128)


class AtspiWorkerError(AtspiMessage):
    code: str = Field(min_length=1, max_length=128)
    fatal: bool = True


class AtspiShutdown(AtspiMessage):
    reason: str = Field(default="core_shutdown", min_length=1, max_length=128)
