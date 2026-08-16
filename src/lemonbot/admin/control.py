from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict


class ApprovalView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str
    created_at: datetime
    expires_at: datetime
    action_type: str
    summary: str
    channel: str
    chat_id: str


class StatusView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: str
    connector: str
    global_paused: bool
    channel_pauses: dict[str, bool]
    emergency_stopped: bool
    queue_depth: int = 0
    unknown_outbox: int = 0
    pending_approvals: int = 0
    started_at: datetime


class ControlBackend(Protocol):
    async def status(self) -> StatusView: ...

    async def set_pause(self, channel: str | None, paused: bool) -> StatusView: ...

    async def emergency_stop(self) -> StatusView: ...

    async def approvals(self) -> list[ApprovalView]: ...

    async def decide_approval(
        self, approval_id: str, decision: Literal["approve_once", "deny"]
    ) -> bool: ...


class InMemoryControl:
    """Fail-closed control surface used until backed by the durable repository."""

    def __init__(self, profile: str, connector: str) -> None:
        self._profile = profile
        self._connector = connector
        self._global_paused = False
        self._channel_pauses: dict[str, bool] = {"wecom": False, "wechat_uia": False}
        self._emergency_stopped = False
        self._started_at = datetime.now(UTC)
        self._approvals: dict[str, ApprovalView] = {}
        self._lock = asyncio.Lock()

    async def status(self) -> StatusView:
        async with self._lock:
            return StatusView(
                profile=self._profile,
                connector=self._connector,
                global_paused=self._global_paused,
                channel_pauses=dict(self._channel_pauses),
                emergency_stopped=self._emergency_stopped,
                pending_approvals=len(self._approvals),
                started_at=self._started_at,
            )

    async def set_pause(self, channel: str | None, paused: bool) -> StatusView:
        async with self._lock:
            if self._emergency_stopped and not paused:
                raise RuntimeError("restart is required after emergency stop")
            if channel is None:
                self._global_paused = paused
            elif channel in self._channel_pauses:
                self._channel_pauses[channel] = paused
            else:
                raise ValueError("unknown channel")
        return await self.status()

    async def emergency_stop(self) -> StatusView:
        async with self._lock:
            self._emergency_stopped = True
            self._global_paused = True
            for channel in self._channel_pauses:
                self._channel_pauses[channel] = True
        return await self.status()

    async def approvals(self) -> list[ApprovalView]:
        async with self._lock:
            now = datetime.now(UTC)
            self._approvals = {
                key: value for key, value in self._approvals.items() if value.expires_at > now
            }
            return list(self._approvals.values())

    async def decide_approval(
        self, approval_id: str, decision: Literal["approve_once", "deny"]
    ) -> bool:
        del decision
        async with self._lock:
            return self._approvals.pop(approval_id, None) is not None

    async def add_approval(self, approval: ApprovalView) -> str:
        async with self._lock:
            approval_id = approval.approval_id or str(uuid4())
            self._approvals[approval_id] = approval.model_copy(update={"approval_id": approval_id})
            return approval_id
