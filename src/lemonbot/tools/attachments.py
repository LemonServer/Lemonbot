from __future__ import annotations

import asyncio
import errno
import hashlib
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePath
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from lemonbot.tools.object_store import ContentAddressedStore, StoredObject

_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,63}$")
DEFAULT_MINIMUM_FREE_BYTES = 1024**3


class AttachmentScopeError(PermissionError):
    pass


class AttachmentCapacityStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    paused: bool = False
    minimum_free_bytes: int = Field(ge=0)
    last_free_bytes: int | None = Field(default=None, ge=0)
    required_object_bytes: int | None = Field(default=None, ge=0)
    reason: str | None = None
    checked_at: datetime | None = None
    triggered_at: datetime | None = None


class AttachmentCapacityError(RuntimeError):
    def __init__(self, status: AttachmentCapacityStatus) -> None:
        self.status = status
        free = "unknown" if status.last_free_bytes is None else str(status.last_free_bytes)
        super().__init__(
            "attachment intake is paused by the disk-capacity circuit breaker "
            f"(free_bytes={free}, reserve_bytes={status.minimum_free_bytes})"
        )


class StoredAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attachment_id: UUID = Field(default_factory=uuid4)
    channel: str
    chat_id: str
    event_id: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str
    original_name: str | None = None
    size: int = Field(ge=1)
    status: str = "quarantined"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AttachmentStore:
    def __init__(
        self,
        database_path: Path,
        object_root: Path,
        *,
        max_attachment_bytes: int = 50 * 1024 * 1024,
        minimum_free_bytes: int = DEFAULT_MINIMUM_FREE_BYTES,
    ) -> None:
        if minimum_free_bytes < 0:
            raise ValueError("minimum_free_bytes cannot be negative")
        self._database_path = database_path.resolve()
        self._object_root = object_root.resolve()
        self._objects = ContentAddressedStore(
            self._object_root, max_object_bytes=max_attachment_bytes
        )
        self._max_attachment_bytes = max_attachment_bytes
        self._minimum_free_bytes = minimum_free_bytes
        self._capacity_status = AttachmentCapacityStatus(minimum_free_bytes=minimum_free_bytes)
        self._lock = asyncio.Lock()

    @property
    def capacity_status(self) -> AttachmentCapacityStatus:
        """Return the last capacity observation without touching the disk."""

        return self._capacity_status

    async def recheck_capacity(self) -> AttachmentCapacityStatus:
        """Explicitly re-arm attachment intake after an operator frees space."""

        async with self._lock:
            return await asyncio.to_thread(self._recheck_capacity_sync)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS attachments (
                    attachment_id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    original_name TEXT,
                    size INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'quarantined','sanitized','rejected'
                    )),
                    created_at TEXT NOT NULL,
                    UNIQUE(channel, event_id, sha256)
                );
                CREATE INDEX IF NOT EXISTS ix_attachment_scope
                    ON attachments(channel, chat_id, event_id, attachment_id);
                """
            )

    async def ingest(
        self,
        *,
        channel: str,
        chat_id: str,
        event_id: str,
        content: bytes,
        media_type: str,
        original_name: str | None = None,
    ) -> StoredAttachment:
        if not channel or not chat_id or not event_id:
            raise ValueError("attachment scope is required")
        if not _MEDIA_TYPE.fullmatch(media_type.lower()):
            raise ValueError("invalid attachment media type")
        if not content or len(content) > self._max_attachment_bytes:
            raise ValueError("attachment is empty or exceeds the configured limit")
        safe_name = None
        if original_name:
            safe_name = PurePath(original_name.replace("\\", "/")).name[:255] or None
        async with self._lock:
            stored = await asyncio.to_thread(self._put_with_capacity_sync, content)
            record = StoredAttachment(
                channel=channel,
                chat_id=chat_id,
                event_id=event_id,
                sha256=stored.sha256,
                media_type=media_type.lower(),
                original_name=safe_name,
                size=stored.size,
            )
            return await asyncio.to_thread(self._insert_sync, record)

    def _put_with_capacity_sync(self, content: bytes) -> StoredObject:
        if self._capacity_status.paused:
            raise AttachmentCapacityError(self._capacity_status)
        digest = hashlib.sha256(content).hexdigest()
        destination = self._objects.path_for(digest)
        required = 0 if destination.is_file() else len(content)
        try:
            usage = shutil.disk_usage(self._object_root)
        except OSError as exc:
            self._latch_capacity_pause(
                free_bytes=None,
                required_object_bytes=required,
                reason="disk_usage_unavailable",
            )
            raise AttachmentCapacityError(self._capacity_status) from exc
        now = datetime.now(UTC)
        if usage.free < self._minimum_free_bytes + required:
            self._latch_capacity_pause(
                free_bytes=usage.free,
                required_object_bytes=required,
                reason="insufficient_free_space",
                at=now,
            )
            raise AttachmentCapacityError(self._capacity_status)
        self._capacity_status = AttachmentCapacityStatus(
            paused=False,
            minimum_free_bytes=self._minimum_free_bytes,
            last_free_bytes=usage.free,
            required_object_bytes=required,
            checked_at=now,
        )
        try:
            return self._objects.put_bytes(content)
        except OSError as exc:
            quota = getattr(errno, "EDQUOT", 122)
            if exc.errno not in {errno.ENOSPC, quota}:
                raise
            self._latch_capacity_pause(
                free_bytes=usage.free,
                required_object_bytes=required,
                reason="object_write_no_space",
            )
            raise AttachmentCapacityError(self._capacity_status) from exc

    def _recheck_capacity_sync(self) -> AttachmentCapacityStatus:
        try:
            free = shutil.disk_usage(self._object_root).free
        except OSError as exc:
            self._latch_capacity_pause(
                free_bytes=None,
                required_object_bytes=0,
                reason="disk_usage_unavailable",
            )
            raise AttachmentCapacityError(self._capacity_status) from exc
        now = datetime.now(UTC)
        if free < self._minimum_free_bytes:
            self._latch_capacity_pause(
                free_bytes=free,
                required_object_bytes=0,
                reason="insufficient_free_space",
                at=now,
            )
        else:
            self._capacity_status = AttachmentCapacityStatus(
                paused=False,
                minimum_free_bytes=self._minimum_free_bytes,
                last_free_bytes=free,
                required_object_bytes=0,
                checked_at=now,
            )
        return self._capacity_status

    def _latch_capacity_pause(
        self,
        *,
        free_bytes: int | None,
        required_object_bytes: int,
        reason: str,
        at: datetime | None = None,
    ) -> None:
        now = at or datetime.now(UTC)
        self._capacity_status = AttachmentCapacityStatus(
            paused=True,
            minimum_free_bytes=self._minimum_free_bytes,
            last_free_bytes=free_bytes,
            required_object_bytes=required_object_bytes,
            reason=reason,
            checked_at=now,
            triggered_at=self._capacity_status.triggered_at or now,
        )

    def _insert_sync(self, record: StoredAttachment) -> StoredAttachment:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO attachments(
                    attachment_id, channel, chat_id, event_id, sha256, media_type,
                    original_name, size, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(record.attachment_id),
                    record.channel,
                    record.chat_id,
                    record.event_id,
                    record.sha256,
                    record.media_type,
                    record.original_name,
                    record.size,
                    record.status,
                    record.created_at.isoformat(),
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM attachments
                WHERE channel=? AND event_id=? AND sha256=?
                """,
                (record.channel, record.event_id, record.sha256),
            ).fetchone()
            connection.commit()
        if row is None:
            raise RuntimeError("attachment insert failed")
        return self._decode(row)

    async def read_bound(
        self,
        attachment_id: UUID,
        *,
        channel: str,
        chat_id: str,
        event_id: str,
    ) -> tuple[StoredAttachment, bytes]:
        record, path = await self.resolve_bound(
            attachment_id,
            channel=channel,
            chat_id=chat_id,
            event_id=event_id,
        )
        content = await asyncio.to_thread(path.read_bytes)
        if len(content) != record.size or hashlib.sha256(content).hexdigest() != record.sha256:
            raise RuntimeError("attachment object failed integrity verification")
        return record, content

    async def resolve_bound(
        self,
        attachment_id: UUID,
        *,
        channel: str,
        chat_id: str,
        event_id: str,
    ) -> tuple[StoredAttachment, Path]:
        """Resolve one exact conversation attachment without copying it into core memory.

        Isolated decoders receive this content-addressed path plus the expected digest and
        size.  They must repeat every filesystem and content check because this lookup and
        the worker read are separated by a process boundary and a possible TOCTOU window.
        """

        record = await asyncio.to_thread(self._get_sync, attachment_id)
        if record is None:
            raise KeyError(str(attachment_id))
        if (record.channel, record.chat_id, record.event_id) != (channel, chat_id, event_id):
            raise AttachmentScopeError("attachment belongs to another conversation event")
        path = self._objects_path(record.sha256)
        size = await asyncio.to_thread(lambda: path.stat().st_size)
        if size != record.size or size > self._max_attachment_bytes:
            raise RuntimeError("attachment object failed size verification")
        return record, path

    def _objects_path(self, digest: str) -> Path:
        return self._objects.path_for(digest)

    def _get_sync(self, attachment_id: UUID) -> StoredAttachment | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM attachments WHERE attachment_id=?", (str(attachment_id),)
            ).fetchone()
        return None if row is None else self._decode(row)

    @staticmethod
    def _decode(row: sqlite3.Row) -> StoredAttachment:
        return StoredAttachment(
            attachment_id=UUID(row["attachment_id"]),
            channel=row["channel"],
            chat_id=row["chat_id"],
            event_id=row["event_id"],
            sha256=row["sha256"],
            media_type=row["media_type"],
            original_name=row["original_name"],
            size=row["size"],
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )
