"""Transactional SQLite raw archive. Does not navigate, download, model or send."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from lemonbot.domain.models import OutboxState

from .models import ConversationScope, Message, Observation, SourceRef


class ArchiveError(ValueError):
    """Redacted fail-closed archive error; never includes message content."""


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def scope_key(scope: ConversationScope) -> str:
    # Storage partition key only. Existing account/event identity is unchanged.
    return hashlib.sha256(canonical(scope.model_dump(mode="json")).encode()).hexdigest()


class SQLiteMessageArchive:
    def __init__(self, database_path: Path) -> None:
        self.path = database_path.expanduser().resolve()

    @asynccontextmanager
    async def _read(self) -> AsyncIterator[aiosqlite.Connection]:
        # Read commands must never create a missing DB or migrate a live DB.
        if not self.path.is_file():
            raise ArchiveError("ArchiveDatabaseMissing")
        async with aiosqlite.connect(self.path.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA query_only=ON")
            yield db

    @asynccontextmanager
    async def _write(self) -> AsyncIterator[aiosqlite.Connection]:
        if not self.path.is_file():
            raise ArchiveError("ArchiveDatabaseMissing")
        async with aiosqlite.connect(self.path, timeout=5) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("PRAGMA synchronous=FULL")
            await db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    @staticmethod
    async def _get(db: aiosqlite.Connection, source: SourceRef) -> tuple[int, Message] | None:
        async with db.execute(
            "SELECT * FROM context_archive_messages WHERE scope_key=? AND message_id=?",
            (scope_key(source.scope), source.message_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        if row["scope_json"] != canonical(source.scope.model_dump(mode="json")):
            raise ArchiveError("ArchiveScopeConflict")
        async with db.execute(
            "SELECT payload_json FROM context_archive_parts WHERE message_pk=? ORDER BY position",
            (row["id"],),
        ) as cursor:
            parts = [json.loads(item[0]) for item in await cursor.fetchall()]
        async with db.execute(
            "SELECT payload_json FROM context_archive_references "
            "WHERE message_pk=? ORDER BY position",
            (row["id"],),
        ) as cursor:
            refs = [json.loads(item[0]) for item in await cursor.fetchall()]
        message = Message.model_validate(
            {**json.loads(row["header_json"]), "parts": parts, "references": refs}
        )
        if message.scope != source.scope or message.message_id != source.message_id:
            raise ArchiveError("ArchiveScopeConflict")
        if row["occurred_at"] != message.occurred_at.isoformat():
            raise ArchiveError("ArchivePayloadCorrupt")
        digest = hashlib.sha256(canonical(message.model_dump(mode="json")).encode()).hexdigest()
        if digest != row["payload_sha256"]:
            raise ArchiveError("ArchivePayloadCorrupt")
        if source.part_id is not None and source.part_id not in {p.part_id for p in message.parts}:
            return None
        return row["id"], message

    async def get_message(self, source: SourceRef) -> Message | None:
        async with self._read() as db:
            result = await self._get(db, source)
        return None if result is None else result[1]

    @staticmethod
    async def _outbox(
        db: aiosqlite.Connection, message: Message, receipt_id: str
    ) -> aiosqlite.Row | None:
        async with db.execute(
            "SELECT * FROM outbox WHERE channel=? AND chat_id=? AND external_id=?",
            (message.scope.channel, message.scope.conversation_id, receipt_id),
        ) as cursor:
            rows = list(await cursor.fetchall())
        if not rows:
            return None  # Manual self message; no bot-send confirmation exists.
        if len(rows) != 1:
            raise ArchiveError("ArchiveReceiptAmbiguous")
        row = rows[0]
        if row["state"] != OutboxState.ACKNOWLEDGED.value:
            return None  # UNKNOWN/failed/pending remain solely in the old audit/outbox.
        text_parts = [part for part in message.parts if part.kind == "text"]
        if (
            row["acknowledged_at"] is None
            or message.delivery_state is not OutboxState.ACKNOWLEDGED
            or len(text_parts) != 1
            or text_parts[0].text != row["text"]
            or any(part.kind not in {"text", "link"} for part in message.parts)
            or not row["reply_to_event_id"]
        ):
            raise ArchiveError("ArchiveReceiptMismatch")
        cause = await SQLiteMessageArchive._get(
            db, SourceRef(scope=message.scope, message_id=row["reply_to_event_id"])
        )
        if cause is None or cause[1].direction != "inbound":
            raise ArchiveError("ArchiveReplySourceMissing")
        if cause[1].event_id != row["reply_to_event_id"]:
            raise ArchiveError("ArchiveReplySourceMismatch")
        return row

    @staticmethod
    async def _put(
        db: aiosqlite.Connection,
        message: Message,
        *,
        outbox_id: str | None = None,
        receipt_id: str | None = None,
    ) -> bool:
        source = SourceRef(scope=message.scope, message_id=message.message_id)
        existing = await SQLiteMessageArchive._get(db, source)
        if existing is not None:
            if existing[1] != message:
                raise ArchiveError("ArchiveMessageConflict")
            async with db.execute(
                "SELECT outbox_message_id,receipt_id FROM context_archive_messages WHERE id=?",
                (existing[0],),
            ) as cursor:
                proof = await cursor.fetchone()
            if proof is None or tuple(proof) != (outbox_id, receipt_id):
                raise ArchiveError("ArchiveReceiptConflict")
            return False
        payload = message.model_dump(mode="json")
        header = {
            key: value for key, value in payload.items() if key not in {"parts", "references"}
        }
        try:
            cursor = await db.execute(
                "INSERT INTO context_archive_messages "
                "(scope_key,scope_json,message_id,occurred_at,header_json,payload_sha256,"
                "outbox_message_id,receipt_id) VALUES (?,?,?,?,?,?,?,?)",
                (
                    scope_key(message.scope),
                    canonical(payload["scope"]),
                    message.message_id,
                    message.occurred_at.isoformat(),
                    canonical(header),
                    hashlib.sha256(canonical(payload).encode()).hexdigest(),
                    outbox_id,
                    receipt_id,
                ),
            )
        except aiosqlite.IntegrityError:
            raise ArchiveError("ArchiveIdentityConflict") from None
        message_pk = cursor.lastrowid
        await cursor.close()
        await db.executemany(
            "INSERT INTO context_archive_parts VALUES (?,?,?,?,?)",
            [
                (message_pk, i, part.part_id, part.kind, canonical(part.model_dump(mode="json")))
                for i, part in enumerate(message.parts)
            ],
        )
        await db.executemany(
            "INSERT INTO context_archive_references VALUES (?,?,?,?,?)",
            [
                (message_pk, i, ref.message_id, ref.part_id, canonical(ref.model_dump(mode="json")))
                for i, ref in enumerate(message.references)
            ],
        )
        return True

    async def put_message(self, message: Message) -> bool:
        if message.direction != "inbound":
            raise ArchiveError("ArchiveOutboundRequiresReceipt")
        async with self._write() as db:
            return await self._put(db, message)

    async def put_observed_batch(self, messages: Sequence[tuple[Message, str | None]]) -> int:
        """Atomically archive one verified target snapshot before advancing anchors.

        A receipt ID for a self row is derived from that exact native message ID,
        never selected by body. Unknown/manual self rows are not promoted.
        """
        if not messages:
            return 0
        if len(messages) > 1000 or len({m.scope for m, _ in messages}) != 1:
            raise ArchiveError("ArchiveBatchBounds")
        inserted = 0
        async with self._write() as db:
            for message, receipt_id in messages:
                outbox_id = None
                if message.direction == "outbound":
                    if receipt_id is None:
                        raise ArchiveError("ArchiveOutboundRequiresReceipt")
                    outbox = await self._outbox(db, message, receipt_id)
                    if outbox is None:
                        continue
                    outbox_id = outbox["message_id"]
                elif receipt_id is not None:
                    raise ArchiveError("ArchiveInboundReceiptForbidden")
                inserted += await self._put(db, message, outbox_id=outbox_id, receipt_id=receipt_id)
        return inserted

    async def apply_delivery_state(self, source: SourceRef, state: OutboxState) -> Message:
        # Stage 2 archives only already-confirmed self observations. No API here
        # can turn an unconfirmed send into delivered history or enqueue a retry.
        async with self._read() as db:
            existing = await self._get(db, source)
            if existing is None or existing[1].direction != "outbound":
                raise ArchiveError("ArchiveOutboundMissing")
            async with db.execute(
                "SELECT receipt_id FROM context_archive_messages WHERE id=?", (existing[0],)
            ) as cursor:
                proof = await cursor.fetchone()
            if state is not OutboxState.ACKNOWLEDGED or proof is None:
                raise ArchiveError("ArchiveDeliveryTransitionForbidden")
            if await self._outbox(db, existing[1], proof[0]) is None:
                raise ArchiveError("ArchiveReceiptMismatch")
            return existing[1]

    async def put_observation(self, observation: Observation) -> bool:
        async with self._write() as db:
            source = await self._get(db, observation.source)
            if source is None or not source[1].eligible_as_evidence:
                raise ArchiveError("ArchiveObservationSourceMissing")
            part = next(
                (p for p in source[1].parts if p.part_id == observation.source.part_id), None
            )
            if observation.kind in {"image_description", "ocr"} and (
                part is None or part.kind != "image"
            ):
                raise ArchiveError("ArchiveObservationPartMismatch")
            if observation.kind in {"page_body", "page_title"} and (
                part is None or part.kind not in {"link", "card"}
            ):
                raise ArchiveError("ArchiveObservationPartMismatch")
            payload = canonical(observation.model_dump(mode="json"))
            key = scope_key(observation.source.scope)
            async with db.execute(
                "SELECT payload_json FROM context_archive_observations "
                "WHERE scope_key=? AND observation_id=?",
                (key, observation.observation_id),
            ) as cursor:
                old = await cursor.fetchone()
            if old is not None:
                if old[0] != payload:
                    raise ArchiveError("ArchiveObservationConflict")
                return False
            await db.execute(
                "INSERT INTO context_archive_observations VALUES (?,?,?,?,?)",
                (key, observation.observation_id, source[0], observation.source.part_id, payload),
            )
            return True

    async def get_observations(self, source: SourceRef) -> tuple[Observation, ...]:
        async with self._read() as db:
            found = await self._get(db, source)
            if found is None:
                return ()
            async with db.execute(
                "SELECT payload_json FROM context_archive_observations WHERE message_pk=? "
                "AND (? IS NULL OR part_id=?) ORDER BY observation_id",
                (found[0], source.part_id, source.part_id),
            ) as cursor:
                observations = tuple(
                    Observation.model_validate_json(row[0]) for row in await cursor.fetchall()
                )
            if any(
                item.source.scope != source.scope
                or item.source.message_id != source.message_id
                or (source.part_id is not None and item.source.part_id != source.part_id)
                for item in observations
            ):
                raise ArchiveError("ArchiveScopeConflict")
            return observations

    async def list_messages(
        self, *, scope: ConversationScope, before_message_id: str | None, limit: int
    ) -> tuple[Message, ...]:
        if not 1 <= limit <= 1000:
            raise ArchiveError("ArchiveQueryBounds")
        async with self._read() as db:
            boundary: tuple[str, str] | None = None
            if before_message_id is not None:
                found = await self._get(db, SourceRef(scope=scope, message_id=before_message_id))
                if found is None:
                    raise ArchiveError("ArchiveCursorMissing")
                boundary = (found[1].occurred_at.isoformat(), found[1].message_id)
            async with db.execute(
                "SELECT message_id FROM context_archive_messages WHERE scope_key=? "
                "AND (? IS NULL OR (occurred_at,message_id)<(?,?)) "
                "ORDER BY occurred_at DESC,message_id DESC LIMIT ?",
                (
                    scope_key(scope),
                    None if boundary is None else boundary[0],
                    None if boundary is None else boundary[0],
                    None if boundary is None else boundary[1],
                    limit,
                ),
            ) as cursor:
                ids = [row[0] for row in await cursor.fetchall()]
            result = []
            for identifier in ids:
                message = await self._get(db, SourceRef(scope=scope, message_id=identifier))
                if message is None or not message[1].eligible_as_evidence:
                    raise ArchiveError("ArchiveEvidenceInvalid")
                result.append(message[1])
            return tuple(result)

    async def scan_scope(
        self, *, scope: ConversationScope, limit: int
    ) -> AsyncGenerator[tuple[Message, tuple[Observation, ...]], None]:
        """One read transaction for a bounded retrieval rebuild, newest first.

        Unlike repeated pagination this shares a coherent snapshot for parts and
        observations. The caller requests max_messages + 1 to detect overflow.
        """
        if not 1 <= limit <= 10001:
            raise ArchiveError("ArchiveQueryBounds")
        async with self._read() as db:
            await db.execute("BEGIN")
            async with db.execute(
                "SELECT message_id FROM context_archive_messages WHERE scope_key=? "
                "ORDER BY occurred_at DESC,message_id DESC LIMIT ?",
                (scope_key(scope), limit),
            ) as cursor:
                identifiers = [row[0] for row in await cursor.fetchall()]
            for identifier in identifiers:
                source = SourceRef(scope=scope, message_id=identifier)
                found = await self._get(db, source)
                if found is None or not found[1].eligible_as_evidence:
                    raise ArchiveError("ArchiveEvidenceInvalid")
                async with db.execute(
                    "SELECT payload_json FROM context_archive_observations WHERE message_pk=? "
                    "ORDER BY observation_id",
                    (found[0],),
                ) as cursor:
                    observations = tuple(
                        Observation.model_validate_json(row[0]) for row in await cursor.fetchall()
                    )
                if any(
                    item.source.scope != scope or item.source.message_id != identifier
                    for item in observations
                ):
                    raise ArchiveError("ArchiveScopeConflict")
                yield found[1], observations
