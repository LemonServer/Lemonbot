"""Durable runtime pause control around the shared canary transport."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from lemonbot.connectors.base import Connector
from lemonbot.connectors.wechat_agent import CHANNEL, AgentWechatConnector, LabPaused
from lemonbot.connectors.wechat_agent_state import private_read, private_write
from lemonbot.domain.models import (
    AuditRecord,
    ConnectorHealth,
    DeliveryReceipt,
    DeliveryStatus,
    InboundEvent,
    OutboundMessage,
)
from lemonbot.latency import record
from lemonbot.storage import CoreRepository


class RuntimeAgentWechatConnector(Connector):
    channel = CHANNEL

    def __init__(
        self,
        transport: AgentWechatConnector,
        repository: CoreRepository,
        state_dir: Path,
    ) -> None:
        self.transport = transport
        self.repository = repository
        self.pause_file = state_dir / "paused.json"
        self.operation_lock = asyncio.Lock()
        self.processing_idle = asyncio.Event()
        self.processing_idle.set()
        self.closed = False
        self.ready = False
        self.pause_reason: str | None = None

    @property
    def chat_ref(self) -> str:
        return self.transport.binding.chat_ref

    @property
    def chat_refs(self) -> tuple[str, ...]:
        return getattr(self.transport, "chat_refs", (self.chat_ref,))

    async def initialize(self) -> None:
        if self.pause_file.exists():
            # Read only to verify ownership/permissions, never replay upstream text.
            try:
                value = json.loads(private_read(self.pause_file))
                reason = value.get(
                    "reason", value.get("code", value.get("error", "ExplicitResumeRequired"))
                )
                if not isinstance(reason, str) or not re.fullmatch(
                    r"[A-Za-z0-9:_-]{1,160}", reason
                ):
                    reason = "ExplicitResumeRequired"
            except (ValueError, AttributeError):
                reason = "ExplicitResumeRequired"
            await self._pause(reason)
        elif await self.repository.is_paused(CHANNEL):
            await self._pause("ExplicitResumeRequired")
        else:
            try:
                await self.transport.baseline()
                self.ready = True
            except LabPaused as error:
                await self._pause(str(error))

    async def _pause(self, reason: str) -> None:
        self.ready = False
        self.pause_reason = reason
        self.transport.paused = reason
        private_write(self.pause_file, json.dumps({"reason": reason}).encode())
        await self.repository.set_paused(channel=CHANNEL, paused=True)
        await self.repository.append_audit(
            AuditRecord(
                action="connector.pause",
                outcome="paused",
                channel=CHANNEL,
                detail={"reason": reason},
            )
        )

    async def pause(self, reason: str = "ManualPause") -> None:
        async with self.operation_lock:
            await self._pause(reason)

    async def resume(self) -> None:
        async with self.operation_lock:
            if not self.processing_idle.is_set():
                raise RuntimeError("wechat_agent is busy; resume later")
            self.transport.paused = None
            prepare = getattr(self.transport, "prepare_resume", None)
            if prepare is not None:
                prepare()
            try:
                await self.transport.baseline()
            except LabPaused as error:
                await self._pause(str(error))
                raise RuntimeError("wechat_agent resume verification failed") from None
            # Remove the sticky marker only after guard and baseline both succeed.
            self.pause_file.unlink(missing_ok=True)
            await self.repository.set_paused(channel=CHANNEL, paused=False)
            self.pause_reason = None
            self.ready = True
            await self.repository.append_audit(
                AuditRecord(
                    action="connector.resume",
                    outcome="baseline_rebuilt",
                    channel=CHANNEL,
                )
            )

    @asynccontextmanager
    async def processing_operation(self) -> AsyncIterator[None]:
        # Mask notification servicing during generation without holding the UI
        # mutex across policy commits (the admin uses the policy lock too).
        started = time.monotonic()
        async with self.operation_lock:
            record("processing.lock_wait", started)
            self.processing_idle.clear()
        try:
            yield
        finally:
            self.processing_idle.set()

    async def events(self) -> AsyncIterator[InboundEvent]:
        while not self.closed:
            await self.processing_idle.wait()
            rows: list[InboundEvent] = []
            async with self.operation_lock:
                if (
                    self.ready
                    and self.processing_idle.is_set()
                    and not await self.repository.is_paused(CHANNEL)
                ):
                    try:
                        rows = await self.transport.poll()
                    except LabPaused as error:
                        await self._pause(str(error))
            for event in rows:
                yield event
            wait = getattr(self.transport, "wait_for_changes", None)
            if not self.processing_idle.is_set():
                await self.processing_idle.wait()
            elif self.ready and wait is not None:
                await wait()
            else:
                await asyncio.sleep(self.transport.poll_seconds)

    async def deliver(self, message: OutboundMessage) -> DeliveryReceipt:
        while True:
            await self.processing_idle.wait()
            async with self.operation_lock:
                if not self.processing_idle.is_set():
                    continue
                if not self.ready or await self.repository.is_paused(CHANNEL):
                    raise LabPaused("ExplicitResumeRequired")
                try:
                    receipt = await self.transport.deliver(message)
                except LabPaused as error:
                    await self._pause(str(error))
                    raise
                if receipt.status is DeliveryStatus.UNKNOWN:
                    await self._pause(self.transport.paused or "UnconfirmedSend")
                return receipt

    async def health(self) -> ConnectorHealth:
        if not self.ready:
            return ConnectorHealth(healthy=False, detail=self.pause_reason)
        async with self.operation_lock:
            if not self.processing_idle.is_set():
                return ConnectorHealth(healthy=True, detail="ProcessingBusy")
            result = await self.transport.health()
            if not result.healthy:
                await self._pause(self.transport.paused or "GuardFailed")
            return result

    async def close(self) -> None:
        self.closed = True
        self.processing_idle.set()
        await self.transport.close()
