"""Deterministic in-process adapters for tests and local smoke runs."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Sequence

from lemonbot.domain import (
    ConnectorHealth,
    DeliveryReceipt,
    DeliveryStatus,
    InboundEvent,
    MessageRole,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    OutboundMessage,
    Unsupported,
    utc_now,
)


class FakeConnector:
    def __init__(self, channel: str = "fake") -> None:
        self.channel = channel
        self._events: asyncio.Queue[InboundEvent | None] = asyncio.Queue()
        self.delivered: list[OutboundMessage] = []
        self.raise_after_accept = False

    async def push(self, event: InboundEvent) -> None:
        await self._events.put(event)

    async def stop(self) -> None:
        await self._events.put(None)

    async def events(self) -> AsyncIterator[InboundEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    async def deliver(self, message: OutboundMessage) -> DeliveryReceipt:
        if message.channel != self.channel:
            raise ValueError("connector channel mismatch")
        self.delivered.append(message)
        if self.raise_after_accept:
            raise ConnectionError("simulated ambiguous connection loss")
        return DeliveryReceipt(
            message_id=message.message_id,
            status=DeliveryStatus.ACKNOWLEDGED,
            external_id=f"fake:{len(self.delivered)}",
            acknowledged_at=utc_now(),
        )

    async def health(self) -> ConnectorHealth:
        return ConnectorHealth(healthy=True, account_id="fake-account")


class FakeModelBackend:
    def __init__(self, responses: Sequence[ModelResponse | str] = ()) -> None:
        self.responses = deque(
            response
            if isinstance(response, ModelResponse)
            else ModelResponse(content=response, model="fake")
            for response in responses
        )
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.responses:
            return self.responses.popleft()
        user_text = next(
            (
                message.content
                for message in reversed(request.messages)
                if message.role is MessageRole.USER and message.content
            ),
            "",
        )
        return ModelResponse(content=f"AI: {user_text}", model="fake")

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise Unsupported("fake backend does not implement embeddings")

    def count_tokens(self, messages: Sequence[object]) -> int:
        return sum(max(1, len(str(message)) // 4) for message in messages)

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(tools=True, json_output=True, context_tokens=32_768)
