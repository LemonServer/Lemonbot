"""Stage boundaries; implementations must not bypass the existing event pipeline."""

from __future__ import annotations

from typing import Protocol

from lemonbot.domain.models import OutboxState

from .models import (
    ContextBundle,
    ContextDecision,
    ConversationScope,
    EmbeddingRequest,
    EmbeddingResult,
    MemoryEntry,
    Message,
    Observation,
    RetrievalResult,
    SourceRef,
)


class MessageArchive(Protocol):
    async def put_message(self, message: Message) -> bool:
        """Insert by scoped message ID; False for identical existing data.

        Conflicting identity/payload must fail, never overwrite or body-dedup.
        Outbound delivery transitions are owned by the existing outbox, not a
        model or an archive caller. UNKNOWN must never transition back to pending.
        """
        ...

    async def get_message(self, source: SourceRef) -> Message | None:
        """Resolve exact scope and message; verify part existence when specified."""
        ...

    async def apply_delivery_state(self, source: SourceRef, state: OutboxState) -> Message:
        """Mirror a verified durable outbox transition, never a model assertion.

        Preserve identity/payload. UNKNOWN is terminal; do not create a new send.
        Implementations must verify the originating outbox/receipt association.
        """
        ...

    async def put_observation(self, observation: Observation) -> bool:
        """Idempotently persist against an existing exact message/part source."""
        ...

    async def get_observations(self, source: SourceRef) -> tuple[Observation, ...]:
        """Read exact scoped source only; return failure/expiry metadata intact."""
        ...

    async def list_messages(
        self, *, scope: ConversationScope, before_message_id: str | None, limit: int
    ) -> tuple[Message, ...]:
        """Scoped deterministic pagination; never creates reception/send work."""
        ...


class MemoryEntryStore(Protocol):
    """Stage 6 derived-memory persistence, separate from the stage 2 raw archive."""

    async def put_memory(self, entry: MemoryEntry) -> bool:
        """Verify eligible sources and supersession scope in one transaction."""
        ...

    async def get_memory(
        self, *, scope: ConversationScope, memory_id: str
    ) -> MemoryEntry | None:
        """Resolve a memory ID within scope, retaining inactive correction versions."""
        ...


class ContextRetriever(Protocol):
    async def retrieve(self, *, current: Message, limit: int = 20) -> RetrievalResult:
        """Pre-filter scope; exact quotes first, bounded hybrid candidates next."""
        ...


class RelevanceClassifier(Protocol):
    async def classify(self, *, current: Message, retrieval: RetrievalResult) -> ContextDecision:
        """One budgeted tools-disabled call, preserving the original message.

        Classifier image intake uses broker-authorized current-message objects.
        Invalid output is surfaced without an automatic paid retry.
        """
        ...


class ContextAssembler(Protocol):
    async def assemble(
        self, *, current: Message, retrieval: RetrievalResult, decision: ContextDecision
    ) -> ContextBundle:
        """Resolve only offered candidate IDs, eligible sources and valid images.

        Check exact current/retrieval scope, ID membership, authors, part IDs,
        expiry, object integrity and token bounds. A hash is not authorization.
        Historical URLs never grant fresh network/tool permission.
        """
        ...


class EmbeddingWorker(Protocol):
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        """Match request ID and vector count; bounded timeout, no silent CPU fallback."""
        ...

    async def close(self) -> None: ...
