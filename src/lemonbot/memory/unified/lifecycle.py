"""Offline lifecycle helpers. Callers hold the profile runtime lock and DB transaction."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from lemonbot.config.paths import RuntimePaths

from .archive import scope_key
from .models import ConversationScope, MemoryEntry, MessagePart

SCOPED = (
    "unified_memories",
    "unified_supersessions",
    "unified_receipts",
    "unified_content_cache",
    "unified_topics",
)


def index_files(paths: RuntimePaths) -> tuple[Path, ...]:
    """Only the canonical derived sidecar; refuse aliases before destructive operations."""
    parent = paths.root / "context-index" / paths.profile
    if any(p.is_symlink() for p in (paths.root, parent.parent, parent)):
        raise ValueError("ContextIndexUnsafe")
    candidates = tuple(
        parent / ("index.sqlite3" + suffix) for suffix in ("", "-wal", "-shm", "-journal")
    )
    for path in candidates:
        if path.is_symlink() or (
            path.exists() and (not path.is_file() or path.stat().st_nlink != 1)
        ):
            raise ValueError("ContextIndexUnsafe")
    return candidates


def invalidate_index(paths: RuntimePaths) -> None:
    # All profile-derived units/vectors can be rebuilt; no original data is stored here.
    for path in reversed(index_files(paths)):
        path.unlink(missing_ok=True)


def archive_scopes(db: sqlite3.Connection, channel: str, chat_id: str) -> tuple[str, ...]:
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "context_archive_messages" not in tables:
        return ()
    keys = set()
    for key, raw in db.execute(
        "SELECT DISTINCT scope_key,scope_json FROM context_archive_messages"
    ):
        scope = ConversationScope.model_validate_json(raw)
        if scope_key(scope) != key:
            raise ValueError("ArchiveScopeConflict")
        if scope.channel == channel and scope.conversation_id == chat_id:
            keys.add(key)
    # Derived rows may outlive the last raw message after a manual maintenance error.
    if "unified_memories" in tables:
        for key, raw in db.execute("SELECT scope,payload FROM unified_memories"):
            scope = MemoryEntry.model_validate_json(raw).scope
            if scope_key(scope) != key:
                raise ValueError("MemoryScopeMismatch")
            if scope.channel == channel and scope.conversation_id == chat_id:
                keys.add(key)
    return tuple(sorted(keys))


def attachment_refs(db: sqlite3.Connection, scopes: tuple[str, ...]) -> tuple[set[str], set[str]]:
    owned: set[str] = set()
    others: set[str] = set()
    for key, raw in db.execute(
        "SELECT m.scope_key,p.payload_json FROM context_archive_parts p "
        "JOIN context_archive_messages m ON m.id=p.message_pk"
    ):
        attachment = MessagePart.model_validate_json(raw).attachment
        if attachment is not None:
            (owned if key in scopes else others).add(attachment.sha256)
    return owned, others


def delete_rows(
    db: sqlite3.Connection,
    *,
    channel: str,
    chat_id: str,
    operation_id: str,
) -> tuple[dict[str, int], dict[str, int]]:
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "context_archive_messages" not in tables:
        return {}, {}
    keys = archive_scopes(db, channel, chat_id)
    counts: dict[str, int] = {}
    redacted: dict[str, int] = {}
    if "unified_delivery_proofs" in tables:
        cursor = db.execute(
            "DELETE FROM unified_delivery_proofs WHERE outbox_id IN "
            "(SELECT message_id FROM outbox WHERE channel=? AND chat_id=?)",
            (channel, chat_id),
        )
        counts["unified_delivery_proofs"] = cursor.rowcount
    for key in keys:
        for table in SCOPED:
            if table in tables:
                cursor = db.execute(f"DELETE FROM {table} WHERE scope=?", (key,))  # noqa: S608
                counts[table] = counts.get(table, 0) + cursor.rowcount
        if "unified_jobs" in tables:
            # Keep money/day and terminal status: erasure must not reset the global daily cap.
            cursor = db.execute(
                "UPDATE unified_jobs SET scope=?,sources='[]' WHERE scope=?",
                ("deleted/" + operation_id, key),
            )
            redacted["unified_jobs"] = redacted.get("unified_jobs", 0) + cursor.rowcount
        # Count children explicitly before their foreign-key cascades.
        for table in (
            "context_archive_observations",
            "context_archive_references",
            "context_archive_parts",
        ):
            cursor = db.execute(
                f"DELETE FROM {table} WHERE message_pk IN "  # noqa: S608
                "(SELECT id FROM context_archive_messages WHERE scope_key=?)",
                (key,),
            )
            counts[table] = counts.get(table, 0) + cursor.rowcount
        cursor = db.execute("DELETE FROM context_archive_messages WHERE scope_key=?", (key,))
        counts["context_archive_messages"] = (
            counts.get("context_archive_messages", 0) + cursor.rowcount
        )
    return counts, redacted
