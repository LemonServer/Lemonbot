"""Stable, serialisable types shared by Lemonbot subsystems.

The domain package intentionally contains no database, transport, or vendor
dependencies.  Worker processes can therefore exchange these models as JSON
without importing the core runtime.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(UTC)


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EventKind(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    ENTER_CHAT = "enter_chat"
    SYSTEM = "system"


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class DeliveryStatus(StrEnum):
    ACKNOWLEDGED = "acknowledged"
    FAILED = "failed"
    UNKNOWN = "unknown"


class InboxState(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    DEAD = "dead"


class OutboxState(StrEnum):
    PENDING = "pending"
    RESERVED = "reserved"
    DISPATCHING = "dispatching"
    ACKNOWLEDGED = "acknowledged"
    UNKNOWN = "unknown"
    DEAD = "dead"


class PolicyDecision(StrEnum):
    AUTO = "AUTO"
    APPROVE_ONCE = "APPROVE_ONCE"
    ENROLL = "ENROLL"
    DENY = "DENY"


class ApprovalState(StrEnum):
    PENDING = "pending"
    EXECUTING = "executing"
    APPROVED = "approved"
    DENIED = "denied"
    UNKNOWN = "unknown"


class DataClass(StrEnum):
    PUBLIC = "PUBLIC"
    CONVERSATION = "CONVERSATION"
    PRIVATE_LOCAL = "PRIVATE_LOCAL"
    SECRET = "SECRET"  # noqa: S105 - classification label, not a credential


class InboundEvent(DomainModel):
    channel: str = Field(min_length=1, max_length=64)
    event_id: str = Field(min_length=1, max_length=256)
    chat_id: str = Field(min_length=1, max_length=512)
    sender_id: str = Field(min_length=1, max_length=512)
    text: str | None = Field(default=None, max_length=100_000)
    kind: EventKind = EventKind.TEXT
    occurred_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    _normalise_time = field_validator("occurred_at")(_aware_utc)


class OutboundMessage(DomainModel):
    message_id: UUID = Field(default_factory=uuid4)
    channel: str = Field(min_length=1, max_length=64)
    chat_id: str = Field(min_length=1, max_length=512)
    text: str = Field(min_length=1, max_length=3_000)
    reply_to_event_id: str | None = Field(default=None, max_length=256)
    created_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    _normalise_time = field_validator("created_at")(_aware_utc)


class DeliveryReceipt(DomainModel):
    message_id: UUID
    status: DeliveryStatus
    external_id: str | None = Field(default=None, max_length=512)
    acknowledged_at: datetime | None = None
    detail: str | None = Field(default=None, max_length=2_000)

    @field_validator("acknowledged_at")
    @classmethod
    def normalise_acknowledged_at(cls, value: datetime | None) -> datetime | None:
        return _aware_utc(value) if value is not None else None


class ConnectorHealth(DomainModel):
    healthy: bool
    detail: str | None = Field(default=None, max_length=2_000)
    checked_at: datetime = Field(default_factory=utc_now)
    account_id: str | None = Field(default=None, max_length=512)

    _normalise_time = field_validator("checked_at")(_aware_utc)


class ToolCall(DomainModel):
    call_id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ModelMessage(DomainModel):
    role: MessageRole
    content: str | None = Field(default=None, max_length=500_000)
    name: str | None = Field(default=None, max_length=128)
    tool_call_id: str | None = Field(default=None, max_length=256)
    tool_calls: tuple[ToolCall, ...] = ()
    # Ephemeral protocol field needed by DeepSeek thinking+tool calls.  The
    # storage schema intentionally has no place to persist it.
    reasoning_content: str | None = Field(default=None, max_length=500_000, repr=False)

    @model_validator(mode="after")
    def content_or_tool_call(self) -> ModelMessage:
        if self.content is None and not self.tool_calls:
            raise ValueError("a model message requires content or tool calls")
        return self


class ModelCapabilities(DomainModel):
    tools: bool = False
    json_output: bool = False
    thinking: bool = False
    vision: bool = False
    embeddings: bool = False
    context_tokens: int = Field(default=32_768, ge=1)


class ModelRequest(DomainModel):
    messages: tuple[ModelMessage, ...]
    tools: tuple[ToolManifest, ...] = ()
    temperature: float = Field(default=0.3, ge=0, le=2)
    max_tokens: int = Field(default=1_500, ge=1)
    response_format: Literal["text", "json"] = "text"
    deep: bool = False
    correlation_id: str | None = Field(default=None, max_length=256)


class ModelResponse(DomainModel):
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    reasoning_content: str | None = Field(default=None, max_length=500_000, repr=False)
    model: str = Field(default="unknown", min_length=1, max_length=256)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    finish_reason: str | None = Field(default=None, max_length=128)


class ToolManifest(DomainModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,128}$")
    description: str = Field(max_length=2_000)
    input_schema: dict[str, Any]
    action_kind: str = Field(min_length=1, max_length=128)
    side_effect: bool = False
    risk_level: Literal["low", "medium", "high", "prohibited"] = "low"
    idempotent: bool = True
    required_scopes: frozenset[str] = frozenset()
    allowed_data: frozenset[DataClass] = frozenset({DataClass.PUBLIC})
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    max_output_bytes: int = Field(default=64 * 1024, ge=256, le=1024 * 1024)
    monetary_cost_cny: str = "0"


class ToolContext(DomainModel):
    profile: str = ""
    channel: str
    chat_id: str
    event_id: str
    principal_id: str = ""
    granted_scopes: frozenset[str] = frozenset()
    data_class: DataClass = DataClass.CONVERSATION
    deadline: datetime | None = None

    @field_validator("deadline")
    @classmethod
    def normalise_deadline(cls, value: datetime | None) -> datetime | None:
        return _aware_utc(value) if value is not None else None


class ToolResult(DomainModel):
    ok: bool
    content: str = Field(default="", max_length=500_000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    facts: tuple[dict[str, Any], ...] = ()
    artifacts: tuple[str, ...] = ()
    error_code: str | None = Field(default=None, max_length=128)
    truncated: bool = False
    side_effect_committed: bool = False
    state_unknown: bool = False


class ProposedAction(DomainModel):
    """An action proposed to policy, with broker-owned routing identity.

    ``bound_*`` values come from the inbound envelope, never from model output.
    A mismatch is always denied.
    """

    kind: str = Field(min_length=1, max_length=128)
    channel: str | None = Field(default=None, max_length=64)
    chat_id: str | None = Field(default=None, max_length=512)
    bound_channel: str | None = Field(default=None, max_length=64)
    bound_chat_id: str | None = Field(default=None, max_length=512)
    reason_event_id: str | None = Field(default=None, max_length=256)
    proactive: bool = False
    side_effect: bool = False
    tool_name: str | None = Field(default=None, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)
    requested_at: datetime = Field(default_factory=utc_now)

    _normalise_time = field_validator("requested_at")(_aware_utc)


class PolicyEvaluation(DomainModel):
    decision: PolicyDecision
    reason: str = Field(min_length=1, max_length=2_000)
    rule_id: str = Field(min_length=1, max_length=128)


class InboxItem(DomainModel):
    id: int
    event: InboundEvent
    state: InboxState
    attempts: int = Field(ge=0)
    claimed_at: datetime | None = None


class ConversationMessage(DomainModel):
    id: int
    channel: str
    chat_id: str
    sender_id: str | None = None
    role: MessageRole
    content: str
    external_id: str | None = None
    occurred_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    _normalise_time = field_validator("occurred_at")(_aware_utc)


class OutboxItem(DomainModel):
    id: int
    message: OutboundMessage
    state: OutboxState
    attempts: int = Field(ge=0)
    reserved_at: datetime | None = None
    dispatch_started_at: datetime | None = None


class AuditRecord(DomainModel):
    action: str
    outcome: str
    channel: str | None = None
    chat_id: str | None = None
    event_id: str | None = None
    message_id: UUID | None = None
    rule_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=utc_now)


class Unsupported(RuntimeError):
    """Raised when a model backend does not implement an optional capability."""
