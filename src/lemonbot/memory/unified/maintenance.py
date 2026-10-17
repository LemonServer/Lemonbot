"""Durable, scoped derived memory and at-most-once maintenance bookkeeping."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

from lemonbot.domain.models import ModelResponse
from lemonbot.models.budget import BudgetManager

from .archive import ArchiveError, SQLiteMessageArchive, canonical, scope_key
from .evidence import EvidenceSnapshot, fingerprint
from .models import (
    ConversationScope,
    CorrectionNotice,
    MemoryEntry,
    Message,
    Observation,
    SourceRef,
)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class UnifiedMemoryStore:
    def __init__(self, archive: SQLiteMessageArchive) -> None:
        self.archive = archive

    async def _evidence(self, db: aiosqlite.Connection, entry: MemoryEntry) -> str:
        authors = set()
        evidence = []
        for ref in entry.sources:
            found = await self.archive._get(db, ref)
            if found is None or not found[1].eligible_as_evidence:
                raise ArchiveError("MemorySourceUnavailable")
            source = found[1]
            if source.occurred_at > entry.created_at:
                raise ArchiveError("MemorySourceInFuture")
            authors.add(source.sender_id)
            observed = []
            async with db.execute(
                "SELECT payload_json FROM context_archive_observations "
                "WHERE message_pk=? ORDER BY observation_id",
                (found[0],),
            ) as cursor:
                for row in await cursor.fetchall():
                    observation = Observation.model_validate_json(row[0])
                    if (
                        (ref.part_id is None or ref.part_id == observation.source.part_id)
                        and observation.status == "available"
                        and observation.created_at <= datetime.now(UTC)
                        and (
                            observation.expires_at is None
                            or observation.expires_at > datetime.now(UTC)
                        )
                    ):
                        observed.append(digest(observation.model_dump(mode="json")))
            evidence.append(digest([source.model_dump(mode="json"), observed]))
        if authors != set(entry.author_ids) or len(entry.author_ids) != len(authors):
            raise ArchiveError("MemoryAuthorMismatch")
        return canonical(evidence)

    async def _put(
        self, db: aiosqlite.Connection, entry: MemoryEntry, *, expected_evidence: str | None = None
    ) -> bool:
        if not entry.active:
            raise ArchiveError("MemoryInactiveInsertionForbidden")
        key = scope_key(entry.scope)
        evidence = await self._evidence(db, entry)
        if expected_evidence is not None and evidence != expected_evidence:
            raise ArchiveError("MemoryEvidenceChanged")
        async with db.execute(
            "SELECT payload FROM unified_memories WHERE scope=? AND memory_id=?",
            (key, entry.memory_id),
        ) as cursor:
            old = await cursor.fetchone()
        if old:
            if old[0] != canonical(entry.model_dump(mode="json")):
                raise ArchiveError("MemoryIdentityConflict")
            return False
        for identifier in entry.supersedes:
            previous = await self._get(db, entry.scope, identifier)
            if (
                previous is None
                or not previous.active
                or set(previous.author_ids) != set(entry.author_ids)
                or previous.created_at > entry.created_at
            ):
                raise ArchiveError("MemoryCorrectionInvalid")
        await db.execute(
            "INSERT INTO unified_memories VALUES (?,?,?,?)",
            (key, entry.memory_id, canonical(entry.model_dump(mode="json")), evidence),
        )
        for identifier in entry.supersedes:
            await db.execute(
                "INSERT INTO unified_supersessions VALUES (?,?,?)",
                (key, identifier, entry.memory_id),
            )
        return True

    async def put_memory(self, entry: MemoryEntry) -> bool:
        async with self.archive._write() as db:
            return await self._put(db, entry)

    async def _get(
        self, db: aiosqlite.Connection, scope: ConversationScope, identifier: str
    ) -> MemoryEntry | None:
        async with db.execute(
            "SELECT payload,evidence,EXISTS(SELECT 1 FROM unified_supersessions s "
            "WHERE s.scope=m.scope AND s.old_id=m.memory_id) FROM unified_memories m "
            "WHERE scope=? AND memory_id=?",
            (scope_key(scope), identifier),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        try:
            entry = MemoryEntry.model_validate_json(row[0])
        except ValueError as error:
            raise ArchiveError("MemoryPayloadInvalid") from error
        if entry.scope != scope or entry.memory_id != identifier:
            raise ArchiveError("MemoryScopeMismatch")
        try:
            valid = await self._evidence(db, entry) == row[1]
        except ArchiveError:
            valid = False
        return entry.model_copy(update={"active": valid and not bool(row[2])})

    async def get_memory(self, *, scope: ConversationScope, memory_id: str) -> MemoryEntry | None:
        async with self.archive._read() as db:
            return await self._get(db, scope, memory_id)

    async def list_memories(self, scope: ConversationScope) -> tuple[MemoryEntry, ...]:
        async with self.archive._read() as db:
            async with db.execute(
                "SELECT memory_id FROM unified_memories WHERE scope=? "
                "ORDER BY memory_id LIMIT 1001",
                (scope_key(scope),),
            ) as cursor:
                ids = [r[0] for r in await cursor.fetchall()]
            if len(ids) > 1000:
                raise ArchiveError("MemoryScopeBounds")
            entries = [await self._get(db, scope, identifier) for identifier in ids]
            return tuple(e for e in entries if e is not None)

    async def _correction_provenance(self, db: aiosqlite.Connection, entry: MemoryEntry) -> None:
        authors = set()
        for ref in entry.sources:
            found = await self.archive._get(db, ref)
            if (
                found is None
                or not found[1].eligible_as_evidence
                or found[1].occurred_at > entry.created_at
            ):
                raise ArchiveError("CorrectionSourceInvalid")
            authors.add(found[1].sender_id)
        if authors != set(entry.author_ids) or len(authors) != len(entry.author_ids):
            raise ArchiveError("CorrectionSourceInvalid")

    async def correction_notices(
        self, sources: tuple[SourceRef, ...], *, before: datetime | None = None
    ) -> tuple[CorrectionNotice, ...]:
        if not sources:
            return ()
        scope = sources[0].scope
        if any(s.scope != scope for s in sources):
            raise ArchiveError("CorrectionScopeMismatch")

        def overlaps(a: SourceRef, b: SourceRef) -> bool:
            return a.message_id == b.message_id and (
                a.part_id is None or b.part_id is None or a.part_id == b.part_id
            )

        async with self.archive._read() as db:
            async with db.execute(
                "SELECT old_id,new_id FROM unified_supersessions WHERE scope=?",
                (scope_key(scope),),
            ) as c:
                links = {str(r[0]): str(r[1]) for r in await c.fetchall()}
            notices = []
            for old_id in sorted(links):
                old = await self._get(db, scope, old_id)
                if old is None:
                    raise ArchiveError("CorrectionSourceMissing")
                if not any(overlaps(a, b) for a in sources for b in old.sources):
                    continue
                await self._correction_provenance(db, old)
                chain: list[str] = []
                seen = {old_id}
                previous = old
                while previous.memory_id in links:
                    identifier = links[previous.memory_id]
                    if identifier in seen or len(chain) >= 32:
                        raise ArchiveError("CorrectionChainInvalid")
                    seen.add(identifier)
                    current = await self._get(db, scope, identifier)
                    if (
                        current is None
                        or set(current.author_ids) != set(old.author_ids)
                        or current.created_at < previous.created_at
                        or previous.memory_id not in current.supersedes
                    ):
                        raise ArchiveError("CorrectionChainInvalid")
                    await self._correction_provenance(db, current)
                    chain.append(identifier)
                    previous = current
                notices.append(
                    CorrectionNotice(
                        superseded_memory_id=old_id,
                        sources=old.sources,
                        replacement_ids=tuple(chain),
                        author_ids=old.author_ids,
                        latest=previous
                        if previous.active and (before is None or previous.created_at <= before)
                        else None,
                    )
                )
                if len(notices) > 6:
                    raise ArchiveError("CorrectionNoticeLimit")
            return tuple(notices)

    async def claim_job(
        self,
        scope: ConversationScope,
        ids: tuple[str, ...],
        *,
        now: datetime,
        reserve: Decimal,
        daily_cap: Decimal,
        timezone: str,
    ) -> str | None:
        key = scope_key(scope)
        day = now.astimezone(ZoneInfo(timezone)).date().isoformat()
        identifier = digest([key, ids, "consolidate/v1"])
        async with self.archive._write() as db:
            async with db.execute("SELECT charged FROM unified_jobs WHERE day=?", (day,)) as c:
                total = sum((Decimal(r[0]) for r in await c.fetchall()), Decimal(0))
            async with db.execute(
                "SELECT 1 FROM unified_jobs WHERE scope=? AND started_at>?",
                (key, (now - timedelta(hours=1)).isoformat()),
            ) as c:
                recent = await c.fetchone()
            if recent or total + reserve > daily_cap:
                return None
            async with db.execute("SELECT 1 FROM unified_jobs WHERE job_id=?", (identifier,)) as c:
                if await c.fetchone():
                    return None
            await db.execute(
                "INSERT INTO unified_jobs VALUES (?,?,?,?,?,?,?)",
                (
                    identifier,
                    key,
                    now.isoformat(),
                    day,
                    "started",
                    str(reserve),
                    canonical(ids),
                ),
            )
        return identifier

    async def processed(self, scope: ConversationScope) -> set[str]:
        async with self.archive._read() as db:
            async with db.execute(
                "SELECT sources FROM unified_jobs WHERE scope=?", (scope_key(scope),)
            ) as c:
                return {
                    identifier for row in await c.fetchall() for identifier in json.loads(row[0])
                }

    async def _validate_snapshot(
        self,
        db: aiosqlite.Connection,
        snapshot: EvidenceSnapshot,
        *,
        allow_superseded: frozenset[str] = frozenset(),
    ) -> None:
        now = datetime.now(UTC)
        for item in snapshot.messages:
            found = await self.archive._get(db, item.source)
            if (
                found is None
                or not found[1].eligible_as_evidence
                or fingerprint(found[1]) != item.sha256
            ):
                raise ArchiveError("MemoryEvidenceChanged")
            async with db.execute(
                "SELECT payload_json FROM context_archive_observations "
                "WHERE message_pk=? ORDER BY observation_id",
                (found[0],),
            ) as c:
                observed = tuple(Observation.model_validate_json(r[0]) for r in await c.fetchall())
            available = tuple(
                o
                for o in observed
                if o.status == "available"
                and o.created_at <= now
                and (o.expires_at is None or o.expires_at > now)
            )
            if tuple((o.observation_id, fingerprint(o)) for o in available) != tuple(
                (o.observation_id, o.sha256) for o in item.observations
            ) or any(o.expires_at is not None and o.expires_at <= now for o in item.observations):
                raise ArchiveError("MemoryEvidenceChanged")
        for offered in snapshot.memories:
            current = await self._get(db, snapshot.scope, offered.memory_id)
            expected = (
                offered.model_copy(update={"active": False})
                if (offered.memory_id in allow_superseded)
                else offered
            )
            if current is None or current != expected:
                raise ArchiveError("MemoryEvidenceChanged")
            if offered.memory_id in allow_superseded:
                # A deliberately superseded entry is inactive for two possible reasons;
                # its original evidence must still be valid at commit time.
                async with db.execute(
                    "SELECT evidence FROM unified_memories WHERE scope=? AND memory_id=?",
                    (scope_key(snapshot.scope), offered.memory_id),
                ) as c:
                    row = await c.fetchone()
                if row is None or await self._evidence(db, offered) != row[0]:
                    raise ArchiveError("MemoryEvidenceChanged")

    async def finish_job(
        self,
        identifier: str,
        entries: tuple[MemoryEntry, ...],
        *,
        charge: Decimal,
        snapshot: EvidenceSnapshot,
    ) -> None:
        rejected = False
        async with self.archive._write() as db:
            async with db.execute(
                "SELECT state,charged,scope,sources FROM unified_jobs WHERE job_id=?", (identifier,)
            ) as c:
                job = await c.fetchone()
            if job is None or job[0] != "started":
                raise ArchiveError("MemoryJobNotStarted")
            if charge < 0 or charge > Decimal(job[1]):
                raise ArchiveError("MemoryJobCostExceeded")
            await db.execute("SAVEPOINT memory_result")
            try:
                if job[2] != scope_key(snapshot.scope) or tuple(json.loads(job[3])) != tuple(
                    m.source.message_id for m in snapshot.messages
                ):
                    raise ArchiveError("MemoryEvidenceChanged")
                await self._validate_snapshot(db, snapshot)
                for entry in entries:
                    await self._put(
                        db, entry, expected_evidence=snapshot.evidence_for(entry.sources)
                    )
                # Time may advance while applying a batch; writes are locked, expiry is not.
                await self._validate_snapshot(
                    db,
                    snapshot,
                    allow_superseded=frozenset(
                        identifier for entry in entries for identifier in entry.supersedes
                    ),
                )
            except ValueError:
                await db.execute("ROLLBACK TO memory_result")
                rejected = True
            await db.execute("RELEASE memory_result")
            await db.execute(
                "UPDATE unified_jobs SET state=?,charged=? WHERE job_id=?",
                ("rejected" if rejected else "done", str(charge), identifier),
            )
        # Raise after committing the terminal state and known charge.
        if rejected:
            raise ArchiveError("MemoryEvidenceChanged")

    async def fail_job(self, identifier: str) -> None:
        async with self.archive._write() as db:
            await db.execute(
                "UPDATE unified_jobs SET state='unknown' WHERE job_id=? AND state='started'",
                (identifier,),
            )

    async def record_usage(
        self,
        operation: str,
        stage: str,
        elapsed_ms: float,
        response: ModelResponse | None,
        budget: BudgetManager,
        *,
        outcome: str,
    ) -> None:
        prompt = response.prompt_tokens if response else 0
        completion = response.completion_tokens if response else 0
        cached = min(prompt, response.cached_prompt_tokens) if response else 0
        price = budget.price_for("deepseek", "deepseek-flash")
        cost = price.cost(
            prompt_tokens=prompt, completion_tokens=completion, cached_prompt_tokens=cached
        )
        if response is None and outcome == "unknown" and not stage.startswith("tool:"):
            cost = price.cost(prompt_tokens=8192, completion_tokens=512)
        async with self.archive._write() as db:
            await db.execute(
                "INSERT OR IGNORE INTO unified_stage_usage VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    operation,
                    stage,
                    datetime.now(UTC).isoformat(),
                    elapsed_ms,
                    prompt,
                    completion,
                    cached,
                    str(cost),
                    outcome,
                ),
            )

    async def cache_content(self, observation: Observation) -> None:
        if observation.status != "available":
            return
        source = await self.archive.get_message(observation.source)
        if source is None or observation not in await self.archive.get_observations(
            observation.source
        ):
            raise ArchiveError("ContentCacheSourceMissing")
        key = digest(
            [source.model_dump(mode="json"), observation.extractor_version, observation.text]
        )
        async with self.archive._write() as db:
            await db.execute(
                "INSERT OR IGNORE INTO unified_content_cache VALUES (?,?,?,?)",
                (
                    scope_key(source.scope),
                    key,
                    canonical(observation.source.model_dump(mode="json")),
                    canonical(observation.model_dump(mode="json")),
                ),
            )

    async def cached_content(self, current: Message) -> tuple[Observation, ...]:
        result = []
        async with self.archive._read() as db:
            async with db.execute(
                "SELECT cache_key,payload FROM unified_content_cache WHERE scope=?",
                (scope_key(current.scope),),
            ) as cursor:
                rows = await cursor.fetchall()
        for key, payload in rows:
            observation = Observation.model_validate_json(payload)
            if observation.source.scope != current.scope:
                raise ArchiveError("ContentCacheScopeMismatch")
            if observation.source.message_id != current.message_id:
                continue
            expected = digest(
                [current.model_dump(mode="json"), observation.extractor_version, observation.text]
            )
            if key != expected or (
                observation.expires_at is not None and observation.expires_at <= datetime.now(UTC)
            ):
                continue
            source = observation.source
            available = await self.archive.get_observations(source)
            if observation in available and observation.status == "available":
                result.append(observation)
        return tuple(result)

    async def topics(self, scope: ConversationScope, *, force: bool = False) -> str:
        entries = [e for e in await self.list_memories(scope) if e.active]
        active = {e.memory_id for e in entries}
        async with self.archive._write() as db:
            async with db.execute(
                "SELECT payload FROM unified_topics WHERE scope=? ORDER BY rowid DESC LIMIT 1",
                (scope_key(scope),),
            ) as cursor:
                cached = await cursor.fetchone()
            previous = [] if cached is None else json.loads(cached[0])
            previous_ids = {e["id"] for e in previous}
            if cached and not force and previous_ids <= active and len(active - previous_ids) < 20:
                return str(cached[0])
            # Corrections/invalidated evidence bypass the normal batch cadence.
            payload = canonical(
                [
                    {"id": e.memory_id, "kind": e.kind.value, "text": e.text[:160]}
                    for e in entries[:20]
                ]
            )
            version = "topics/v1/" + digest([len(entries) // 20, payload])
            await db.execute(
                "INSERT OR IGNORE INTO unified_topics VALUES (?,?,?)",
                (scope_key(scope), version, payload),
            )
            return payload
