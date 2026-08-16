"""Transactional inbox, outbox, audit, and conversation repository."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, exists, func, or_, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import aliased

from lemonbot.domain import (
    AuditRecord,
    ConversationMessage,
    DeliveryReceipt,
    DeliveryStatus,
    EventKind,
    InboundEvent,
    InboxItem,
    InboxState,
    MessageRole,
    OutboundMessage,
    OutboxItem,
    OutboxState,
    utc_now,
)

from .database import Database
from .models import (
    AllowlistRow,
    AuditRow,
    DraftRow,
    InboxRow,
    MessageRow,
    OutboxRow,
    RuntimeStateRow,
    ToolExecutionRow,
)


def _rowcount(result: object) -> int:
    return int(getattr(result, "rowcount", 0))


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _required_utc(value: datetime | None) -> datetime:
    result = _utc(value)
    if result is None:
        raise ValueError("persisted required timestamp is null")
    return result


def _tool_arguments_metadata(arguments: dict[str, Any]) -> tuple[str, str]:
    canonical = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")

    def shape(value: Any) -> dict[str, Any]:
        if isinstance(value, str):
            return {"type": "string", "length": len(value)}
        if isinstance(value, list):
            return {"type": "array", "length": len(value)}
        if isinstance(value, dict):
            return {"type": "object", "keys": len(value)}
        if value is None:
            return {"type": "null"}
        if isinstance(value, bool):
            return {"type": "boolean"}
        if isinstance(value, int | float):
            return {"type": "number"}
        return {"type": type(value).__name__[:32]}

    summary = json.dumps(
        {key: shape(value) for key, value in sorted(arguments.items())},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical).hexdigest(), summary


class CoreRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def has_inbound_event(self, channel: str, chat_id: str, event_id: str) -> bool:
        if not channel or not chat_id or not event_id:
            return False
        async with self.database.sessions() as session:
            item_id = await session.scalar(
                select(InboxRow.id).where(
                    InboxRow.channel == channel,
                    InboxRow.chat_id == chat_id,
                    InboxRow.event_id == event_id,
                )
            )
        return item_id is not None

    async def inbound_event(
        self, channel: str, chat_id: str, event_id: str
    ) -> InboundEvent | None:
        """Return one exact broker-owned event for approval revalidation."""

        if not channel or not chat_id or not event_id:
            return None
        async with self.database.sessions() as session:
            row = await session.scalar(
                select(InboxRow).where(
                    InboxRow.channel == channel,
                    InboxRow.chat_id == chat_id,
                    InboxRow.event_id == event_id,
                )
            )
        return None if row is None else self._inbox_item(row).event

    async def record_inbound(self, event: InboundEvent) -> bool:
        """Persist an event and its raw message exactly once."""
        async with self.database.sessions() as session, session.begin():
            result = await session.execute(
                sqlite_insert(InboxRow)
                .values(
                    channel=event.channel,
                    event_id=event.event_id,
                    chat_id=event.chat_id,
                    sender_id=event.sender_id,
                    kind=event.kind.value,
                    text=event.text,
                    occurred_at=event.occurred_at,
                    metadata_json=event.metadata,
                    state=InboxState.PENDING.value,
                    attempts=0,
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
                .on_conflict_do_nothing(index_elements=["channel", "event_id"])
            )
            inserted = _rowcount(result) == 1
            if inserted:
                session.add(
                    MessageRow(
                        channel=event.channel,
                        chat_id=event.chat_id,
                        sender_id=event.sender_id,
                        role=MessageRole.USER.value,
                        kind=event.kind.value,
                        content=event.text or "",
                        external_id=event.event_id,
                        occurred_at=event.occurred_at,
                        metadata_json=event.metadata,
                    )
                )
                session.add(
                    AuditRow(
                        action="inbox.receive",
                        outcome="accepted",
                        channel=event.channel,
                        chat_id=event.chat_id,
                        event_id=event.event_id,
                        detail_json={"kind": event.kind.value},
                        occurred_at=utc_now(),
                    )
                )
        return inserted

    async def claim_next_inbox(self, channel: str | None = None) -> InboxItem | None:
        """Claim the oldest eligible event while preserving per-chat FIFO."""
        current = aliased(InboxRow)
        earlier = aliased(InboxRow)
        blocking_earlier = exists(
            select(earlier.id).where(
                earlier.channel == current.channel,
                earlier.chat_id == current.chat_id,
                earlier.state.in_([InboxState.PENDING.value, InboxState.PROCESSING.value]),
                or_(
                    earlier.occurred_at < current.occurred_at,
                    and_(earlier.occurred_at == current.occurred_at, earlier.id < current.id),
                ),
            )
        )
        query = (
            select(current.id)
            .where(current.state == InboxState.PENDING.value, ~blocking_earlier)
            .order_by(current.occurred_at, current.id)
            .limit(1)
        )
        if channel is not None:
            query = query.where(current.channel == channel)

        async with self.database.sessions() as session, session.begin():
            item_id = await session.scalar(query)
            if item_id is None:
                return None
            now = utc_now()
            claimed = await session.execute(
                update(InboxRow)
                .where(InboxRow.id == item_id, InboxRow.state == InboxState.PENDING.value)
                .values(
                    state=InboxState.PROCESSING.value,
                    attempts=InboxRow.attempts + 1,
                    claimed_at=now,
                    updated_at=now,
                )
            )
            if _rowcount(claimed) != 1:
                return None
            row = await session.get(InboxRow, item_id)
            assert row is not None
            return self._inbox_item(row)

    async def complete_inbox(self, item_id: int) -> bool:
        return await self._transition_inbox(item_id, InboxState.PROCESSING, InboxState.COMPLETED)

    async def mark_inbox_model_started(self, item_id: int) -> bool:
        """Persist the monetary/remote commit boundary before provider I/O."""

        async with self.database.sessions() as session, session.begin():
            result = await session.execute(
                update(InboxRow)
                .where(
                    InboxRow.id == item_id,
                    InboxRow.state == InboxState.PROCESSING.value,
                    InboxRow.model_started_at.is_(None),
                )
                .values(model_started_at=utc_now(), updated_at=utc_now())
            )
            if _rowcount(result) == 1:
                return True
            row = await session.get(InboxRow, item_id)
            return bool(
                row is not None
                and row.state == InboxState.PROCESSING.value
                and row.model_started_at is not None
            )

    async def fail_inbox(
        self,
        item_id: int,
        error: str,
        *,
        retryable: bool = True,
        max_attempts: int = 3,
    ) -> InboxState:
        error = error[:2_000]
        async with self.database.sessions() as session, session.begin():
            row = await session.get(InboxRow, item_id)
            if row is None:
                raise KeyError(item_id)
            if row.state != InboxState.PROCESSING.value:
                return InboxState(row.state)
            # Once provider I/O may have begun, another attempt could incur a
            # duplicate charge or repeat a tool-planning chain.  Only failures
            # proven to occur before that durable boundary are retryable.
            safe_to_retry = row.model_started_at is None
            next_state = (
                InboxState.PENDING
                if retryable and safe_to_retry and row.attempts < max_attempts
                else InboxState.DEAD
            )
            row.state = next_state.value
            row.claimed_at = None
            row.last_error = error
            row.updated_at = utc_now()
            session.add(
                AuditRow(
                    action="inbox.process",
                    outcome=next_state.value,
                    channel=row.channel,
                    chat_id=row.chat_id,
                    event_id=row.event_id,
                    detail_json={"error": error, "attempts": row.attempts},
                    occurred_at=utc_now(),
                )
            )
            return next_state

    async def _transition_inbox(self, item_id: int, source: InboxState, target: InboxState) -> bool:
        async with self.database.sessions() as session, session.begin():
            result = await session.execute(
                update(InboxRow)
                .where(InboxRow.id == item_id, InboxRow.state == source.value)
                .values(state=target.value, claimed_at=None, updated_at=utc_now())
            )
            return _rowcount(result) == 1

    async def begin_tool_execution(
        self,
        *,
        profile: str,
        channel: str,
        chat_id: str,
        event_id: str,
        call_id: str,
        tool_name: str,
        action_kind: str,
        arguments: dict[str, Any],
        side_effect: bool,
    ) -> tuple[str, bool]:
        """Persist a value-minimised tool request and reject call-id rebinding."""

        arguments_sha256, arguments_summary = _tool_arguments_metadata(arguments)
        execution_id = str(uuid4())
        now = utc_now()
        async with self.database.sessions() as session, session.begin():
            inserted = await session.execute(
                sqlite_insert(ToolExecutionRow)
                .values(
                    execution_id=execution_id,
                    profile=profile,
                    channel=channel,
                    chat_id=chat_id,
                    event_id=event_id,
                    call_id=call_id,
                    tool_name=tool_name,
                    action_kind=action_kind,
                    arguments_summary=arguments_summary,
                    arguments_sha256=arguments_sha256,
                    side_effect=int(side_effect),
                    state="requested",
                    result_summary_json={},
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=["profile", "channel", "chat_id", "event_id", "call_id"]
                )
            )
            row = await session.scalar(
                select(ToolExecutionRow).where(
                    ToolExecutionRow.profile == profile,
                    ToolExecutionRow.channel == channel,
                    ToolExecutionRow.chat_id == chat_id,
                    ToolExecutionRow.event_id == event_id,
                    ToolExecutionRow.call_id == call_id,
                )
            )
            if row is None:
                raise RuntimeError("tool execution insert failed")
            if (
                row.tool_name != tool_name
                or row.action_kind != action_kind
                or row.arguments_sha256 != arguments_sha256
                or bool(row.side_effect) != side_effect
            ):
                raise ValueError("tool call id was rebound to a different request")
            return row.execution_id, _rowcount(inserted) == 1

    async def mark_tool_executing(self, execution_id: str) -> bool:
        async with self.database.sessions() as session, session.begin():
            now = utc_now()
            result = await session.execute(
                update(ToolExecutionRow)
                .where(
                    ToolExecutionRow.execution_id == execution_id,
                    ToolExecutionRow.state == "requested",
                )
                .values(state="executing", started_at=now, updated_at=now)
            )
            return _rowcount(result) == 1

    async def resolve_tool_execution(
        self,
        execution_id: str,
        *,
        state: str,
        outcome_code: str,
        result_summary: dict[str, Any] | None = None,
    ) -> bool:
        terminal_sources = {
            "succeeded": ("executing",),
            "failed": ("requested", "executing"),
            "denied": ("requested",),
            "approval_pending": ("requested",),
            "unknown": ("executing",),
        }
        sources = terminal_sources.get(state)
        if sources is None:
            raise ValueError("invalid tool execution outcome")
        now = utc_now()
        async with self.database.sessions() as session, session.begin():
            result = await session.execute(
                update(ToolExecutionRow)
                .where(
                    ToolExecutionRow.execution_id == execution_id,
                    ToolExecutionRow.state.in_(sources),
                )
                .values(
                    state=state,
                    outcome_code=outcome_code[:128],
                    result_summary_json=result_summary or {},
                    resolved_at=(None if state == "approval_pending" else now),
                    updated_at=now,
                )
            )
            return _rowcount(result) == 1

    async def create_draft(self, message: OutboundMessage) -> DraftRow:
        """Persist one non-dispatchable draft for an inbound event, idempotently."""

        if message.reply_to_event_id is None:
            raise ValueError("a reply draft must be bound to an inbound event")
        async with self.database.sessions() as session, session.begin():
            await session.execute(
                sqlite_insert(DraftRow)
                .values(
                    draft_id=str(message.message_id),
                    channel=message.channel,
                    chat_id=message.chat_id,
                    text=message.text,
                    reply_to_event_id=message.reply_to_event_id,
                    metadata_json=message.metadata,
                    state="pending",
                    created_at=message.created_at,
                    updated_at=utc_now(),
                )
                .on_conflict_do_nothing()
            )
            row = await session.scalar(
                select(DraftRow).where(
                    or_(
                        DraftRow.draft_id == str(message.message_id),
                        and_(
                            DraftRow.channel == message.channel,
                            DraftRow.reply_to_event_id == message.reply_to_event_id,
                        ),
                    )
                )
            )
            if row is None:
                raise RuntimeError("draft insert failed without a conflicting row")
            return row

    async def pending_drafts(
        self,
        *,
        channel: str | None = None,
        chat_id: str | None = None,
        limit: int = 100,
    ) -> list[DraftRow]:
        """List durable drafts without converting them into outbound messages."""

        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        query = select(DraftRow).where(DraftRow.state == "pending")
        if channel is not None:
            query = query.where(DraftRow.channel == channel)
        if chat_id is not None:
            query = query.where(DraftRow.chat_id == chat_id)
        query = query.order_by(DraftRow.created_at.desc(), DraftRow.id.desc()).limit(limit)
        async with self.database.sessions() as session:
            return list((await session.scalars(query)).all())

    async def create_outbox(self, message: OutboundMessage) -> OutboxItem:
        """Create one durable logical reply; duplicates return the original."""
        async with self.database.sessions() as session, session.begin():
            statement = (
                sqlite_insert(OutboxRow)
                .values(
                    message_id=str(message.message_id),
                    channel=message.channel,
                    chat_id=message.chat_id,
                    text=message.text,
                    reply_to_event_id=message.reply_to_event_id,
                    metadata_json=message.metadata,
                    proactive=int(bool(message.metadata.get("proactive", False))),
                    state=OutboxState.PENDING.value,
                    attempts=0,
                    created_at=message.created_at,
                    updated_at=utc_now(),
                )
                .on_conflict_do_nothing()
            )
            await session.execute(statement)
            conflict_conditions = [OutboxRow.message_id == str(message.message_id)]
            if message.reply_to_event_id is not None:
                conflict_conditions.append(
                    and_(
                        OutboxRow.channel == message.channel,
                        OutboxRow.reply_to_event_id == message.reply_to_event_id,
                    )
                )
            row = await session.scalar(select(OutboxRow).where(or_(*conflict_conditions)))
            if row is None:
                raise RuntimeError("outbox insert failed without a conflicting row")
            # Store the assistant message only if this call inserted this exact
            # outbox record and no matching conversation row already exists.
            existing_message = await session.scalar(
                select(MessageRow.id).where(
                    MessageRow.channel == row.channel,
                    MessageRow.external_id == row.message_id,
                    MessageRow.role == MessageRole.ASSISTANT.value,
                )
            )
            if existing_message is None:
                session.add(
                    MessageRow(
                        channel=row.channel,
                        chat_id=row.chat_id,
                        sender_id=None,
                        role=MessageRole.ASSISTANT.value,
                        kind="text",
                        content=row.text,
                        external_id=row.message_id,
                        occurred_at=_utc(row.created_at) or utc_now(),
                        metadata_json=row.metadata_json,
                    )
                )
            return self._outbox_item(row)

    async def reserve_next_outbox(self, channel: str | None = None) -> OutboxItem | None:
        query = (
            select(OutboxRow.id)
            .where(OutboxRow.state == OutboxState.PENDING.value)
            .order_by(OutboxRow.created_at, OutboxRow.id)
            .limit(1)
        )
        if channel is not None:
            query = query.where(OutboxRow.channel == channel)
        async with self.database.sessions() as session, session.begin():
            item_id = await session.scalar(query)
            if item_id is None:
                return None
            now = utc_now()
            reserved = await session.execute(
                update(OutboxRow)
                .where(OutboxRow.id == item_id, OutboxRow.state == OutboxState.PENDING.value)
                .values(
                    state=OutboxState.RESERVED.value,
                    reserved_at=now,
                    attempts=OutboxRow.attempts + 1,
                    updated_at=now,
                )
            )
            if _rowcount(reserved) != 1:
                return None
            row = await session.get(OutboxRow, item_id)
            assert row is not None
            return self._outbox_item(row)

    async def mark_dispatching(self, item_id: int) -> bool:
        async with self.database.sessions() as session, session.begin():
            now = utc_now()
            result = await session.execute(
                update(OutboxRow)
                .where(OutboxRow.id == item_id, OutboxRow.state == OutboxState.RESERVED.value)
                .values(
                    state=OutboxState.DISPATCHING.value,
                    dispatch_started_at=now,
                    updated_at=now,
                )
            )
            return _rowcount(result) == 1

    async def apply_receipt(self, item_id: int, receipt: DeliveryReceipt) -> OutboxState:
        async with self.database.sessions() as session, session.begin():
            row = await session.get(OutboxRow, item_id)
            if row is None:
                raise KeyError(item_id)
            if row.message_id != str(receipt.message_id):
                raise ValueError("receipt message_id does not match outbox item")
            if row.state != OutboxState.DISPATCHING.value:
                return OutboxState(row.state)
            if receipt.status is DeliveryStatus.ACKNOWLEDGED:
                state = OutboxState.ACKNOWLEDGED
            elif receipt.status is DeliveryStatus.UNKNOWN:
                state = OutboxState.UNKNOWN
            else:
                state = OutboxState.DEAD
            row.state = state.value
            row.external_id = receipt.external_id
            row.acknowledged_at = receipt.acknowledged_at
            row.failure_detail = receipt.detail
            row.updated_at = utc_now()
            session.add(
                AuditRow(
                    action="outbox.deliver",
                    outcome=state.value,
                    channel=row.channel,
                    chat_id=row.chat_id,
                    event_id=row.reply_to_event_id,
                    message_id=row.message_id,
                    detail_json={"external_id": receipt.external_id, "detail": receipt.detail},
                    occurred_at=utc_now(),
                )
            )
            return state

    async def mark_outbox_unknown(self, item_id: int, detail: str) -> bool:
        async with self.database.sessions() as session, session.begin():
            result = await session.execute(
                update(OutboxRow)
                .where(OutboxRow.id == item_id, OutboxRow.state == OutboxState.DISPATCHING.value)
                .values(
                    state=OutboxState.UNKNOWN.value,
                    failure_detail=detail[:2_000],
                    updated_at=utc_now(),
                )
            )
            return _rowcount(result) == 1

    async def release_reserved(self, item_id: int, detail: str) -> bool:
        """Release a reservation only before delivery has been attempted."""
        async with self.database.sessions() as session, session.begin():
            result = await session.execute(
                update(OutboxRow)
                .where(OutboxRow.id == item_id, OutboxRow.state == OutboxState.RESERVED.value)
                .values(
                    state=OutboxState.PENDING.value,
                    reserved_at=None,
                    failure_detail=detail[:2_000],
                    updated_at=utc_now(),
                )
            )
            return _rowcount(result) == 1

    async def mark_reserved_dead(self, item_id: int, detail: str) -> bool:
        """Permanently stop an outbox item before any delivery attempt."""
        async with self.database.sessions() as session, session.begin():
            result = await session.execute(
                update(OutboxRow)
                .where(OutboxRow.id == item_id, OutboxRow.state == OutboxState.RESERVED.value)
                .values(
                    state=OutboxState.DEAD.value,
                    failure_detail=detail[:2_000],
                    updated_at=utc_now(),
                )
            )
            return _rowcount(result) == 1

    async def recover_interrupted(
        self, stale_after: timedelta = timedelta(minutes=5)
    ) -> dict[str, int]:
        """Recover safe states and quarantine possibly-sent messages."""
        cutoff = utc_now() - stale_after
        async with self.database.sessions() as session, session.begin():
            inbox_ambiguous = await session.execute(
                update(InboxRow)
                .where(
                    InboxRow.state == InboxState.PROCESSING.value,
                    InboxRow.claimed_at < cutoff,
                    InboxRow.model_started_at.is_not(None),
                )
                .values(
                    state=InboxState.DEAD.value,
                    claimed_at=None,
                    last_error=(
                        "process stopped after model provider I/O may have begun; "
                        "automatic retry is forbidden"
                    ),
                    updated_at=utc_now(),
                )
            )
            inbox = await session.execute(
                update(InboxRow)
                .where(
                    InboxRow.state == InboxState.PROCESSING.value,
                    InboxRow.claimed_at < cutoff,
                    InboxRow.model_started_at.is_(None),
                )
                .values(state=InboxState.PENDING.value, claimed_at=None, updated_at=utc_now())
            )
            reserved = await session.execute(
                update(OutboxRow)
                .where(
                    OutboxRow.state == OutboxState.RESERVED.value,
                    OutboxRow.reserved_at < cutoff,
                )
                .values(state=OutboxState.PENDING.value, reserved_at=None, updated_at=utc_now())
            )
            dispatching = await session.execute(
                update(OutboxRow)
                .where(OutboxRow.state == OutboxState.DISPATCHING.value)
                .values(
                    state=OutboxState.UNKNOWN.value,
                    failure_detail=(
                        "process stopped after dispatch began; manual reconciliation required"
                    ),
                    updated_at=utc_now(),
                )
            )
            interrupted_tool_requests = await session.execute(
                update(ToolExecutionRow)
                .where(ToolExecutionRow.state == "requested")
                .values(
                    state="failed",
                    outcome_code="interrupted_before_invoke",
                    resolved_at=utc_now(),
                    updated_at=utc_now(),
                )
            )
            interrupted_tool_calls = await session.execute(
                update(ToolExecutionRow)
                .where(ToolExecutionRow.state == "executing")
                .values(
                    state="unknown",
                    outcome_code="interrupted_during_invoke",
                    resolved_at=utc_now(),
                    updated_at=utc_now(),
                )
            )
            return {
                "inbox_requeued": _rowcount(inbox),
                "inbox_dead_ambiguous": _rowcount(inbox_ambiguous),
                "outbox_released": _rowcount(reserved),
                "outbox_unknown": _rowcount(dispatching),
                "tool_requests_failed": _rowcount(interrupted_tool_requests),
                "tool_calls_unknown": _rowcount(interrupted_tool_calls),
            }

    async def set_allowlisted(
        self, channel: str, chat_id: str, enabled: bool = True, label: str | None = None
    ) -> None:
        statement = (
            sqlite_insert(AllowlistRow)
            .values(
                channel=channel,
                chat_id=chat_id,
                enabled=int(enabled),
                label=label,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            .on_conflict_do_update(
                index_elements=["channel", "chat_id"],
                set_={"enabled": int(enabled), "label": label, "updated_at": utc_now()},
            )
        )
        async with self.database.sessions() as session, session.begin():
            await session.execute(statement)

    async def is_allowlisted(self, channel: str, chat_id: str) -> bool:
        async with self.database.sessions() as session:
            value = await session.scalar(
                select(AllowlistRow.enabled).where(
                    AllowlistRow.channel == channel, AllowlistRow.chat_id == chat_id
                )
            )
        return value == 1

    async def set_paused(self, *, channel: str | None = None, paused: bool = True) -> None:
        key = "pause:global" if channel is None else f"pause:channel:{channel}"
        statement = (
            sqlite_insert(RuntimeStateRow)
            .values(key=key, value_json={"paused": paused}, updated_at=utc_now())
            .on_conflict_do_update(
                index_elements=["key"],
                set_={"value_json": {"paused": paused}, "updated_at": utc_now()},
            )
        )
        async with self.database.sessions() as session, session.begin():
            await session.execute(statement)

    async def is_paused(self, channel: str | None = None) -> bool:
        keys = ["pause:global"]
        if channel is not None:
            keys.append(f"pause:channel:{channel}")
        async with self.database.sessions() as session:
            values = (
                await session.scalars(
                    select(RuntimeStateRow.value_json).where(RuntimeStateRow.key.in_(keys))
                )
            ).all()
        return any(bool(value.get("paused")) for value in values)

    async def count_outbound_since(
        self,
        since: datetime,
        *,
        channel: str,
        chat_id: str | None = None,
        proactive: bool | None = None,
        exclude_message_id: UUID | None = None,
    ) -> int:
        states = [
            OutboxState.PENDING.value,
            OutboxState.RESERVED.value,
            OutboxState.DISPATCHING.value,
            OutboxState.ACKNOWLEDGED.value,
            OutboxState.UNKNOWN.value,
        ]
        query = select(func.count(OutboxRow.id)).where(
            OutboxRow.channel == channel,
            OutboxRow.created_at >= since,
            OutboxRow.state.in_(states),
        )
        if chat_id is not None:
            query = query.where(OutboxRow.chat_id == chat_id)
        if proactive is not None:
            query = query.where(OutboxRow.proactive == int(proactive))
        if exclude_message_id is not None:
            query = query.where(OutboxRow.message_id != str(exclude_message_id))
        async with self.database.sessions() as session:
            return int(await session.scalar(query) or 0)

    async def recent_messages(
        self,
        channel: str,
        chat_id: str,
        *,
        limit: int = 20,
        through_external_id: str | None = None,
    ) -> list[ConversationMessage]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        async with self.database.sessions() as session:
            boundary: int | None = None
            if through_external_id is not None:
                boundary = await session.scalar(
                    select(MessageRow.id).where(
                        MessageRow.channel == channel,
                        MessageRow.chat_id == chat_id,
                        MessageRow.external_id == through_external_id,
                    )
                )
                if boundary is None:
                    raise KeyError(
                        f"message boundary {channel}/{chat_id}/{through_external_id} not found"
                    )
            query = select(MessageRow).where(
                MessageRow.channel == channel, MessageRow.chat_id == chat_id
            )
            if boundary is not None:
                query = query.where(MessageRow.id <= boundary)
            rows = (
                await session.scalars(
                    query.order_by(MessageRow.occurred_at.desc(), MessageRow.id.desc()).limit(limit)
                )
            ).all()
        return [self._conversation_message(row) for row in reversed(rows)]

    async def search_messages(
        self, channel: str, chat_id: str, query: str, *, limit: int = 10
    ) -> list[ConversationMessage]:
        if not query.strip():
            return []
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        # Quoting makes user input an FTS phrase instead of executable FTS
        # syntax, preventing malformed expressions and scope broadening.
        safe_query = '"' + query.strip().replace('"', '""') + '"'
        statement = text(
            "SELECT m.id FROM messages_fts "
            "JOIN messages m ON m.id = messages_fts.rowid "
            "WHERE messages_fts MATCH :query AND m.channel = :channel AND m.chat_id = :chat_id "
            "ORDER BY bm25(messages_fts), m.occurred_at DESC LIMIT :limit"
        )
        async with self.database.sessions() as session:
            ids = list(
                (
                    await session.execute(
                        statement,
                        {
                            "query": safe_query,
                            "channel": channel,
                            "chat_id": chat_id,
                            "limit": limit,
                        },
                    )
                ).scalars()
            )
            if not ids:
                return []
            rows = (await session.scalars(select(MessageRow).where(MessageRow.id.in_(ids)))).all()
        by_id = {row.id: row for row in rows}
        return [self._conversation_message(by_id[item_id]) for item_id in ids if item_id in by_id]

    async def append_audit(self, record: AuditRecord) -> None:
        async with self.database.sessions() as session, session.begin():
            session.add(
                AuditRow(
                    action=record.action,
                    outcome=record.outcome,
                    channel=record.channel,
                    chat_id=record.chat_id,
                    event_id=record.event_id,
                    message_id=str(record.message_id) if record.message_id else None,
                    rule_id=record.rule_id,
                    detail_json=record.detail,
                    occurred_at=record.occurred_at,
                )
            )

    async def audit_records(self, *, limit: int = 100) -> list[dict[str, Any]]:
        async with self.database.sessions() as session:
            rows = (
                await session.scalars(
                    select(AuditRow)
                    .order_by(AuditRow.occurred_at.desc(), AuditRow.id.desc())
                    .limit(limit)
                )
            ).all()
            return [
            {
                "id": row.id,
                "action": row.action,
                "outcome": row.outcome,
                "channel": row.channel,
                "chat_id": row.chat_id,
                "event_id": row.event_id,
                "message_id": row.message_id,
                "rule_id": row.rule_id,
                "detail": row.detail_json,
                "occurred_at": _utc(row.occurred_at),
            }
                for row in rows
            ]

    async def runtime_counts(self) -> dict[str, int]:
        async with self.database.sessions() as session:
            queue_depth = await session.scalar(
                select(func.count(InboxRow.id)).where(
                    InboxRow.state.in_([InboxState.PENDING.value, InboxState.PROCESSING.value])
                )
            )
            unknown_outbox = await session.scalar(
                select(func.count(OutboxRow.id)).where(
                    OutboxRow.state == OutboxState.UNKNOWN.value
                )
            )
        return {
            "queue_depth": int(queue_depth or 0),
            "unknown_outbox": int(unknown_outbox or 0),
        }

    async def outbox_state(self, message_id: UUID) -> OutboxState | None:
        async with self.database.sessions() as session:
            value = await session.scalar(
                select(OutboxRow.state).where(OutboxRow.message_id == str(message_id))
            )
        return OutboxState(value) if value else None

    @staticmethod
    def _inbox_item(row: InboxRow) -> InboxItem:
        return InboxItem(
            id=row.id,
            event=InboundEvent(
                channel=row.channel,
                event_id=row.event_id,
                chat_id=row.chat_id,
                sender_id=row.sender_id,
                text=row.text,
                kind=EventKind(row.kind),
                occurred_at=_required_utc(row.occurred_at),
                metadata=row.metadata_json,
            ),
            state=InboxState(row.state),
            attempts=row.attempts,
            claimed_at=_utc(row.claimed_at),
        )

    @staticmethod
    def _outbox_item(row: OutboxRow) -> OutboxItem:
        return OutboxItem(
            id=row.id,
            message=OutboundMessage(
                message_id=UUID(row.message_id),
                channel=row.channel,
                chat_id=row.chat_id,
                text=row.text,
                reply_to_event_id=row.reply_to_event_id,
                created_at=_required_utc(row.created_at),
                metadata=row.metadata_json,
            ),
            state=OutboxState(row.state),
            attempts=row.attempts,
            reserved_at=_utc(row.reserved_at),
            dispatch_started_at=_utc(row.dispatch_started_at),
        )

    @staticmethod
    def _conversation_message(row: MessageRow) -> ConversationMessage:
        return ConversationMessage(
            id=row.id,
            channel=row.channel,
            chat_id=row.chat_id,
            sender_id=row.sender_id,
            role=MessageRole(row.role),
            content=row.content,
            external_id=row.external_id,
            occurred_at=_required_utc(row.occurred_at),
            metadata=row.metadata_json,
        )
