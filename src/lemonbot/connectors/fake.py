"""Deterministic in-memory connector for tests and local demonstrations."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from uuid import UUID

from lemonbot.domain.models import (
    ConnectorHealth,
    DeliveryReceipt,
    DeliveryStatus,
    InboundEvent,
    OutboundMessage,
    utc_now,
)

from ._dedup import BoundedDeduplicator
from .base import Connector

_STOP = object()


class FakeConnector(Connector):
    """An idempotent connector that never performs external I/O."""

    def __init__(
        self,
        channel: str = "fake",
        initial_events: Iterable[InboundEvent] = (),
        *,
        close_when_drained: bool = False,
        delivery_status: DeliveryStatus = DeliveryStatus.ACKNOWLEDGED,
        dedup_capacity: int = 10_000,
    ) -> None:
        if not channel:
            raise ValueError("channel must not be empty")
        self.channel = channel
        self._queue: asyncio.Queue[InboundEvent | object] = asyncio.Queue()
        self._seen = BoundedDeduplicator(dedup_capacity)
        self._closed = False
        self._delivery_status = delivery_status
        self._delivered: list[OutboundMessage] = []
        self._receipts: dict[UUID, DeliveryReceipt] = {}
        for event in initial_events:
            self.push_nowait(event)
        if close_when_drained:
            self.close_nowait()

    @property
    def delivered_messages(self) -> tuple[OutboundMessage, ...]:
        return tuple(self._delivered)

    def push_nowait(self, event: InboundEvent) -> bool:
        """Queue an event; return false when it is a duplicate."""

        if self._closed:
            raise RuntimeError("fake connector is closed")
        if event.channel != self.channel:
            raise ValueError(f"event channel {event.channel!r} does not match {self.channel!r}")
        if not self._seen.add(event.event_id):
            return False
        self._queue.put_nowait(event)
        return True

    async def push(self, event: InboundEvent) -> bool:
        return self.push_nowait(event)

    def close_nowait(self) -> None:
        if not self._closed:
            self._closed = True
            self._queue.put_nowait(_STOP)

    async def close(self) -> None:
        self.close_nowait()

    async def events(self) -> AsyncIterator[InboundEvent]:
        while True:
            item = await self._queue.get()
            if item is _STOP:
                return
            assert isinstance(item, InboundEvent)
            yield item

    async def deliver(self, message: OutboundMessage) -> DeliveryReceipt:
        previous = self._receipts.get(message.message_id)
        if previous is not None:
            return previous
        if message.channel != self.channel:
            receipt = DeliveryReceipt(
                message_id=message.message_id,
                status=DeliveryStatus.FAILED,
                detail=(
                    f"message channel {message.channel!r} does not match "
                    f"connector channel {self.channel!r}"
                ),
            )
        elif self._closed:
            receipt = DeliveryReceipt(
                message_id=message.message_id,
                status=DeliveryStatus.FAILED,
                detail="fake connector is closed",
            )
        else:
            self._delivered.append(message)
            acknowledged_at = (
                utc_now() if self._delivery_status is DeliveryStatus.ACKNOWLEDGED else None
            )
            receipt = DeliveryReceipt(
                message_id=message.message_id,
                status=self._delivery_status,
                external_id=f"fake:{message.message_id}",
                acknowledged_at=acknowledged_at,
                detail=(
                    None
                    if self._delivery_status is DeliveryStatus.ACKNOWLEDGED
                    else "configured fake delivery result"
                ),
            )
        self._receipts[message.message_id] = receipt
        return receipt

    async def health(self) -> ConnectorHealth:
        return ConnectorHealth(
            healthy=not self._closed,
            detail="closed" if self._closed else "ready (in-memory)",
            account_id="fake",
        )
