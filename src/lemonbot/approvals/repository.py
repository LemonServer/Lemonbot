"""Transactional persistence for exact, one-use tool approvals."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from lemonbot.domain import ApprovalState
from lemonbot.storage.database import Database
from lemonbot.storage.models import ApprovalRow, AuditRow

from .models import ApprovalClaim, ApprovalListItem, ApprovalRequest


def _rowcount(result: object) -> int:
    return int(getattr(result, "rowcount", 0))


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _required_utc(value: datetime | None) -> datetime:
    normalized = _utc(value)
    if normalized is None:
        raise ValueError("persisted approval timestamp is null")
    return normalized


class ApprovalRepository:
    """Repository whose only full-argument read is an atomic execution claim."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def create(self, request: ApprovalRequest) -> ApprovalListItem:
        """Create an idempotent request for one exact event/action/argument binding."""

        async with self.database.sessions() as session, session.begin():
            result = await session.execute(
                sqlite_insert(ApprovalRow)
                .values(
                    approval_id=str(request.approval_id),
                    profile=request.profile,
                    channel=request.channel,
                    chat_id=request.chat_id,
                    event_id=request.event_id,
                    tool_name=request.tool_name,
                    action_kind=request.action_kind,
                    arguments_summary=request.arguments_summary,
                    arguments_sha256=request.arguments_sha256,
                    arguments_json=request.arguments,
                    state=ApprovalState.PENDING.value,
                    created_at=request.created_at,
                    expires_at=request.expires_at,
                    updated_at=request.created_at,
                )
                .on_conflict_do_nothing()
            )
            row = await session.scalar(
                select(ApprovalRow).where(
                    ApprovalRow.profile == request.profile,
                    ApprovalRow.channel == request.channel,
                    ApprovalRow.chat_id == request.chat_id,
                    ApprovalRow.event_id == request.event_id,
                    ApprovalRow.tool_name == request.tool_name,
                    ApprovalRow.action_kind == request.action_kind,
                    ApprovalRow.arguments_sha256 == request.arguments_sha256,
                )
            )
            if row is None:
                raise RuntimeError("approval insert conflicted with a different approval id")
            if _rowcount(result) == 1:
                session.add(self._audit(row, "approval.request", ApprovalState.PENDING.value))
            return self._list_item(row)

    async def list_pending(
        self,
        *,
        profile: str,
        now: datetime,
        channel: str | None = None,
        chat_id: str | None = None,
        limit: int = 100,
    ) -> list[ApprovalListItem]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        async with self.database.sessions() as session, session.begin():
            await self._expire_pending(session, profile=profile, now=now)
            query = select(ApprovalRow).where(
                ApprovalRow.profile == profile,
                ApprovalRow.state == ApprovalState.PENDING.value,
                ApprovalRow.expires_at > now,
            )
            if channel is not None:
                query = query.where(ApprovalRow.channel == channel)
            if chat_id is not None:
                query = query.where(ApprovalRow.chat_id == chat_id)
            query = query.order_by(ApprovalRow.created_at, ApprovalRow.approval_id).limit(limit)
            rows = list((await session.scalars(query)).all())
            return [self._list_item(row) for row in rows]

    async def get_safe(self, approval_id: UUID, *, profile: str) -> ApprovalListItem | None:
        """Return lifecycle metadata without ever exposing full arguments."""

        async with self.database.sessions() as session:
            row = await session.scalar(
                select(ApprovalRow).where(
                    ApprovalRow.approval_id == str(approval_id),
                    ApprovalRow.profile == profile,
                )
            )
            return None if row is None else self._list_item(row)

    async def claim(
        self,
        approval_id: UUID,
        *,
        profile: str,
        now: datetime,
    ) -> ApprovalClaim | None:
        """Atomically consume a pending approval and reveal its bound parameters once."""

        claim_token = uuid4()
        async with self.database.sessions() as session, session.begin():
            await self._expire_pending(session, profile=profile, now=now)
            result = await session.execute(
                update(ApprovalRow)
                .where(
                    ApprovalRow.approval_id == str(approval_id),
                    ApprovalRow.profile == profile,
                    ApprovalRow.state == ApprovalState.PENDING.value,
                    ApprovalRow.expires_at > now,
                )
                .values(
                    state=ApprovalState.EXECUTING.value,
                    claim_token=str(claim_token),
                    claimed_at=now,
                    updated_at=now,
                )
            )
            if _rowcount(result) != 1:
                return None
            row = await session.get(ApprovalRow, str(approval_id))
            if row is None or row.profile != profile:
                raise RuntimeError("claimed approval disappeared")
            session.add(self._audit(row, "approval.claim", ApprovalState.EXECUTING.value))
            return self._claim(row)

    async def deny(
        self,
        approval_id: UUID,
        *,
        profile: str,
        now: datetime,
        outcome_code: str = "administrator_denied",
    ) -> bool:
        async with self.database.sessions() as session, session.begin():
            await self._expire_pending(session, profile=profile, now=now)
            result = await session.execute(
                update(ApprovalRow)
                .where(
                    ApprovalRow.approval_id == str(approval_id),
                    ApprovalRow.profile == profile,
                    ApprovalRow.state == ApprovalState.PENDING.value,
                )
                .values(
                    state=ApprovalState.DENIED.value,
                    outcome_code=outcome_code,
                    resolved_at=now,
                    updated_at=now,
                )
            )
            if _rowcount(result) != 1:
                return False
            row = await session.get(ApprovalRow, str(approval_id))
            assert row is not None
            session.add(self._audit(row, "approval.decision", ApprovalState.DENIED.value))
            return True

    async def resolve(
        self,
        approval_id: UUID,
        *,
        profile: str,
        claim_token: UUID,
        outcome: ApprovalState,
        outcome_code: str,
        now: datetime,
    ) -> bool:
        """Resolve a claimed action exactly once; terminal records are immutable."""

        if outcome not in {
            ApprovalState.APPROVED,
            ApprovalState.DENIED,
            ApprovalState.UNKNOWN,
        }:
            raise ValueError("approval outcome must be approved, denied or unknown")
        async with self.database.sessions() as session, session.begin():
            result = await session.execute(
                update(ApprovalRow)
                .where(
                    ApprovalRow.approval_id == str(approval_id),
                    ApprovalRow.profile == profile,
                    ApprovalRow.state == ApprovalState.EXECUTING.value,
                    ApprovalRow.claim_token == str(claim_token),
                )
                .values(
                    state=outcome.value,
                    outcome_code=outcome_code,
                    resolved_at=now,
                    updated_at=now,
                )
            )
            if _rowcount(result) != 1:
                return False
            row = await session.get(ApprovalRow, str(approval_id))
            assert row is not None
            session.add(self._audit(row, "approval.execute", outcome.value))
            return True

    async def recover_executing(self, *, profile: str, now: datetime) -> int:
        """Make every interrupted execution terminal-unknown; never requeue it."""

        async with self.database.sessions() as session, session.begin():
            rows = list(
                (
                    await session.scalars(
                        select(ApprovalRow).where(
                            ApprovalRow.profile == profile,
                            ApprovalRow.state == ApprovalState.EXECUTING.value,
                        )
                    )
                ).all()
            )
            for row in rows:
                row.state = ApprovalState.UNKNOWN.value
                row.outcome_code = "interrupted_execution"
                row.resolved_at = now
                row.updated_at = now
                session.add(self._audit(row, "approval.recover", ApprovalState.UNKNOWN.value))
            return len(rows)

    async def _expire_pending(
        self,
        session: AsyncSession,
        *,
        profile: str,
        now: datetime,
    ) -> int:
        rows = list(
            (
                await session.scalars(
                    select(ApprovalRow).where(
                        ApprovalRow.profile == profile,
                        ApprovalRow.state == ApprovalState.PENDING.value,
                        ApprovalRow.expires_at <= now,
                    )
                )
            ).all()
        )
        for row in rows:
            row.state = ApprovalState.DENIED.value
            row.outcome_code = "expired"
            row.resolved_at = now
            row.updated_at = now
            session.add(self._audit(row, "approval.expire", ApprovalState.DENIED.value))
        return len(rows)

    @staticmethod
    def _audit(row: ApprovalRow, action: str, outcome: str) -> AuditRow:
        return AuditRow(
            action=action,
            outcome=outcome,
            channel=row.channel,
            chat_id=row.chat_id,
            event_id=row.event_id,
            detail_json={
                "approval_id": row.approval_id,
                "profile": row.profile,
                "tool_name": row.tool_name,
                "action_kind": row.action_kind,
                "arguments_sha256": row.arguments_sha256,
            },
            occurred_at=_required_utc(row.updated_at),
        )

    @staticmethod
    def _list_item(row: ApprovalRow) -> ApprovalListItem:
        return ApprovalListItem(
            approval_id=UUID(row.approval_id),
            profile=row.profile,
            channel=row.channel,
            chat_id=row.chat_id,
            event_id=row.event_id,
            tool_name=row.tool_name,
            action_kind=row.action_kind,
            arguments_summary=row.arguments_summary,
            arguments_sha256=row.arguments_sha256,
            state=ApprovalState(row.state),
            created_at=_required_utc(row.created_at),
            expires_at=_required_utc(row.expires_at),
            claimed_at=_utc(row.claimed_at),
            resolved_at=_utc(row.resolved_at),
            outcome_code=row.outcome_code,
        )

    @classmethod
    def _claim(cls, row: ApprovalRow) -> ApprovalClaim:
        item = cls._list_item(row)
        if row.claim_token is None:
            raise ValueError("persisted executing approval has no claim token")
        return ApprovalClaim(
            **item.model_dump(),
            claim_token=UUID(row.claim_token),
            arguments=row.arguments_json,
        )
