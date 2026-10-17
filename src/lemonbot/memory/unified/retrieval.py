"""Bounded scoped hybrid retrieval; no model classification or sending route."""

from __future__ import annotations

import asyncio
import math
from collections import Counter
from collections.abc import Callable
from contextlib import aclosing
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import Field

from lemonbot.embeddings.artifacts import EmbeddingError
from lemonbot.embeddings.protocol import Ready
from lemonbot.embeddings.text import TextChunk

from .archive import SQLiteMessageArchive
from .documents import (
    INDEX_VERSION,
    IndexUnit,
    current_query,
    image_followup,
    literal_passages,
    message_units,
    query_over_limit,
    terms,
)
from .index import RetrievalError, SQLiteRetrievalIndex, checked_vector
from .models import (
    ArchiveModel,
    ConversationScope,
    EmbeddingRequest,
    EmbeddingResult,
    Message,
    RetrievalCandidate,
    RetrievalResult,
    SourceRef,
)


class RetrievalEmbedder(Protocol):
    ready: Ready

    async def chunks(
        self, text: str, purpose: Literal["query", "document"]
    ) -> tuple[TextChunk, ...]: ...
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult: ...
    async def close(self) -> None: ...


class SyncReport(ArchiveModel):
    messages: int
    units: int
    changed: int
    vectorized: int
    pending_vectors: int
    model_version: str | None
    degraded_reason: str | None


class RetrievalOptions(ArchiveModel):
    max_messages: int = Field(default=10000, ge=1, le=10000)
    vector_chunks_per_refresh: int = Field(default=256, ge=1, le=4096)
    semantic_min_similarity: float = Field(default=0.5, ge=0, le=1)
    candidate_char_budget: int = Field(default=16000, ge=2048, le=32000)
    recent_messages: int = Field(default=6, ge=0, le=20)


class HybridContextRetriever:
    def __init__(
        self,
        archive: SQLiteMessageArchive,
        index: SQLiteRetrievalIndex,
        *,
        worker: RetrievalEmbedder | None = None,
        unavailable_reason: str = "embedding_worker_unavailable",
        options: RetrievalOptions | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if archive.path != index.archive_path:
            raise RetrievalError("RetrievalIndexArchiveMismatch")
        self.archive, self.index, self.worker = archive, index, worker
        self.options = options or RetrievalOptions()
        self.clock = clock
        self.unavailable_reason = unavailable_reason
        self._lock = asyncio.Lock()
        self.last_sync: SyncReport | None = None

    async def _load(
        self, scope: ConversationScope, now: datetime
    ) -> tuple[int, tuple[IndexUnit, ...]]:
        count = 0
        units: list[IndexUnit] = []
        async with aclosing(
            self.archive.scan_scope(
                scope=scope,
                limit=self.options.max_messages + 1,
            )
        ) as snapshot:
            async for message, observations in snapshot:
                count += 1
                if count > self.options.max_messages:
                    raise RetrievalError("RetrievalArchiveScopeTooLarge")
                units.extend(message_units(message, observations, now))
                if len(units) > 50000:
                    raise RetrievalError("RetrievalIndexBounds")
        return count, tuple(units)

    async def _vector(
        self,
        text: str,
        purpose: Literal["query", "document"],
        remaining: int,
    ) -> tuple[tuple[float, ...], int]:
        worker = self.worker
        if worker is None:
            raise EmbeddingError("embedding_worker_unavailable")
        chunks = await worker.chunks(text, purpose)
        offset = 0
        for chunk in chunks:
            if chunk.start != offset or chunk.text != text[chunk.start : chunk.end]:
                raise RetrievalError("RetrievalChunkInvalid")
            offset = chunk.end
        if not chunks or offset != len(text):
            raise RetrievalError("RetrievalChunkInvalid")
        if len(chunks) > remaining:
            raise RetrievalError("RetrievalEmbeddingWorkLimit")
        vectors: list[tuple[float, ...]] = []
        for start in range(0, len(chunks), 16):
            batch = chunks[start : start + 16]
            request = EmbeddingRequest(
                request_id=uuid4().hex, purpose=purpose, texts=tuple(chunk.text for chunk in batch)
            )
            result = await worker.embed(request)
            if (
                result.request_id != request.request_id
                or len(result.vectors) != len(batch)
                or (result.model_version, result.backend, result.precision)
                != (
                    worker.ready.model_version,
                    worker.ready.backend,
                    worker.ready.precision,
                )
            ):
                raise RetrievalError("RetrievalEmbeddingIdentityMismatch")
            vectors.extend(checked_vector(vector) for vector in result.vectors)
        # All chunks contribute. Pooling recipe is part of INDEX_VERSION.
        pooled = tuple(sum(vector[i] for vector in vectors) / len(vectors) for i in range(512))
        norm = math.sqrt(sum(value * value for value in pooled))
        if not math.isfinite(norm) or norm <= 0:
            raise RetrievalError("RetrievalVectorInvalid")
        return checked_vector(tuple(value / norm for value in pooled)), len(chunks)

    async def _refresh(self, scope: ConversationScope, now: datetime) -> SyncReport:
        messages, units = await self._load(scope, now)
        changed = await self.index.replace_scope(scope, units)
        reason = None if self.worker is not None else self.unavailable_reason
        version = None if self.worker is None else self.worker.ready.model_version
        ready_vectors = {} if version is None else await self.index.vectors(scope, version)
        pending = [unit for unit in units if unit.searchable and unit.unit_id not in ready_vectors]
        vectorized = 0
        remaining = self.options.vector_chunks_per_refresh
        for unit in pending:
            if self.worker is None:
                break
            try:
                vector, used = await self._vector(unit.text, "document", remaining)
            except RetrievalError as error:
                if str(error) == "RetrievalEmbeddingWorkLimit":
                    reason = "semantic_index_incomplete"
                    break
                reason = "embedding_result_invalid"
                await self.worker.close()
                self.worker = None
                self.unavailable_reason = reason
                break
            except (EmbeddingError, OSError, TimeoutError):
                reason = "embedding_worker_failed"
                await self.worker.close()
                self.worker = None
                self.unavailable_reason = reason
                break
            assert version is not None
            await self.index.put_vector(unit, version, vector)
            remaining -= used
            vectorized += 1
        report = SyncReport(
            messages=messages,
            units=len(units),
            changed=changed,
            vectorized=vectorized,
            pending_vectors=len(pending) - vectorized,
            model_version=version,
            degraded_reason=reason,
        )
        self.last_sync = report
        return report

    async def refresh(self, scope: ConversationScope) -> SyncReport:
        async with self._lock:
            now = self.clock()
            if now.tzinfo is None or now.utcoffset() is None:
                raise RetrievalError("RetrievalClockInvalid")
            return await self._refresh(scope, now)

    async def retrieve(self, *, current: Message, limit: int = 20) -> RetrievalResult:
        if not 1 <= limit <= 20 or current.direction != "inbound":
            raise RetrievalError("RetrievalQueryBounds")
        if any(reference.scope != current.scope for reference in current.references):
            raise RetrievalError("RetrievalScopeMismatch")
        async with self._lock:
            now = self.clock()
            if now.tzinfo is None or now.utcoffset() is None:
                raise RetrievalError("RetrievalClockInvalid")
            report = await self._refresh(current.scope, now)
            return await self._retrieve(current, limit, now, report)

    async def _retrieve(
        self,
        current: Message,
        limit: int,
        now: datetime,
        report: SyncReport,
    ) -> RetrievalResult:
        scope = current.scope
        query = current_query(current)
        query_terms = set(terms(query)[:64])
        indexed = await self.index.units(scope)
        has_new_image = any(part.kind == "image" for part in current.parts)
        # Re-resolve selected index data against immutable archive + current observations.
        # This also prevents an expired/deleted observation surviving via a stale cache.
        valid: dict[str, IndexUnit] = {}
        for unit in indexed:
            if (
                unit.source.message_id != current.message_id
                and unit.occurred_at <= current.occurred_at
                and (
                    not has_new_image
                    or not unit.media
                    or any(
                        reference.message_id == unit.source.message_id
                        and (reference.part_id is None or reference.part_id == unit.source.part_id)
                        for reference in current.references
                    )
                )
            ):
                valid[unit.unit_id] = unit
        reasons: dict[str, set[str]] = {}
        scores: dict[str, float] = {}
        unresolved = []
        issues = [report.degraded_reason] if report.degraded_reason else []
        if query_over_limit(current):
            issues.append("query_text_limit")

        def offer(unit: IndexUnit, reason: str, score: float) -> None:
            recorded = reasons.setdefault(unit.unit_id, set())
            if reason not in recorded:
                scores[unit.unit_id] = scores.get(unit.unit_id, 0) + score
                recorded.add(reason)

        # Quotes resolve exact IDs/parts; a missing quote never selects a nearby image.
        for reference in current.references[:20]:
            found = await self.archive.get_message(reference)
            if (
                found is None
                or not found.eligible_as_evidence
                or found.message_id == current.message_id
                or found.occurred_at > min(now, current.occurred_at)
            ):
                unresolved.append(reference)
                continue
            quoted = [
                unit
                for unit in valid.values()
                if unit.source.message_id == reference.message_id
                and (reference.part_id is None or unit.source.part_id == reference.part_id)
            ]
            quoted.sort(
                key=lambda unit: (
                    -len(query_terms & set(terms(unit.text))),
                    unit.observation_id is None,
                    unit.start,
                    unit.unit_id,
                )
            )
            if not quoted:
                unresolved.append(reference)
            else:
                offer(quoted[0], "quote", 1.0)
        if len(current.references) > 20:
            issues.append("quote_limit")
        if unresolved:
            issues.append("unresolved_reference")

        if query.strip():
            # A user-quoted passage is stronger evidence than approximate similarity
            # to an earlier summary. It is still only a candidate, never a trigger or
            # a source of URL/tool authorization. Exact message references rank higher.
            passages = literal_passages(query)
            for unit in valid.values():
                if unit.observation_id and any(p in unit.text for p in passages):
                    offer(unit, "keyword", 0.5)
            hits = await self.index.keywords(scope, query)
            ranked = sorted(
                (unit for key, unit in valid.items() if key in hits and unit.searchable),
                key=lambda unit: (
                    -len(query_terms & set(terms(unit.text))),
                    -unit.occurred_at.timestamp(),
                    unit.unit_id,
                ),
            )[:40]
            for rank, unit in enumerate(ranked):
                offer(unit, "keyword", 1 / (60 + rank + 1))
            if self.worker is not None:
                try:
                    vector, _ = await self._vector(query, "query", 16)
                    version = self.worker.ready.model_version
                    vectors = await self.index.vectors(scope, version)
                    semantic = []
                    for key, stored in vectors.items():
                        if key in valid and valid[key].searchable:
                            similarity = sum(a * b for a, b in zip(vector, stored, strict=True))
                            if similarity >= self.options.semantic_min_similarity:
                                semantic.append((similarity, key))
                    ordered = sorted(semantic, key=lambda pair: (-pair[0], pair[1]))[:40]
                    for rank, (_, key) in enumerate(ordered):
                        offer(valid[key], "semantic", 1 / (60 + rank + 1))
                except RetrievalError as error:
                    if str(error) == "RetrievalEmbeddingWorkLimit":
                        issues.append("query_chunk_limit")
                    else:
                        issues.append("embedding_result_invalid")
                        await self.worker.close()
                        self.worker = None
                        self.unavailable_reason = "embedding_result_invalid"
                except (EmbeddingError, OSError, TimeoutError):
                    issues.append("embedding_worker_failed")
                    await self.worker.close()
                    self.worker = None
                    self.unavailable_reason = "embedding_worker_failed"
            recent = sorted(
                valid.values(), key=lambda unit: (-unit.occurred_at.timestamp(), unit.unit_id)
            )
            seen_recent: set[str] = set()
            for unit in recent:
                if unit.media and (current.references or not image_followup(query)):
                    continue
                if unit.source.message_id not in seen_recent:
                    if len(seen_recent) >= self.options.recent_messages:
                        break
                    seen_recent.add(unit.source.message_id)
                    offer(unit, "recent", 0.25 / (60 + len(seen_recent)))
        ordered_reasons: tuple[Literal["quote", "keyword", "semantic", "recent"], ...] = (
            "quote",
            "keyword",
            "semantic",
            "recent",
        )
        selected = []
        size = 0
        per_message: Counter[str] = Counter()
        # Weighted reciprocal-rank fusion; exact quotes dominate every nonquote score.
        ranked_ids = sorted(
            reasons,
            key=lambda key: (
                -scores[key],
                -valid[key].occurred_at.timestamp(),
                key,
            ),
        )
        checked_messages: dict[str, dict[str, IndexUnit]] = {}
        for key in ranked_ids:
            unit = valid[key]
            message_id = unit.source.message_id
            if per_message[message_id] >= 2:
                continue
            if message_id not in checked_messages:
                source = SourceRef(scope=scope, message_id=message_id)
                found = await self.archive.get_message(source)
                observations = await self.archive.get_observations(source)
                actual = () if found is None else message_units(found, observations, now)
                checked_messages[message_id] = {item.unit_id: item for item in actual}
            if checked_messages[message_id].get(key) != unit:
                issues.append("index_source_changed")
                continue
            candidate = RetrievalCandidate(
                candidate_id=unit.unit_id,
                scope=scope,
                sources=(unit.source,),
                summary=unit.text,
                author_ids=(unit.author_id,),
                occurred_at=unit.occurred_at,
                observation_ids=() if unit.observation_id is None else (unit.observation_id,),
                reasons=tuple(reason for reason in ordered_reasons if reason in reasons[key]),
                score=scores[key],
            )
            cost = len(candidate.model_dump_json())
            if size + cost > self.options.candidate_char_budget:
                issues.append("candidate_length_limit")
                continue
            selected.append(candidate)
            size += cost
            per_message[message_id] += 1
            if len(selected) >= limit:
                break
        reason = ";".join(dict.fromkeys(issue for issue in issues if issue)) or None
        return RetrievalResult(
            scope=scope,
            candidates=tuple(selected),
            index_version=INDEX_VERSION,
            degraded_reason=reason,
            unresolved_references=tuple(unresolved),
        )

    async def close(self) -> None:
        if self.worker is not None:
            await self.worker.close()
            self.worker = None
