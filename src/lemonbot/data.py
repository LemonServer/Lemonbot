"""Offline administrator-only profile export and explicit conversation deletion."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from lemonbot.backup import create_backup
from lemonbot.config.paths import RuntimePaths
from lemonbot.runtime_lock import RuntimeLock

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SCOPED_TABLES = (
    "inbox",
    "outbox",
    "drafts",
    "approvals",
    "tool_executions",
    "messages",
    "memory_records",
    "attachments",
    "proactive_jobs",
    "allowlist",
    "audit_log",
)


class DataOperationError(RuntimeError):
    pass


class ConversationNotFoundError(DataOperationError):
    pass


@dataclass(frozen=True, slots=True)
class ConversationDeletionResult:
    operation_id: str
    rows_deleted: dict[str, int]
    objects_removed: int
    object_cleanup_failures: int

    @property
    def total_rows(self) -> int:
        return sum(self.rows_deleted.values())


def export_profile_data(paths: RuntimePaths, output: Path | None = None) -> Path:
    """Create an offline, consistent export in Lemonbot backup format v1.

    The archive contains only the current profile database and current profile
    object store. Secret Service, configuration, logs and process state are
    deliberately outside this format.
    """

    if not paths.database.is_file():
        raise DataOperationError("profile database does not exist")
    _validate_offline_paths(paths, include_objects=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = output or (
        paths.backups / f"lemonbot-data-export-{paths.profile}-{timestamp}.zip"
    )
    destination = destination.expanduser().resolve()
    if destination.suffix.casefold() != ".zip":
        raise DataOperationError("data export must use a .zip destination")
    _validate_export_destination(paths, destination)
    with RuntimeLock(paths.lock_file):
        _validate_export_object_store(paths)
        return create_backup(paths, destination)


def delete_conversation(
    paths: RuntimePaths,
    *,
    channel: str,
    chat_id: str,
) -> ConversationDeletionResult:
    """Permanently delete one exact conversation from an offline profile."""

    _validate_scope(channel, chat_id)
    if not paths.database.is_file():
        raise DataOperationError("profile database does not exist")
    _validate_offline_paths(paths, include_objects=False)
    with RuntimeLock(paths.lock_file):
        return _delete_conversation_locked(paths, channel=channel, chat_id=chat_id)


def _delete_conversation_locked(
    paths: RuntimePaths,
    *,
    channel: str,
    chat_id: str,
) -> ConversationDeletionResult:
    operation_id = str(uuid4())
    database = paths.database.resolve(strict=True)
    with closing(sqlite3.connect(database, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        secure_delete = connection.execute("PRAGMA secure_delete=ON").fetchone()
        if secure_delete is None or int(secure_delete[0]) != 1:
            raise DataOperationError("SQLite secure_delete could not be enabled")
        existing = _table_names(connection)
        if "audit_log" not in existing:
            raise DataOperationError("database schema has no audit_log table")
        unsupported = _unsupported_scoped_tables(connection, existing)
        if unsupported:
            joined = ", ".join(sorted(unsupported))
            raise DataOperationError(f"unsupported conversation-scoped tables: {joined}")
        _checkpoint_truncate(connection)

        connection.execute("BEGIN IMMEDIATE")
        try:
            attachment_hashes = _attachment_hashes(
                connection,
                existing,
                channel=channel,
                chat_id=chat_id,
            )
            removable_hashes = _unshared_attachment_hashes(
                connection,
                existing,
                attachment_hashes,
                channel=channel,
                chat_id=chat_id,
            )
            object_paths = _preflight_object_paths(paths, removable_hashes)
            counts: dict[str, int] = {}
            for table in _SCOPED_TABLES:
                if table not in existing:
                    continue
                count = _scope_count(connection, table, channel=channel, chat_id=chat_id)
                counts[table] = count
                if count:
                    connection.execute(
                        f"DELETE FROM {_quote(table)} WHERE channel=? AND chat_id=?",  # noqa: S608
                        (channel, chat_id),
                    )
            if sum(counts.values()) == 0:
                raise ConversationNotFoundError("conversation has no persisted records")

            _rebuild_fts(connection, existing)
            summary = {
                "operation_id": operation_id,
                "rows_deleted": counts,
                "object_candidates": len(object_paths),
            }
            audit_cursor = connection.execute(
                """
                INSERT INTO audit_log(
                    action, outcome, channel, chat_id, event_id, message_id,
                    rule_id, detail_json, occurred_at
                ) VALUES (?, ?, ?, NULL, NULL, NULL, ?, ?, ?)
                """,
                (
                    "data.delete_conversation",
                    "database_committed",
                    channel,
                    "administrator_explicit_delete",
                    json.dumps(summary, sort_keys=True, separators=(",", ":")),
                    datetime.now(UTC).isoformat(),
                ),
            )
            audit_id = audit_cursor.lastrowid
            if audit_id is None:
                raise DataOperationError("deletion audit summary could not be created")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

        removed, failures = _remove_unreferenced_objects(object_paths)
        summary.update(
            {
                "objects_removed": removed,
                "object_cleanup_failures": failures,
            }
        )
        connection.execute(
            "UPDATE audit_log SET outcome=?, detail_json=? WHERE id=?",
            (
                "completed" if failures == 0 else "partial",
                json.dumps(summary, sort_keys=True, separators=(",", ":")),
                audit_id,
            ),
        )
        connection.commit()
        _checkpoint_truncate(connection)
        connection.execute("VACUUM")
        _checkpoint_truncate(connection)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]) != "ok":
            raise DataOperationError("database integrity_check failed after deletion")

    return ConversationDeletionResult(
        operation_id=operation_id,
        rows_deleted=counts,
        objects_removed=removed,
        object_cleanup_failures=failures,
    )


def _validate_export_destination(paths: RuntimePaths, destination: Path) -> None:
    objects = paths.objects.expanduser().resolve()
    quarantine = paths.quarantine.expanduser().resolve()
    for forbidden in (objects, quarantine):
        try:
            destination.relative_to(forbidden)
        except ValueError:
            continue
        raise DataOperationError("export destination cannot be inside an object store")
    if destination in {paths.database.resolve(), paths.lock_file.resolve()}:
        raise DataOperationError("export destination conflicts with runtime state")


def _validate_scope(channel: str, chat_id: str) -> None:
    if not channel or channel != channel.strip() or len(channel) > 64 or "\0" in channel:
        raise DataOperationError("channel must be an exact non-empty stable identifier")
    if not chat_id or chat_id != chat_id.strip() or len(chat_id) > 512 or "\0" in chat_id:
        raise DataOperationError("chat_id must be an exact non-empty stable identifier")


def _validate_offline_paths(paths: RuntimePaths, *, include_objects: bool) -> None:
    if paths.database.is_symlink():
        raise DataOperationError("refusing to use a database symbolic link")
    if paths.lock_file.is_symlink():
        raise DataOperationError("refusing to use a runtime-lock symbolic link")
    _resolved_under(paths.database, paths.root)
    _resolved_under(paths.lock_file, paths.root)
    if include_objects and paths.objects.exists():
        if paths.objects.is_symlink():
            raise DataOperationError("refusing to export a symbolic-link object store")
        _resolved_under(paths.objects, paths.root)


def _validate_export_object_store(paths: RuntimePaths) -> None:
    if not paths.objects.exists():
        return
    root = _resolved_under(paths.objects, paths.root)
    for shard in root.iterdir():
        if (
            shard.is_symlink()
            or not shard.is_dir()
            or len(shard.name) != 2
            or any(character not in "0123456789abcdef" for character in shard.name)
        ):
            raise DataOperationError("object store contains an unexpected entry")
        for item in shard.iterdir():
            if (
                item.is_symlink()
                or not item.is_file()
                or not _DIGEST.fullmatch(item.name)
                or item.name[:2] != shard.name
            ):
                raise DataOperationError("object store contains an invalid content object")
            digest = hashlib.sha256()
            with item.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != item.name:
                raise DataOperationError("object store content digest verification failed")


def _resolved_under(path: Path, root: Path) -> Path:
    resolved_root = root.expanduser().resolve()
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise DataOperationError("data path escapes the configured profile root") from exc
    return resolved


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({_quote(table)})")}


def _unsupported_scoped_tables(
    connection: sqlite3.Connection,
    existing: set[str],
) -> set[str]:
    known = set(_SCOPED_TABLES)
    return {
        table
        for table in existing - known
        if {"channel", "chat_id"}.issubset(_columns(connection, table))
    }


def _scope_count(
    connection: sqlite3.Connection,
    table: str,
    *,
    channel: str,
    chat_id: str,
) -> int:
    row = connection.execute(
        f"SELECT count(*) FROM {_quote(table)} WHERE channel=? AND chat_id=?",  # noqa: S608
        (channel, chat_id),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _attachment_hashes(
    connection: sqlite3.Connection,
    existing: set[str],
    *,
    channel: str,
    chat_id: str,
) -> set[str]:
    if "attachments" not in existing:
        return set()
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT sha256 FROM attachments WHERE channel=? AND chat_id=?",
            (channel, chat_id),
        )
    }


def _unshared_attachment_hashes(
    connection: sqlite3.Connection,
    existing: set[str],
    hashes: set[str],
    *,
    channel: str,
    chat_id: str,
) -> set[str]:
    if "attachments" not in existing:
        return set()
    removable: set[str] = set()
    for digest in hashes:
        other = connection.execute(
            """
            SELECT 1 FROM attachments
            WHERE sha256=? AND NOT (channel=? AND chat_id=?) LIMIT 1
            """,
            (digest, channel, chat_id),
        ).fetchone()
        if other is None:
            removable.add(digest)
    return removable


def _preflight_object_paths(paths: RuntimePaths, hashes: set[str]) -> tuple[Path, ...]:
    if not hashes or not paths.objects.exists():
        return ()
    if paths.objects.is_symlink():
        raise DataOperationError("refusing to clean a symbolic-link object store")
    root = _resolved_under(paths.objects, paths.root)
    candidates: list[Path] = []
    for digest in sorted(hashes):
        if not _DIGEST.fullmatch(digest):
            raise DataOperationError("attachment metadata contains an invalid object digest")
        shard = root / digest[:2]
        candidate = shard / digest
        if shard.is_symlink() or candidate.is_symlink():
            raise DataOperationError("refusing to clean a symbolic-link object path")
        _resolved_under(candidate, root)
        if candidate.exists():
            if not candidate.is_file():
                raise DataOperationError("attachment object path is not a regular file")
            candidates.append(candidate)
    return tuple(candidates)


def _remove_unreferenced_objects(paths: tuple[Path, ...]) -> tuple[int, int]:
    removed = 0
    failures = 0
    for path in paths:
        try:
            path.unlink()
            removed += 1
            try:
                path.parent.rmdir()
            except OSError:
                pass
        except OSError:
            failures += 1
    return removed, failures


def _rebuild_fts(connection: sqlite3.Connection, existing: set[str]) -> None:
    for table in ("messages_fts", "memory_fts"):
        if table in existing:
            connection.execute(f"INSERT INTO {_quote(table)}({_quote(table)}) VALUES('rebuild')")


def _checkpoint_truncate(connection: sqlite3.Connection) -> None:
    result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if result is None or int(result[0]) != 0:
        raise DataOperationError("SQLite WAL is busy; close every database reader and retry")
