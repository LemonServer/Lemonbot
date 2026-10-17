"""Source-bearing archive and context contracts, independent of storage and vendors."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from lemonbot.domain.models import DomainModel, OutboxState
from lemonbot.memory.models import MemoryKind

Identifier = Annotated[str, Field(min_length=1, max_length=512, pattern=r"^\S+$")]


class ArchiveModel(DomainModel):
    @field_validator("*", check_fields=False)
    @classmethod
    def aware_times(cls, value: object) -> object:
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("archive timestamps must include a timezone")
            return value.astimezone(UTC)
        return value


class ConversationScope(ArchiveModel):
    """Broker-bound exact identifiers, never display names or model-selected targets.

    Group scope deliberately excludes sender; Message retains author separately.
    The entire tuple is mandatory for every storage/retrieval operation.
    """

    channel: Identifier
    account_id: Identifier
    conversation_id: Identifier
    kind: Literal["private", "group"]


class SourceRef(ArchiveModel):
    scope: ConversationScope
    message_id: Identifier
    part_id: Identifier | None = None


class AttachmentRef(ArchiveModel):
    """Content address only; broker resolves and verifies private bytes at use time.

    Possession of a hash is not authorization. SourceRef determines ownership;
    neither arbitrary paths nor inline bytes are accepted by this contract.
    """

    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=1)
    media_type: str = Field(min_length=1, max_length=128)
    expires_at: datetime


class MessagePart(ArchiveModel):
    part_id: Identifier
    kind: Literal["text", "image", "link", "card", "audio", "video", "file", "unknown"]
    text: str | None = Field(default=None, max_length=100_000)
    url: str | None = Field(default=None, min_length=1, max_length=4096)
    title: str | None = Field(default=None, max_length=2000)
    attachment: AttachmentRef | None = None
    unavailable_reason: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def content_matches_kind(self) -> MessagePart:
        if self.kind == "text" and self.text is None:
            raise ValueError("a text part requires original text")
        if self.kind in {"image", "audio", "video", "file"}:
            if self.attachment is None and self.unavailable_reason is None:
                raise ValueError("media requires an attachment or an unavailable reason")
        elif self.attachment is not None:
            raise ValueError("only media parts carry attachments")
        if self.kind in {"link", "card"} and self.url is None and self.unavailable_reason is None:
            raise ValueError("links/cards require a URL or an unavailable reason")
        if self.kind == "unknown" and self.unavailable_reason is None:
            raise ValueError("unknown parts require an explicit reason")
        return self


class Message(ArchiveModel):
    scope: ConversationScope
    message_id: Identifier
    sender_id: Identifier
    event_id: Identifier | None = None
    # Broker-only native locator, scoped by account and conversation. Never an
    # authority to fetch media, nor a replacement for the existing event ID.
    transport_message_id: Identifier | None = None
    direction: Literal["inbound", "outbound"]
    occurred_at: datetime
    parts: tuple[MessagePart, ...] = Field(min_length=1)
    references: tuple[SourceRef, ...] = ()
    delivery_state: OutboxState | None = None

    @model_validator(mode="after")
    def source_and_delivery_boundaries(self) -> Message:
        ids = [part.part_id for part in self.parts]
        if len(ids) != len(set(ids)):
            raise ValueError("part IDs must be unique within a message")
        if any(ref.scope != self.scope for ref in self.references):
            raise ValueError("references cannot cross conversation scope")
        if (self.direction == "outbound") != (self.delivery_state is not None):
            raise ValueError("only outbound messages require an explicit delivery state")
        return self

    @property
    def eligible_as_evidence(self) -> bool:
        """Archiving an attempted send does not make it delivered history."""
        return self.direction == "inbound" or self.delivery_state is OutboxState.ACKNOWLEDGED


class Observation(ArchiveModel):
    observation_id: Identifier
    source: SourceRef
    kind: Literal["image_description", "ocr", "page_body", "page_title", "unavailable"]
    status: Literal["available", "unavailable"]
    text: str | None = Field(default=None, max_length=100_000)
    error_code: str | None = Field(default=None, min_length=1, max_length=256)
    extractor_version: str = Field(min_length=1, max_length=256)
    created_at: datetime
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def preserve_failure(self) -> Observation:
        if self.status == "available" and (not self.text or self.kind == "unavailable"):
            raise ValueError("available observations require extracted content")
        if self.status == "unavailable" and (not self.error_code or self.text is not None):
            raise ValueError("unavailable observations carry a reason, not extracted content")
        if self.status == "available" and self.error_code is not None:
            raise ValueError("available observations cannot carry a failure code")
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("observation expiry must follow creation")
        return self


class MemoryEntry(ArchiveModel):
    memory_id: Identifier
    scope: ConversationScope
    kind: MemoryKind
    text: str = Field(min_length=1, max_length=100_000)
    sources: tuple[SourceRef, ...] = Field(min_length=1)
    author_ids: tuple[Identifier, ...] = Field(min_length=1)
    derivation_version: str = Field(min_length=1, max_length=256)
    created_at: datetime
    supersedes: tuple[Identifier, ...] = ()
    active: bool = True

    @model_validator(mode="after")
    def scoped_provenance(self) -> MemoryEntry:
        if any(source.scope != self.scope for source in self.sources):
            raise ValueError("memory sources cannot cross conversation scope")
        if self.memory_id in self.supersedes:
            raise ValueError("memory cannot supersede itself")
        if len(self.supersedes) != len(set(self.supersedes)):
            raise ValueError("superseded memory IDs must be unique")
        return self


class CorrectionNotice(ArchiveModel):
    """A scoped historical statement plus its validated replacement chain."""

    superseded_memory_id: Identifier
    sources: tuple[SourceRef, ...] = Field(min_length=1)
    replacement_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=32)
    author_ids: tuple[Identifier, ...] = Field(min_length=1)
    latest: MemoryEntry | None = None

    @model_validator(mode="after")
    def correction_boundaries(self) -> CorrectionNotice:
        scope = self.sources[0].scope
        if any(s.scope != scope for s in self.sources):
            raise ValueError("correction sources must share a scope")
        if self.superseded_memory_id in self.replacement_ids or len(
            set(self.replacement_ids)
        ) != len(self.replacement_ids):
            raise ValueError("correction chain cannot cycle")
        if self.latest is not None and (
            self.latest.scope != scope
            or not self.latest.active
            or self.latest.memory_id != self.replacement_ids[-1]
            or set(self.latest.author_ids) != set(self.author_ids)
        ):
            raise ValueError("latest correction must be active, in scope and by the same authors")
        return self


class RetrievalCandidate(ArchiveModel):
    candidate_id: Identifier
    scope: ConversationScope
    sources: tuple[SourceRef, ...] = Field(min_length=1)
    memory_ids: tuple[Identifier, ...] = ()
    corrections: tuple[CorrectionNotice, ...] = Field(default=(), max_length=6)
    correction_error: str | None = Field(default=None, max_length=256)
    occurred_at: datetime | None = None
    observation_ids: tuple[Identifier, ...] = ()
    summary: str = Field(min_length=1, max_length=4000)
    author_ids: tuple[Identifier, ...] = Field(min_length=1)
    reasons: tuple[Literal["quote", "keyword", "semantic", "recent", "memory"], ...] = Field(
        min_length=1
    )
    score: float = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def scoped_sources(self) -> RetrievalCandidate:
        if any(source.scope != self.scope for source in self.sources):
            raise ValueError("candidate sources cannot cross conversation scope")
        if any(s.scope != self.scope for n in self.corrections for s in n.sources):
            raise ValueError("candidate corrections cannot cross conversation scope")
        return self


class RetrievalResult(ArchiveModel):
    scope: ConversationScope
    candidates: tuple[RetrievalCandidate, ...] = Field(default=(), max_length=20)
    index_version: Identifier
    unresolved_references: tuple[SourceRef, ...] = Field(default=(), max_length=20)
    degraded_reason: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def scoped_candidates(self) -> RetrievalResult:
        if any(candidate.scope != self.scope for candidate in self.candidates):
            raise ValueError("retrieval candidates cannot cross conversation scope")
        if any(source.scope != self.scope for source in self.unresolved_references):
            raise ValueError("unresolved references cannot cross conversation scope")
        ids = [candidate.candidate_id for candidate in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate IDs must be unique")
        return self


class ContextDecision(ArchiveModel):
    """Untrusted classifier output. Never contains a rewritten user request."""

    mode: Literal["standalone", "related", "clarify"]
    selected_candidate_ids: tuple[Identifier, ...] = Field(default=(), max_length=6)
    current_observation: str | None = Field(default=None, max_length=4000)
    image_source: SourceRef | None = None
    needs_original_image: bool = False
    relation: Literal["none", "followup", "quote", "correction", "topic", "ambiguous"]

    @model_validator(mode="after")
    def decision_shape(self) -> ContextDecision:
        if len(self.selected_candidate_ids) != len(set(self.selected_candidate_ids)):
            raise ValueError("selected candidate IDs must be unique")
        if self.mode == "related" and not self.selected_candidate_ids:
            raise ValueError("related decisions require selected evidence")
        if self.mode != "related" and self.selected_candidate_ids:
            raise ValueError("only related decisions select historical evidence")
        if self.needs_original_image != (self.image_source is not None):
            raise ValueError("an original image requires an exact source")
        if self.image_source is not None and self.image_source.part_id is None:
            raise ValueError("an image source requires an exact part ID")
        return self


class ContextBundle(ArchiveModel):
    """Ephemeral selected data, not the legacy rendered-prompt ContextBundle.

    The assembler must additionally resolve selected IDs against this request's
    candidate set and verify expiry, file hash and ownership before returning.
    """

    current: Message
    decision: ContextDecision
    source_messages: tuple[Message, ...] = ()
    observations: tuple[Observation, ...] = ()
    memories: tuple[MemoryEntry, ...] = ()
    selected_sources: tuple[SourceRef, ...] = ()
    corrections: tuple[CorrectionNotice, ...] = Field(default=(), max_length=6)
    unavailable_sources: tuple[SourceRef, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def evidence_boundaries(self) -> ContextBundle:
        scope = self.current.scope
        if self.current.direction != "inbound":
            raise ValueError("current message must be inbound")
        if any(message.scope != scope for message in self.source_messages):
            raise ValueError("context messages cannot cross conversation scope")
        if any(not message.eligible_as_evidence for message in self.source_messages):
            raise ValueError("unconfirmed assistant messages cannot become context")
        if any(observation.source.scope != scope for observation in self.observations):
            raise ValueError("observations cannot cross conversation scope")
        if any(memory.scope != scope or not memory.active for memory in self.memories):
            raise ValueError("context memories must be active and in scope")
        if any(source.scope != scope for source in self.selected_sources):
            raise ValueError("selected sources cannot cross conversation scope")
        if any(s.scope != scope for n in self.corrections for s in n.sources):
            raise ValueError("context corrections cannot cross conversation scope")
        if any(source.scope != scope for source in self.unavailable_sources):
            raise ValueError("unavailable sources cannot cross conversation scope")
        image = self.decision.image_source
        if image is not None and image.scope != scope:
            raise ValueError("image source cannot cross conversation scope")
        if self.decision.mode != "related":
            if self.source_messages or self.memories or self.selected_sources or self.corrections:
                raise ValueError("standalone/clarify context cannot contain history")
            if any(obs.source.message_id != self.current.message_id for obs in self.observations):
                raise ValueError(
                    "standalone/clarify context cannot contain historical observations"
                )
            if image is not None and image.message_id != self.current.message_id:
                raise ValueError("standalone/clarify context cannot carry a historical image")
        return self


class EmbeddingRequest(ArchiveModel):
    """Bounded worker request; no attachment bytes or conversation authority."""

    request_id: Identifier
    purpose: Literal["query", "document"]
    texts: tuple[Annotated[str, Field(min_length=1, max_length=4000)], ...] = Field(
        min_length=1, max_length=16
    )


class EmbeddingResult(ArchiveModel):
    request_id: Identifier
    model_version: Identifier
    backend: Literal["tensorrt", "cpu"]
    precision: Literal["fp16", "fp32"]
    vectors: tuple[
        Annotated[
            tuple[Annotated[float, Field(allow_inf_nan=False)], ...],
            Field(min_length=512, max_length=512),
        ],
        ...,
    ] = Field(min_length=1, max_length=16)
