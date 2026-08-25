"""SQLAlchemy persistence schema for the core event pipeline."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from lemonbot.domain import ApprovalState, InboxState, OutboxState, utc_now


class Base(DeclarativeBase):
    pass


class InboxRow(Base):
    __tablename__ = "inbox"
    __table_args__ = (
        UniqueConstraint("channel", "event_id", name="uq_inbox_channel_event"),
        Index("ix_inbox_claim", "state", "occurred_at", "id"),
        Index("ix_inbox_chat_order", "channel", "chat_id", "state", "occurred_at", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    event_id: Mapped[str] = mapped_column(String(256), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(512), nullable=False)
    sender_id: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    state: Mapped[str] = mapped_column(String(32), default=InboxState.PENDING.value, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    model_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class DraftRow(Base):
    """A generated reply that is durable but is never eligible for dispatch."""

    __tablename__ = "drafts"
    __table_args__ = (
        UniqueConstraint("draft_id", name="uq_drafts_draft_id"),
        UniqueConstraint("channel", "reply_to_event_id", name="uq_drafts_one_reply_per_event"),
        CheckConstraint("state IN ('pending')", name="ck_drafts_state"),
        Index("ix_drafts_pending", "state", "created_at", "id"),
        Index("ix_drafts_scope", "channel", "chat_id", "state", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    draft_id: Mapped[str] = mapped_column(String(36), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(512), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    reply_to_event_id: Mapped[str] = mapped_column(String(256), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class OutboxRow(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_outbox_message_id"),
        UniqueConstraint("channel", "reply_to_event_id", name="uq_outbox_one_reply_per_event"),
        Index("ix_outbox_dispatch", "state", "created_at", "id"),
        Index("ix_outbox_eligibility", "state", "next_attempt_at", "created_at", "id"),
        Index("ix_outbox_rate", "channel", "chat_id", "created_at", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(String(36), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(512), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    reply_to_event_id: Mapped[str | None] = mapped_column(String(256))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    proactive: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    state: Mapped[str] = mapped_column(
        String(32), default=OutboxState.PENDING.value, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatch_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_id: Mapped[str | None] = mapped_column(String(512))
    failure_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class MessageRow(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_recent", "channel", "chat_id", "occurred_at", "id"),
        Index("ix_messages_external", "channel", "external_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(512), nullable=False)
    sender_id: Mapped[str | None] = mapped_column(String(512))
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(512))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class AuditRow(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_time", "occurred_at", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome: Mapped[str] = mapped_column(String(128), nullable=False)
    channel: Mapped[str | None] = mapped_column(String(64))
    chat_id: Mapped[str | None] = mapped_column(String(512))
    event_id: Mapped[str | None] = mapped_column(String(256))
    message_id: Mapped[str | None] = mapped_column(String(36))
    rule_id: Mapped[str | None] = mapped_column(String(128))
    detail_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AllowlistRow(Base):
    __tablename__ = "allowlist"
    __table_args__ = (UniqueConstraint("channel", "chat_id", name="uq_allowlist_channel_chat"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(512), nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    label: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ApprovalRow(Base):
    """A one-time authorization bound to one exact broker-owned tool action."""

    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint("claim_token", name="uq_approvals_claim_token"),
        UniqueConstraint(
            "profile",
            "channel",
            "chat_id",
            "event_id",
            "tool_name",
            "action_kind",
            "arguments_sha256",
            name="uq_approvals_action_binding",
        ),
        CheckConstraint(
            "state IN ('pending','executing','approved','denied','unknown')",
            name="ck_approvals_state",
        ),
        CheckConstraint(
            "(state = 'pending' AND claim_token IS NULL AND claimed_at IS NULL "
            "AND resolved_at IS NULL) OR "
            "(state = 'executing' AND claim_token IS NOT NULL AND claimed_at IS NOT NULL "
            "AND resolved_at IS NULL) OR "
            "(state IN ('approved','unknown') AND claim_token IS NOT NULL "
            "AND claimed_at IS NOT NULL AND resolved_at IS NOT NULL) OR "
            "(state = 'denied' AND resolved_at IS NOT NULL)",
            name="ck_approvals_lifecycle_fields",
        ),
        Index("ix_approvals_pending", "profile", "state", "expires_at", "created_at"),
        Index(
            "ix_approvals_scope",
            "profile",
            "channel",
            "chat_id",
            "event_id",
        ),
    )

    approval_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    profile: Mapped[str] = mapped_column(String(32), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(512), nullable=False)
    event_id: Mapped[str] = mapped_column(String(256), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    action_kind: Mapped[str] = mapped_column(String(128), nullable=False)
    arguments_summary: Mapped[str] = mapped_column(Text, nullable=False)
    arguments_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    state: Mapped[str] = mapped_column(
        String(32), default=ApprovalState.PENDING.value, nullable=False
    )
    claim_token: Mapped[str | None] = mapped_column(String(36))
    outcome_code: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ToolExecutionRow(Base):
    """Durable, value-minimised audit state for every model-requested tool call."""

    __tablename__ = "tool_executions"
    __table_args__ = (
        UniqueConstraint(
            "profile",
            "channel",
            "chat_id",
            "event_id",
            "call_id",
            name="uq_tool_executions_call_binding",
        ),
        CheckConstraint(
            "state IN ('requested','executing','succeeded','failed',"
            "'denied','approval_pending','unknown')",
            name="ck_tool_executions_state",
        ),
        Index("ix_tool_executions_scope", "profile", "channel", "chat_id", "event_id"),
        Index("ix_tool_executions_state", "profile", "state", "created_at"),
    )

    execution_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    profile: Mapped[str] = mapped_column(String(32), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(512), nullable=False)
    event_id: Mapped[str] = mapped_column(String(256), nullable=False)
    call_id: Mapped[str] = mapped_column(String(256), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    action_kind: Mapped[str] = mapped_column(String(128), nullable=False)
    arguments_summary: Mapped[str] = mapped_column(Text, nullable=False)
    arguments_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    side_effect: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome_code: Mapped[str | None] = mapped_column(String(128))
    result_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class RuntimeStateRow(Base):
    __tablename__ = "runtime_state"

    key: Mapped[str] = mapped_column(String(512), primary_key=True)
    value_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
