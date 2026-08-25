"""Validated approval records and deliberately redacted list projections."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lemonbot.domain import ApprovalState

MAX_APPROVAL_ARGUMENT_BYTES = 256 * 1024
_SAFE_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_SENSITIVE_FIELD_NAME = re.compile(
    r"(?i)(?:authorization|cookie|credential|key|mfa|otp|pass(?:word)?|secret|token)"
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("approval timestamps must include a timezone")
    return value.astimezone(UTC)


def _shape(value: object, *, sensitive: bool) -> str:
    if sensitive:
        return "sensitive"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return f"string({len(value)} chars)"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return f"array({len(value)} items)"
    if isinstance(value, dict):
        return f"object({len(value)} fields)"
    return "unsupported"


def summarize_arguments(arguments: dict[str, Any]) -> str:
    """Describe argument structure without returning any argument values."""

    if not arguments:
        return "no arguments"
    fields: list[str] = []
    for index, name in enumerate(sorted(arguments)):
        if index == 32:
            fields.append(f"… (+{len(arguments) - index} fields)")
            break
        display_name = name if _SAFE_FIELD_NAME.fullmatch(name) else "<field>"
        sensitive = bool(_SENSITIVE_FIELD_NAME.search(name))
        fields.append(f"{display_name}=<{_shape(arguments[name], sensitive=sensitive)}>")
    return ", ".join(fields)


def canonicalize_arguments(
    arguments: dict[str, Any],
) -> tuple[dict[str, Any], str, str]:
    """Return a JSON-only copy, SHA-256 binding and non-value summary."""

    try:
        encoded = json.dumps(
            arguments,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("approval arguments must be finite JSON values") from exc
    if len(encoded) > MAX_APPROVAL_ARGUMENT_BYTES:
        raise ValueError("approval arguments exceed the 256 KiB limit")
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
        raise ValueError("approval arguments must be a JSON object with string keys")
    normalized: dict[str, Any] = decoded
    digest = hashlib.sha256(encoded).hexdigest()
    return normalized, digest, summarize_arguments(normalized)


class ApprovalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ApprovalRequest(ApprovalModel):
    """Complete durable record used only when creating an approval."""

    approval_id: UUID
    profile: str = Field(min_length=1, max_length=32)
    channel: str = Field(min_length=1, max_length=64)
    chat_id: str = Field(min_length=1, max_length=512)
    event_id: str = Field(min_length=1, max_length=256)
    tool_name: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,128}$")
    action_kind: str = Field(min_length=1, max_length=128)
    arguments_summary: str = Field(min_length=1, max_length=2_000)
    arguments_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    arguments: dict[str, Any]
    created_at: datetime
    expires_at: datetime

    @field_validator("profile", "channel", "chat_id", "event_id", "action_kind")
    @classmethod
    def exact_identifier(cls, value: str) -> str:
        if value != value.strip() or "\0" in value:
            raise ValueError("approval identifiers must be exact and contain no NUL")
        return value

    @field_validator("created_at", "expires_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_binding(self) -> ApprovalRequest:
        if self.expires_at <= self.created_at:
            raise ValueError("approval must expire after it is created")
        normalized, digest, summary = canonicalize_arguments(self.arguments)
        if normalized != self.arguments:
            raise ValueError("approval arguments are not canonical JSON values")
        if digest != self.arguments_sha256 or summary != self.arguments_summary:
            raise ValueError("approval argument binding does not match its parameters")
        return self


class ApprovalListItem(ApprovalModel):
    """Safe projection for list/status APIs; full arguments are intentionally absent."""

    approval_id: UUID
    profile: str
    channel: str
    chat_id: str
    event_id: str
    tool_name: str
    action_kind: str
    arguments_summary: str
    arguments_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: ApprovalState
    created_at: datetime
    expires_at: datetime
    claimed_at: datetime | None = None
    resolved_at: datetime | None = None
    outcome_code: str | None = None

    @field_validator("created_at", "expires_at", "claimed_at", "resolved_at")
    @classmethod
    def normalize_optional_time(cls, value: datetime | None) -> datetime | None:
        return _utc(value) if value is not None else None


class ApprovalClaim(ApprovalListItem):
    """One-time execution capability returned only by the atomic claim operation."""

    claim_token: UUID
    arguments: dict[str, Any]

    @model_validator(mode="after")
    def require_executing_binding(self) -> ApprovalClaim:
        if self.state is not ApprovalState.EXECUTING:
            raise ValueError("an approval claim must be in executing state")
        normalized, digest, summary = canonicalize_arguments(self.arguments)
        if normalized != self.arguments:
            raise ValueError("claimed arguments are not canonical JSON values")
        if digest != self.arguments_sha256 or summary != self.arguments_summary:
            raise ValueError("claimed parameters do not match the durable binding")
        return self
