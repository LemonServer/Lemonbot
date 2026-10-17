"""Explicit offline import of attributable legacy rows; never queues work."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from lemonbot.connectors.wechat_agent import CHANNEL, reference
from lemonbot.connectors.wechat_agent_conversation import ConversationTarget
from lemonbot.domain.models import OutboxState

from .archive import ArchiveError, SQLiteMessageArchive
from .models import Message, MessagePart
from .wechat import archive_scope


@dataclass
class MigrationReport:
    inserted: int = 0
    existing: int = 0
    excluded: Counter[str] = field(default_factory=Counter)

    def counts(self) -> dict[str, Any]:
        return {
            "inserted": self.inserted,
            "existing": self.existing,
            "excluded": dict(sorted(self.excluded.items())),
        }


def _time(raw: str) -> datetime:
    value = datetime.fromisoformat(raw)
    # Legacy SQLAlchemy SQLite DateTime stored naive UTC, not local wall time.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def migrate_legacy_copy(
    archive: SQLiteMessageArchive, *, targets: tuple[ConversationTarget, ...], key: bytes
) -> MigrationReport:
    """Caller supplies already-verified bindings and an explicit offline copy.

    Preserve every legacy table. Import inbound first so ACK provenance can be
    checked. Repetition is idempotent; conflicts fail without replacing history.
    No cloud extraction, media backfill, source decryption or sending occurs.
    """
    report = MigrationReport()
    async with archive._read() as source:
        for target in targets:
            binding = target.binding
            scope = archive_scope(binding, is_group=target.is_group)
            async with source.execute(
                "SELECT * FROM messages WHERE channel=? AND chat_id=? "
                "ORDER BY CASE role WHEN 'user' THEN 0 ELSE 1 END,occurred_at,id",
                (CHANNEL, binding.chat_ref),
            ) as cursor:
                while batch := await cursor.fetchmany(100):
                    for row in batch:
                        if row["role"] == "user":
                            message = await _inbound(source, row, target, key)
                            if message is None:
                                report.excluded["unverified_inbound"] += 1
                                continue
                            added = await archive.put_message(message)
                        elif row["role"] == "assistant":
                            async with source.execute(
                                "SELECT * FROM outbox "
                                "WHERE channel=? AND chat_id=? AND message_id=?",
                                (CHANNEL, binding.chat_ref, row["external_id"]),
                            ) as outbox_cursor:
                                outbox = await outbox_cursor.fetchone()
                            if (
                                outbox is None
                                or outbox["state"] != OutboxState.ACKNOWLEDGED.value
                                or not outbox["external_id"]
                                or not outbox["acknowledged_at"]
                            ):
                                report.excluded["unconfirmed_assistant"] += 1
                                continue
                            if row["content"] != outbox["text"]:
                                raise ArchiveError("LegacyAssistantConflict")
                            message = Message(
                                scope=scope,
                                message_id=outbox["external_id"],
                                # Legacy rows lack canonical self sender metadata;
                                # use the bound bot-account author, never a nickname.
                                sender_id=binding.account_ref,
                                direction="outbound",
                                occurred_at=_time(row["occurred_at"]),
                                parts=(
                                    MessagePart(part_id="text", kind="text", text=row["content"]),
                                ),
                                delivery_state=OutboxState.ACKNOWLEDGED,
                            )
                            try:
                                added = bool(
                                    await archive.put_observed_batch(
                                        [(message, outbox["external_id"])]
                                    )
                                )
                            except ArchiveError as error:
                                if str(error) == "ArchiveReplySourceMissing":
                                    report.excluded["assistant_source_unverified"] += 1
                                    continue
                                raise
                        else:
                            report.excluded["unsupported_legacy_role"] += 1
                            continue
                        if added:
                            report.inserted += 1
                        else:
                            report.existing += 1
        # Report rows outside the explicitly verified binding set, without IDs/content.
        async with source.execute("SELECT channel,chat_id FROM messages") as cursor:
            allowed = {(CHANNEL, target.binding.chat_ref) for target in targets}
            async for row in cursor:
                if tuple(row) not in allowed:
                    report.excluded["unbound_scope"] += 1
    return report


async def _inbound(
    source: aiosqlite.Connection, row: aiosqlite.Row, target: ConversationTarget, key: bytes
) -> Message | None:
    sender, identifier = row["sender_id"], row["external_id"]
    if not isinstance(sender, str) or not sender or not identifier:
        return None
    binding = target.binding
    try:
        metadata = json.loads(row["metadata_json"])
    except (ValueError, TypeError):
        return None
    if not isinstance(metadata, dict):
        return None
    expected = reference(key, "context", binding.chat_ref, sender)
    if (
        metadata.get("wechat_mode") != "conversation"
        or metadata.get("context_scope") != expected
        or metadata.get("is_group") is not target.is_group
        or (
            not target.is_group
            and sender != reference(key, "sender", binding.account_ref, binding.chat_id)
        )
    ):
        return None
    async with source.execute(
        "SELECT sender_id,text,occurred_at,metadata_json FROM inbox "
        "WHERE channel=? AND chat_id=? AND event_id=?",
        (CHANNEL, binding.chat_ref, identifier),
    ) as cursor:
        inbox = await cursor.fetchone()
    if (
        inbox is None
        or inbox["sender_id"] != sender
        or (inbox["text"] or "") != row["content"]
        or _time(inbox["occurred_at"]) != _time(row["occurred_at"])
    ):
        return None
    try:
        inbound_metadata = json.loads(inbox["metadata_json"])
        if not isinstance(inbound_metadata, dict):
            return None
        if any(
            inbound_metadata.get(k) != metadata.get(k)
            for k in ("wechat_mode", "context_scope", "is_group")
        ):
            return None
    except (ValueError, TypeError):
        return None
    return Message(
        scope=archive_scope(binding, is_group=target.is_group),
        message_id=identifier,
        event_id=identifier,
        sender_id=sender,
        direction="inbound",
        occurred_at=_time(row["occurred_at"]),
        parts=(
            MessagePart(part_id="legacy-text", kind="text", text=row["content"]),
            MessagePart(
                part_id="legacy-structure",
                kind="unknown",
                unavailable_reason="legacy_original_structure_unavailable",
            ),
        ),
    )
