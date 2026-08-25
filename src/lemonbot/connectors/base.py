"""Connector interface shared by concrete transports.

The core also exposes a structural ``Protocol`` for dependency injection.  This
nominal abstract base class gives connector authors an explicit implementation
contract while remaining compatible with that protocol.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from lemonbot.domain.models import (
    ConnectorHealth,
    DeliveryReceipt,
    InboundEvent,
    OutboundMessage,
)


class Connector(ABC):
    """A source of inbound events and a sink for broker-owned messages."""

    @abstractmethod
    def events(self) -> AsyncIterator[InboundEvent]:
        """Yield inbound events until the connector is closed."""

    @abstractmethod
    async def deliver(self, message: OutboundMessage) -> DeliveryReceipt:
        """Attempt one delivery and return a receipt without blind retries."""

    @abstractmethod
    async def health(self) -> ConnectorHealth:
        """Return a non-secret diagnostic snapshot."""


BaseConnector = Connector
