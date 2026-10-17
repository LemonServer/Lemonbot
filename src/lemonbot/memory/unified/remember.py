"""Validated derived-memory candidates alongside raw hybrid retrieval."""

from __future__ import annotations

from lemonbot.embeddings.artifacts import EmbeddingError

from .archive import ArchiveError
from .documents import current_query, terms
from .index import RetrievalError
from .maintenance import UnifiedMemoryStore, digest
from .models import MemoryEntry, Message, RetrievalCandidate, RetrievalResult
from .retrieval import HybridContextRetriever


def memory_candidate(entry: MemoryEntry) -> RetrievalCandidate:
    return RetrievalCandidate(
        candidate_id="memory-" + digest(entry.model_dump(mode="json")),
        scope=entry.scope,
        sources=entry.sources,
        memory_ids=(entry.memory_id,),
        summary=entry.text[:4000],
        author_ids=entry.author_ids,
        occurred_at=entry.created_at,
        reasons=("memory",),
        score=0.02,
    )


class MemoryContextRetriever:
    def __init__(self, base: HybridContextRetriever, store: UnifiedMemoryStore) -> None:
        self.base, self.store = base, store

    async def retrieve(self, *, current: Message, limit: int = 20) -> RetrievalResult:
        result = await self.base.retrieve(current=current, limit=limit)
        store_invalid = False
        try:
            entries = await self.store.list_memories(current.scope)
        except ValueError:
            entries = ()
            store_invalid = True
        valid = [
            e
            for e in entries
            if e.active
            and e.created_at <= current.occurred_at
            and all(s.message_id != current.message_id for s in e.sources)
        ]
        query_text = current_query(current)
        query = set(terms(query_text))
        valid.sort(
            key=lambda e: (-len(query & set(terms(e.text))), -e.created_at.timestamp(), e.memory_id)
        )
        scores = {e.memory_id: float(len(query & set(terms(e.text)))) for e in valid}
        semantic = set()
        issues = [result.degraded_reason] if result.degraded_reason else []
        if self.base.worker is not None and valid and 0 < len(query_text) <= 8000:
            async with self.base._lock:
                remaining = min(128, self.base.options.vector_chunks_per_refresh)
                try:
                    vector, _ = await self.base._vector(query_text, "query", 16)
                    for entry in valid[:32]:
                        other, used = await self.base._vector(entry.text, "document", remaining)
                        remaining -= used
                        similarity = sum(a * b for a, b in zip(vector, other, strict=True))
                        if similarity >= self.base.options.semantic_min_similarity:
                            semantic.add(entry.memory_id)
                            scores[entry.memory_id] += similarity
                    if len(valid) > 32:
                        issues.append("memory_semantic_partial")
                except RetrievalError as error:
                    issues.append("memory_semantic_partial")
                    if str(error) != "RetrievalEmbeddingWorkLimit":
                        await self.base.worker.close()
                        self.base.worker = None
                        self.base.unavailable_reason = "memory_semantic_invalid"
                except (EmbeddingError, OSError, TimeoutError):
                    issues.append("memory_semantic_unavailable")
                    await self.base.worker.close()
                    self.base.worker = None
                    self.base.unavailable_reason = "memory_semantic_unavailable"
        valid.sort(key=lambda e: (-scores[e.memory_id], -e.created_at.timestamp(), e.memory_id))
        offered = [
            memory_candidate(e).model_copy(
                update={
                    "reasons": (
                        "memory",
                        "semantic" if e.memory_id in semantic else "keyword",
                    )
                }
            )
            for e in valid
            if scores[e.memory_id] > 0
        ][:6]
        # Raw history remains independently usable; corrections annotate, never erase it.
        raw = list(result.candidates)
        candidates = []
        size = 0
        correction_ids: set[str] = set()
        quoted = [c for c in raw if "quote" in c.reasons]
        for candidate in [*quoted, *offered, *(c for c in raw if "quote" not in c.reasons)]:
            try:
                if store_invalid:
                    raise ArchiveError("CorrectionUnavailable")
                notices = await self.store.correction_notices(
                    candidate.sources, before=current.occurred_at
                )
                if len(correction_ids | {n.superseded_memory_id for n in notices}) > 6:
                    raise ArchiveError("CorrectionNoticeLimit")
                candidate = candidate.model_copy(update={"corrections": notices})
                if len(candidate.model_dump_json()) > self.base.options.candidate_char_budget:
                    candidate = candidate.model_copy(
                        update={
                            "corrections": (),
                            "correction_error": "CorrectionContextExceedsBudget",
                        }
                    )
            except (ArchiveError, ValueError):
                candidate = candidate.model_copy(
                    update={"correction_error": "CorrectionUnavailable"}
                )
            size += len(candidate.model_dump_json())
            if size > self.base.options.candidate_char_budget:
                break
            candidates.append(candidate)
            correction_ids.update(n.superseded_memory_id for n in candidate.corrections)
            if len(candidates) >= limit:
                break
        return result.model_copy(
            update={"candidates": tuple(candidates), "degraded_reason": ";".join(issues) or None}
        )
