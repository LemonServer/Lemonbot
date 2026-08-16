"""Profile-scoped orchestration for durable APPROVE_ONCE actions."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from lemonbot.domain import ApprovalState

from .models import (
    ApprovalClaim,
    ApprovalListItem,
    ApprovalRequest,
    canonicalize_arguments,
)
from .repository import ApprovalRepository

_OUTCOME_CODE = re.compile(r"^[a-zA-Z0-9_.-]{1,128}$")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("approval timestamps must include a timezone")
    return value.astimezone(UTC)


def _uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(value)


class ApprovalService:
    """Issue, decide and settle approvals without exposing pending parameters."""

    def __init__(
        self,
        repository: ApprovalRepository,
        *,
        profile: str,
        default_ttl: timedelta = timedelta(minutes=15),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not profile or profile != profile.strip() or len(profile) > 32 or "\0" in profile:
            raise ValueError("profile must be an exact stable identifier")
        if default_ttl <= timedelta(0):
            raise ValueError("default approval TTL must be positive")
        self.repository = repository
        self.profile = profile
        self.default_ttl = default_ttl
        self.clock = clock or _utc_now

    async def request(
        self,
        *,
        channel: str,
        chat_id: str,
        event_id: str,
        tool_name: str,
        action_kind: str,
        arguments: Mapping[str, Any],
        expires_at: datetime | None = None,
    ) -> ApprovalListItem:
        now = self._now()
        expiry = _aware_utc(expires_at) if expires_at is not None else now + self.default_ttl
        normalized, digest, summary = canonicalize_arguments(dict(arguments))
        request = ApprovalRequest(
            approval_id=uuid4(),
            profile=self.profile,
            channel=channel,
            chat_id=chat_id,
            event_id=event_id,
            tool_name=tool_name,
            action_kind=action_kind,
            arguments_summary=summary,
            arguments_sha256=digest,
            arguments=normalized,
            created_at=now,
            expires_at=expiry,
        )
        return await self.repository.create(request)

    async def pending(
        self,
        *,
        channel: str | None = None,
        chat_id: str | None = None,
        limit: int = 100,
    ) -> list[ApprovalListItem]:
        return await self.repository.list_pending(
            profile=self.profile,
            now=self._now(),
            channel=channel,
            chat_id=chat_id,
            limit=limit,
        )

    async def status(self, approval_id: UUID | str) -> ApprovalListItem | None:
        return await self.repository.get_safe(_uuid(approval_id), profile=self.profile)

    async def approve_once(self, approval_id: UUID | str) -> ApprovalClaim | None:
        """Atomically claim a still-pending approval for immediate execution."""

        return await self.repository.claim(
            _uuid(approval_id), profile=self.profile, now=self._now()
        )

    async def deny(
        self,
        approval_id: UUID | str,
        *,
        outcome_code: str = "administrator_denied",
    ) -> bool:
        return await self.repository.deny(
            _uuid(approval_id),
            profile=self.profile,
            now=self._now(),
            outcome_code=self._outcome_code(outcome_code),
        )

    async def resolve(
        self,
        claim: ApprovalClaim,
        *,
        outcome: ApprovalState,
        outcome_code: str,
    ) -> bool:
        """Record a known result or an ambiguous side-effect outcome exactly once."""

        if claim.profile != self.profile:
            raise ValueError("approval claim belongs to another profile")
        return await self.repository.resolve(
            claim.approval_id,
            profile=self.profile,
            claim_token=claim.claim_token,
            outcome=outcome,
            outcome_code=self._outcome_code(outcome_code),
            now=self._now(),
        )

    async def recover_interrupted(self) -> int:
        return await self.repository.recover_executing(
            profile=self.profile, now=self._now()
        )

    def _now(self) -> datetime:
        return _aware_utc(self.clock())

    @staticmethod
    def _outcome_code(value: str) -> str:
        if not _OUTCOME_CODE.fullmatch(value):
            raise ValueError("outcome_code must be a non-sensitive stable code")
        return value
