"""Exact archive-to-intake image binding. Never choose by recency or body."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID

from lemonbot.domain.models import ModelImage
from lemonbot.storage import CoreRepository
from lemonbot.tools.attachments import AttachmentStore

from .archive import SQLiteMessageArchive
from .models import Message, SourceRef


class ImageResolver(Protocol):
    async def resolve(self, *, current: Message, source: SourceRef) -> ModelImage | None: ...


class BoundImageResolver:
    def __init__(
        self,
        archive: SQLiteMessageArchive,
        repository: CoreRepository,
        attachments: AttachmentStore,
        *,
        retention_days: int = 7,
    ) -> None:
        self.archive, self.repository, self.attachments = archive, repository, attachments
        self.retention_days = retention_days

    @staticmethod
    def _verify(path: Path, digest: str, size: int) -> bool:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as content:
            info = os.fstat(content.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != size or size > 10 * 1024 * 1024:
                return False
            hasher = hashlib.sha256()
            remaining = size + 1
            received = 0
            while remaining:
                data = content.read(min(remaining, 1024 * 1024))
                if not data:
                    break
                hasher.update(data)
                received += len(data)
                remaining -= len(data)
            return received == size and hasher.hexdigest() == digest

    async def resolve(self, *, current: Message, source: SourceRef) -> ModelImage | None:
        if source.scope != current.scope or source.part_id is None:
            return None
        found = await self.archive.get_message(source)
        if found is None or not found.eligible_as_evidence:
            return None
        part = next((p for p in found.parts if p.part_id == source.part_id), None)
        now = datetime.now(UTC)
        if (
            part is None
            or part.kind != "image"
            or (part.attachment is not None and part.attachment.expires_at <= now)
        ):
            return None
        # Intake envelopes already carry the exact broker-verified image_source_ref.
        # Multiple matching intakes are refused, never resolved with first/latest.
        direct = []
        for identifier in dict.fromkeys((current.message_id, source.message_id)):
            event = await self.repository.inbound_event(
                current.scope.channel,
                current.scope.conversation_id,
                identifier,
            )
            if event is not None and event.metadata.get("image_source_ref") == source.message_id:
                direct.append(event)
        # Exact current intake or original-source event is causal, not a recency guess.
        intakes = (
            tuple(direct[:1])
            if direct
            else await self.repository.image_intakes(
                current.scope.channel,
                current.scope.conversation_id,
                source.message_id,
            )
        )
        if len(intakes) != 1:
            return None
        event = intakes[0]
        intake = await self.archive.get_message(
            SourceRef(scope=current.scope, message_id=event.event_id)
        )
        if intake is None or intake.direction != "inbound" or intake.sender_id != event.sender_id:
            return None
        if intake.message_id != source.message_id and not any(
            ref.message_id == source.message_id and ref.part_id in {None, source.part_id}
            for ref in intake.references
        ):
            return None
        ids = event.metadata.get("attachment_ids")
        if (
            event.metadata.get("image_source") != "validated_wal_snapshot"
            or not isinstance(ids, list)
            or len(ids) != 1
            or not isinstance(ids[0], str)
        ):
            return None
        try:
            record, path = await self.attachments.resolve_bound(
                UUID(ids[0]),
                channel=event.channel,
                chat_id=event.chat_id,
                event_id=event.event_id,
            )
            if record.media_type not in {"image/jpeg", "image/png", "image/webp"} or (
                record.created_at > now
                or now - record.created_at > timedelta(days=self.retention_days)
            ):
                return None
            if part.attachment is not None and (part.attachment.sha256, part.attachment.size) != (
                record.sha256,
                record.size,
            ):
                return None
            if not await asyncio.to_thread(self._verify, path, record.sha256, record.size):
                return None
            return ModelImage(sha256=record.sha256, size=record.size)
        except (ValueError, KeyError, OSError, RuntimeError, PermissionError):
            return None
