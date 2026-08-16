"""Dependency-inversion boundaries used by the core orchestrator."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol, runtime_checkable

from .models import (
    ConnectorHealth,
    DeliveryReceipt,
    InboundEvent,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    OutboundMessage,
    PolicyEvaluation,
    ProposedAction,
    ToolContext,
    ToolManifest,
    ToolResult,
)


@runtime_checkable
class Connector(Protocol):
    def events(self) -> AsyncIterator[InboundEvent]: ...

    async def deliver(self, message: OutboundMessage) -> DeliveryReceipt: ...

    async def health(self) -> ConnectorHealth: ...


@runtime_checkable
class ModelBackend(Protocol):
    async def generate(self, request: ModelRequest) -> ModelResponse: ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    def count_tokens(self, messages: Sequence[object]) -> int: ...

    def capabilities(self) -> ModelCapabilities: ...


@runtime_checkable
class Tool(Protocol):
    def manifest(self) -> ToolManifest: ...

    async def invoke(self, context: ToolContext, arguments: dict[str, object]) -> ToolResult: ...


@runtime_checkable
class Policy(Protocol):
    async def evaluate(self, action: ProposedAction) -> PolicyEvaluation: ...
