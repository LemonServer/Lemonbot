"""Post-ACK local maintenance. Never sends or initiates a cloud call."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from lemonbot.domain.models import OutboundMessage
from lemonbot.memory.models import MemoryKind

from .archive import ArchiveError, canonical, scope_key
from .maintenance import UnifiedMemoryStore, digest
from .models import ContextBundle, MemoryEntry, Message, Observation, SourceRef


def delivery_metadata(bundle: ContextBundle) -> dict[str, Any]:
    return {
        "current": bundle.current.message_id,
        "observations": [
            o.model_dump(mode="json")
            for o in bundle.observations
            if o.extractor_version == "context-classifier/v1"
            and o.observation_id == "classifier-current-image"
        ],
        "correction": bundle.decision.relation == "correction",
        "memory_ids": [e.memory_id for e in bundle.memories],
        "corrected_sources": [
            ref.model_dump(mode="json")
            for ref in bundle.selected_sources
            if any(
                m.message_id == ref.message_id
                and m.direction == "inbound"
                and m.sender_id == bundle.current.sender_id
                for m in bundle.source_messages
            )
        ],
    }


async def confirm_delivery(
    store: UnifiedMemoryStore, outbound: OutboundMessage, observed: tuple[Message, str]
) -> None:
    message, receipt = observed
    metadata = outbound.metadata.get("unified_context", {})
    if (
        message.scope.channel != outbound.channel
        or message.scope.conversation_id != outbound.chat_id
        or metadata.get("current") != outbound.reply_to_event_id
    ):
        raise ArchiveError("MemoryReceiptScopeMismatch")
    archive = store.archive
    # Persisted ACK and exact native receipt are verified again by the raw archive.
    async with archive._read() as db:
        proof = await archive._outbox(db, message, receipt)
        if proof is None or proof["message_id"] != str(outbound.message_id):
            raise ArchiveError("MemoryReceiptMissing")
    await archive.put_observed_batch([(message, receipt)])
    key = scope_key(message.scope)
    async with archive._write() as db:
        async with db.execute(
            "SELECT 1 FROM unified_receipts WHERE outbox_id=?", (str(outbound.message_id),)
        ) as c:
            if await c.fetchone():
                return
        current = await archive._get(
            db, SourceRef(scope=message.scope, message_id=outbound.reply_to_event_id or "")
        )
        if current is None:
            raise ArchiveError("MemoryReplySourceMissing")
        for raw in metadata.get("observations", []):
            observation = Observation.model_validate(raw)
            if (
                observation.source.scope != message.scope
                or observation.extractor_version != "context-classifier/v1"
            ):
                raise ArchiveError("MemoryObservationMismatch")
            # Older queued bundles also carried already archived descriptions.
            # Only the assembler's fresh observation is an ACK-time write; replaying
            # historical evidence would duplicate it and collide with the new one.
            if observation.observation_id != "classifier-current-image":
                continue
            source = await archive._get(db, observation.source)
            if source is None:
                raise ArchiveError("MemoryObservationSourceMissing")
            # The observation is a description of a validated source, not fresh image access.
            part = next(
                (p for p in source[1].parts if p.part_id == observation.source.part_id), None
            )
            if part is None or part.kind != "image" or observation.status != "available":
                raise ArchiveError("MemoryObservationPartMismatch")
            identifier = digest(
                [
                    str(outbound.message_id),
                    observation.source.model_dump(mode="json"),
                    "description/v1",
                ]
            )
            observation = observation.model_copy(update={"observation_id": identifier})
            payload = canonical(observation.model_dump(mode="json"))
            await db.execute(
                "INSERT INTO context_archive_observations VALUES (?,?,?,?,?)",
                (key, identifier, source[0], observation.source.part_id, payload),
            )
            cache_key = digest(
                [source[1].model_dump(mode="json"), observation.extractor_version, observation.text]
            )
            await db.execute(
                "INSERT OR IGNORE INTO unified_content_cache VALUES (?,?,?,?)",
                (
                    key,
                    cache_key,
                    canonical(observation.source.model_dump(mode="json")),
                    payload,
                ),
            )
        correction_ids = list(metadata.get("memory_ids", []))
        if metadata.get("correction"):
            for raw in metadata.get("corrected_sources", []):
                # Existing queued metadata used whole-message IDs; preserve that explicit scope.
                reference = (
                    SourceRef(scope=message.scope, message_id=raw)
                    if isinstance(raw, str)
                    else SourceRef.model_validate(raw)
                )
                if reference.scope != message.scope:
                    raise ArchiveError("MemoryCorrectionSourceInvalid")
                source = await archive._get(db, reference)
                if source is None or source[1].sender_id != current[1].sender_id:
                    raise ArchiveError("MemoryCorrectionSourceInvalid")
                text = "\n".join(
                    p.text or p.url or ""
                    for p in source[1].parts
                    if reference.part_id is None or p.part_id == reference.part_id
                ).strip()
                if not text:
                    continue
                previous = MemoryEntry(
                    memory_id=digest([key, reference.model_dump(mode="json"), "raw-version/v2"]),
                    scope=message.scope,
                    kind=MemoryKind.FACT,
                    text=text,
                    sources=(reference,),
                    author_ids=(source[1].sender_id,),
                    derivation_version="raw-version/v2",
                    created_at=source[1].occurred_at,
                )
                await store._put(db, previous)
                correction_ids.append(previous.memory_id)
        correction_ids = list(dict.fromkeys(correction_ids))
        if metadata.get("correction") and correction_ids:
            old = [await store._get(db, message.scope, identifier) for identifier in correction_ids]
            if all(
                e is not None and e.active and e.author_ids == (current[1].sender_id,) for e in old
            ):
                text = "\n".join(p.text or p.url or "" for p in current[1].parts)
                entry = MemoryEntry(
                    memory_id=digest([str(outbound.message_id), "correction/v1"]),
                    scope=message.scope,
                    kind=MemoryKind.FACT,
                    text=text,
                    sources=(SourceRef(scope=message.scope, message_id=current[1].message_id),),
                    author_ids=(current[1].sender_id,),
                    derivation_version="confirmed-correction/v1",
                    created_at=datetime.now(UTC),
                    supersedes=tuple(correction_ids),
                )
                await store._put(db, entry)
        await db.execute(
            "INSERT INTO unified_receipts VALUES (?,?,?)",
            (str(outbound.message_id), key, datetime.now(UTC).isoformat()),
        )
