"""Rebuildable private SQLite FTS/vector sidecar; archive is always read-only."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from .archive import canonical, scope_key
from .documents import INDEX_VERSION, IndexUnit, terms
from .models import ConversationScope


class RetrievalError(ValueError):
    """Redacted local retrieval/index failure."""


def checked_vector(raw: Sequence[float]) -> tuple[float, ...]:
    values = tuple(float(value) for value in raw)
    norm = math.sqrt(sum(value * value for value in values))
    if len(values) != 512 or not math.isfinite(norm) or abs(norm - 1) > 1e-4:
        raise RetrievalError("RetrievalVectorInvalid")
    return values


class SQLiteRetrievalIndex:
    def __init__(self, path: Path, archive_path: Path) -> None:
        if path.is_symlink() or path.resolve() == archive_path.resolve():
            raise RetrievalError("RetrievalRequiresSeparateIndex")
        if path.exists() and (path.stat().st_nlink != 1 or path.samefile(archive_path)):
            raise RetrievalError("RetrievalRequiresSeparateIndex")
        self.path = path.absolute()
        self.archive_path = archive_path.resolve(strict=True)
        stat = self.archive_path.stat()
        self.file_identity = (stat.st_dev, stat.st_ino)
        self.identity = hashlib.sha256(
            canonical(
                [
                    str(self.archive_path),
                    stat.st_dev,
                    stat.st_ino,
                    INDEX_VERSION,
                ]
            ).encode()
        ).hexdigest()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        stat = self.archive_path.stat()
        if (stat.st_dev, stat.st_ino) != self.file_identity:
            raise RetrievalError("RetrievalIndexArchiveMismatch")
        if self.path.is_symlink() or (self.path.exists() and self.path.stat().st_nlink != 1):
            raise RetrievalError("RetrievalRequiresSeparateIndex")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        async with aiosqlite.connect(self.path, timeout=5) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("PRAGMA synchronous=FULL")
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute("PRAGMA user_version") as cursor:
                    version_row = await cursor.fetchone()
                    assert version_row is not None
                    version = version_row[0]
                if version == 0:
                    async with db.execute("SELECT name FROM sqlite_master WHERE type='table'") as c:
                        if await c.fetchone() is not None:
                            raise RetrievalError("RetrievalIndexSchemaConflict")
                    await db.execute("CREATE TABLE metadata (identity TEXT NOT NULL)")
                    await db.execute("INSERT INTO metadata VALUES (?)", (self.identity,))
                    await db.execute(
                        "CREATE TABLE units (id INTEGER PRIMARY KEY, scope TEXT NOT NULL, "
                        "unit_id TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL, "
                        "UNIQUE(scope,unit_id))"
                    )
                    await db.execute("CREATE INDEX units_scope ON units(scope)")
                    await db.execute(
                        "CREATE VIRTUAL TABLE lexical USING fts5(partition, tokens, "
                        "tokenize='unicode61')"
                    )
                    await db.execute(
                        "CREATE TABLE vectors (unit_pk INTEGER PRIMARY KEY REFERENCES units(id) "
                        "ON DELETE CASCADE, model_version TEXT NOT NULL, digest TEXT NOT NULL, "
                        "vector TEXT NOT NULL)"
                    )
                    await db.execute("PRAGMA user_version=1")
                elif version != 1:
                    raise RetrievalError("RetrievalIndexVersionUnsupported")
                async with db.execute("SELECT identity FROM metadata") as cursor:
                    rows = list(await cursor.fetchall())
                if len(rows) != 1 or rows[0][0] != self.identity:
                    raise RetrievalError("RetrievalIndexArchiveMismatch")
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def replace_scope(self, scope: ConversationScope, units: Sequence[IndexUnit]) -> int:
        partition = scope_key(scope)
        if any(unit.source.scope != scope for unit in units):
            raise RetrievalError("RetrievalScopeMismatch")
        if len(units) > 50000 or len({unit.unit_id for unit in units}) != len(units):
            raise RetrievalError("RetrievalIndexBounds")
        wanted = {unit.unit_id for unit in units}
        changed = 0
        async with self.connection() as db:
            async with db.execute(
                "SELECT id,unit_id,digest FROM units WHERE scope=?", (partition,)
            ) as c:
                old = {row["unit_id"]: row for row in await c.fetchall()}
            for identifier, row in old.items():
                if identifier not in wanted:
                    await db.execute("DELETE FROM lexical WHERE rowid=?", (row["id"],))
                    await db.execute("DELETE FROM units WHERE id=?", (row["id"],))
                    changed += 1
            for unit in units:
                previous = old.get(unit.unit_id)
                if previous is not None and previous["digest"] == unit.digest:
                    continue
                if previous is not None:
                    await db.execute("DELETE FROM lexical WHERE rowid=?", (previous["id"],))
                    await db.execute("DELETE FROM units WHERE id=?", (previous["id"],))
                cursor = await db.execute(
                    "INSERT INTO units(scope,unit_id,digest,payload) VALUES (?,?,?,?)",
                    (partition, unit.unit_id, unit.digest, unit.model_dump_json()),
                )
                rowid = cursor.lastrowid
                await cursor.close()
                if unit.searchable:
                    await db.execute(
                        "INSERT INTO lexical(rowid,partition,tokens) VALUES (?,?,?)",
                        (rowid, partition, " ".join(terms(unit.text))),
                    )
                changed += 1
        return changed

    async def units(self, scope: ConversationScope) -> tuple[IndexUnit, ...]:
        async with self.connection() as db:
            async with db.execute(
                "SELECT payload,digest FROM units WHERE scope=? ORDER BY unit_id",
                (scope_key(scope),),
            ) as cursor:
                rows = await cursor.fetchall()
        result = []
        for row in rows:
            unit = IndexUnit.model_validate_json(row["payload"])
            if unit.source.scope != scope or unit.digest != row["digest"]:
                raise RetrievalError("RetrievalIndexCorrupt")
            result.append(unit)
        return tuple(result)

    async def keywords(self, scope: ConversationScope, query: str) -> set[str]:
        selected = terms(query)[:64]
        if not selected:
            return set()
        partition = scope_key(scope)
        expression = (
            'partition:"'
            + partition
            + '" AND tokens:('
            + " OR ".join('"' + term.replace('"', '""') + '"' for term in selected)
            + ")"
        )
        async with self.connection() as db:
            # Partition participates in MATCH itself; no global BM25 statistics.
            async with db.execute(
                "SELECT units.unit_id FROM units JOIN lexical ON units.id=lexical.rowid "
                "WHERE units.scope=? AND lexical MATCH ?",
                (partition, expression),
            ) as cursor:
                return {row[0] for row in await cursor.fetchall()}

    async def vectors(self, scope: ConversationScope, version: str) -> dict[str, tuple[float, ...]]:
        async with self.connection() as db:
            async with db.execute(
                "SELECT units.unit_id,units.digest,vectors.digest,vector FROM units "
                "JOIN vectors ON units.id=vectors.unit_pk WHERE units.scope=? "
                "AND vectors.model_version=?",
                (scope_key(scope), version),
            ) as cursor:
                rows = await cursor.fetchall()
        result = {}
        for row in rows:
            if row[1] != row[2]:
                raise RetrievalError("RetrievalIndexCorrupt")
            try:
                result[row[0]] = checked_vector(json.loads(row[3]))
            except (ValueError, TypeError, OverflowError):
                raise RetrievalError("RetrievalVectorInvalid") from None
        return result

    async def put_vector(self, unit: IndexUnit, version: str, vector: Sequence[float]) -> None:
        checked = checked_vector(vector)
        async with self.connection() as db:
            async with db.execute(
                "SELECT id,digest FROM units WHERE scope=? AND unit_id=?",
                (scope_key(unit.source.scope), unit.unit_id),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None or row[1] != unit.digest:
                raise RetrievalError("RetrievalIndexChanged")
            await db.execute(
                "INSERT OR REPLACE INTO vectors VALUES (?,?,?,?)",
                (row[0], version, unit.digest, json.dumps(checked, allow_nan=False)),
            )
