"""Memory persistence boundary plus a deterministic in-memory implementation."""

from __future__ import annotations

import asyncio
import math
import re
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from .models import MemoryKind, MemoryRecord, SearchHit


class MemoryScopeError(ValueError):
    """An operation attempted to cross a channel/chat isolation boundary."""


@runtime_checkable
class MemoryStore(Protocol):
    async def add(self, record: MemoryRecord) -> None: ...

    async def get(
        self,
        memory_id: UUID,
        *,
        channel: str,
        chat_id: str,
    ) -> MemoryRecord | None: ...

    async def search(
        self,
        *,
        channel: str,
        chat_id: str,
        query: str,
        kinds: Sequence[MemoryKind] | None = None,
        limit: int = 10,
    ) -> tuple[SearchHit, ...]: ...


_WORD_RE = re.compile(r"[a-zA-Z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff]")


def _terms(text: str) -> tuple[str, ...]:
    base = [match.group(0).casefold() for match in _WORD_RE.finditer(text)]
    cjk = [item for item in base if len(item) == 1 and "\u3400" <= item <= "\u9fff"]
    # Character bigrams preserve useful phrase specificity for Chinese while
    # keeping the implementation dependency-free.  Production SQLite uses FTS5.
    bigrams = ["".join(cjk[index : index + 2]) for index in range(len(cjk) - 1)]
    return tuple(base + bigrams)


class InMemoryMemoryStore:
    """Reference store used by unit tests and disconnected development."""

    def __init__(self) -> None:
        self._records: dict[UUID, MemoryRecord] = {}
        self._lock = asyncio.Lock()

    async def add(self, record: MemoryRecord) -> None:
        async with self._lock:
            if record.memory_id in self._records:
                raise ValueError(f"memory {record.memory_id} already exists")
            replacements: list[MemoryRecord] = []
            for superseded_id in record.provenance.supersedes:
                old = self._records.get(superseded_id)
                if old is None:
                    raise ValueError(f"superseded memory {superseded_id} does not exist")
                if (old.channel, old.chat_id) != (record.channel, record.chat_id):
                    raise MemoryScopeError("a memory cannot supersede another conversation")
                if not old.active:
                    raise ValueError(f"superseded memory {superseded_id} is already inactive")
                replacements.append(old)
            for old in replacements:
                self._records[old.memory_id] = old.model_copy(update={"active": False})
            self._records[record.memory_id] = record

    async def get(
        self,
        memory_id: UUID,
        *,
        channel: str,
        chat_id: str,
    ) -> MemoryRecord | None:
        if not channel or not chat_id:
            raise MemoryScopeError("channel and chat_id are mandatory")
        async with self._lock:
            record = self._records.get(memory_id)
            if record is None or (record.channel, record.chat_id) != (channel, chat_id):
                return None
            return record

    @staticmethod
    def _score(record: MemoryRecord, query_terms: tuple[str, ...], now: datetime) -> SearchHit:
        record_terms = _terms(record.text)
        counts = Counter(record_terms)
        unique_query = tuple(dict.fromkeys(query_terms))
        matched = tuple(term for term in unique_query if counts[term])
        if not unique_query:
            lexical = 0.0
        else:
            lexical = sum(1.0 + math.log(counts[term]) for term in matched) / len(unique_query)
        age_days = max(0.0, (now - record.created_at).total_seconds() / 86_400)
        recency = 1.0 / (1.0 + age_days / 30.0)
        kind_bonus = 0.15 if record.kind is MemoryKind.COMMITMENT else 0.0
        score = lexical * 0.75 + record.importance * 0.15 + recency * 0.1 + kind_bonus
        return SearchHit(record=record, score=max(0.0, score), matched_terms=matched)

    async def search(
        self,
        *,
        channel: str,
        chat_id: str,
        query: str,
        kinds: Sequence[MemoryKind] | None = None,
        limit: int = 10,
    ) -> tuple[SearchHit, ...]:
        if not channel or not chat_id:
            raise MemoryScopeError("channel and chat_id are mandatory")
        if limit < 1 or limit > 100:
            raise ValueError("search limit must be between 1 and 100")
        allowed = set(kinds) if kinds is not None else None
        query_terms = _terms(query)
        now = datetime.now(UTC)
        async with self._lock:
            candidates = [
                item
                for item in self._records.values()
                if item.active
                and item.channel == channel
                and item.chat_id == chat_id
                and (allowed is None or item.kind in allowed)
            ]
        hits = [self._score(item, query_terms, now) for item in candidates]
        if query_terms:
            hits = [
                hit for hit in hits if hit.matched_terms or hit.record.kind is MemoryKind.COMMITMENT
            ]
        hits.sort(key=lambda hit: (hit.score, hit.record.created_at), reverse=True)
        return tuple(hits[:limit])
