"""Conversation-scoped memory records with mandatory derivation provenance."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lemonbot.domain.models import DataClass, MessageRole, ModelMessage


def utc_now() -> datetime:
    return datetime.now(UTC)


class MemoryKind(StrEnum):
    SUMMARY = "summary"
    FACT = "fact"
    PREFERENCE = "preference"
    COMMITMENT = "commitment"
    EPISODE = "episode"


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_message_ids: tuple[str, ...] = Field(min_length=1)
    source_event_ids: tuple[str, ...] = ()
    model: str = Field(min_length=1, max_length=256)
    prompt_version: str = Field(min_length=1, max_length=128)
    confidence: float = Field(ge=0, le=1)
    supersedes: tuple[UUID, ...] = ()
    derived_at: datetime = Field(default_factory=utc_now)

    @field_validator("source_message_ids", "source_event_ids")
    @classmethod
    def identifiers_must_be_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("provenance identifiers cannot be blank")
        if len(value) != len(set(value)):
            raise ValueError("provenance identifiers must be unique")
        return value

    @field_validator("derived_at")
    @classmethod
    def timestamp_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("derived_at must include a timezone")
        return value.astimezone(UTC)


class MemoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: UUID = Field(default_factory=uuid4)
    channel: str = Field(min_length=1, max_length=64)
    chat_id: str = Field(min_length=1, max_length=512)
    kind: MemoryKind
    text: str = Field(min_length=1, max_length=100_000)
    provenance: Provenance
    importance: float = Field(default=0.5, ge=0, le=1)
    data_class: Literal[DataClass.PUBLIC, DataClass.CONVERSATION] = DataClass.CONVERSATION
    created_at: datetime = Field(default_factory=utc_now)
    active: bool = True

    @field_validator("created_at")
    @classmethod
    def timestamp_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        return value.astimezone(UTC)


class ConversationTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str = Field(min_length=1, max_length=256)
    event_id: str | None = Field(default=None, max_length=256)
    channel: str = Field(min_length=1, max_length=64)
    chat_id: str = Field(min_length=1, max_length=512)
    role: MessageRole
    content: str = Field(max_length=100_000)
    occurred_at: datetime = Field(default_factory=utc_now)

    @field_validator("occurred_at")
    @classmethod
    def timestamp_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        return value.astimezone(UTC)


class SearchHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record: MemoryRecord
    score: float = Field(ge=0)
    matched_terms: tuple[str, ...] = ()


class GeneratedSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=100_000)
    model: str = Field(min_length=1, max_length=256)
    prompt_version: str = Field(min_length=1, max_length=128)
    confidence: float = Field(ge=0, le=1)


class ContextBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    messages: tuple[ModelMessage, ...]
    memory_ids: tuple[UUID, ...]
    estimated_tokens: int = Field(ge=0)
    omitted_turns: int = Field(ge=0)
    omitted_memories: int = Field(ge=0)
    truncated: bool

    @model_validator(mode="after")
    def truncation_matches_omissions(self) -> ContextBundle:
        expected = bool(self.omitted_turns or self.omitted_memories)
        if self.truncated != expected:
            raise ValueError("truncated must reflect omitted context")
        return self

