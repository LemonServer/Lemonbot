"""Durable SQLite/FTS5 implementation of the scoped memory boundary."""

# ruff: noqa: S608

from __future__ import annotations

import asyncio
import re
import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from .models import MemoryKind, MemoryRecord, SearchHit
from .store import InMemoryMemoryStore, MemoryScopeError, _terms

_FTS_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]{3,}|[\u3400-\u4dbf\u4e00-\u9fff]{3,}")


class SQLiteMemoryStore:
    """Permanent memory storage suitable for either ``prod.db`` or ``lab.db``.

    Every query requires both channel and chat ID.  The class opens short-lived
    connections so it can safely share the profile database with the core
    SQLAlchemy pool; writes use ``BEGIN IMMEDIATE`` plus SQLite's busy timeout.
    """

    def __init__(self, database_path: Path, *, busy_timeout_ms: int = 5_000) -> None:
        self._path = Path(database_path)
        self._busy_timeout_ms = busy_timeout_ms
        self._initialized = False
        self._initialize_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._path,
            timeout=self._busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_records (
                    memory_id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    active INTEGER NOT NULL CHECK (active IN (0, 1)),
                    importance REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    record_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_memory_scope
                    ON memory_records(channel, chat_id, active, kind, created_at);
                """
            )
            fts_existed = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memory_fts'"
            ).fetchone()
            try:
                connection.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts "
                    "USING fts5(text, content='memory_records', content_rowid='rowid', "
                    "tokenize='trigram')"
                )
            except sqlite3.OperationalError:
                # Older development SQLite builds may lack trigram; unicode61
                # keeps persistence usable, while deployed Linux builds use
                # the required trigram tokenizer.
                connection.execute("DROP TABLE IF EXISTS memory_fts")
                connection.execute(
                    "CREATE VIRTUAL TABLE memory_fts "
                    "USING fts5(text, content='memory_records', content_rowid='rowid', "
                    "tokenize='unicode61')"
                )
            connection.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS memory_records_ai
                AFTER INSERT ON memory_records BEGIN
                    INSERT INTO memory_fts(rowid, text) VALUES (new.rowid, new.text);
                END;
                CREATE TRIGGER IF NOT EXISTS memory_records_ad
                AFTER DELETE ON memory_records BEGIN
                    INSERT INTO memory_fts(memory_fts, rowid, text)
                    VALUES ('delete', old.rowid, old.text);
                END;
                CREATE TRIGGER IF NOT EXISTS memory_records_au
                AFTER UPDATE OF text ON memory_records BEGIN
                    INSERT INTO memory_fts(memory_fts, rowid, text)
                    VALUES ('delete', old.rowid, old.text);
                    INSERT INTO memory_fts(rowid, text) VALUES (new.rowid, new.text);
                END;
                """
            )
            # Bring an existing content table into sync after an interrupted
            # first initialization or schema migration.
            if fts_existed is None:
                connection.execute("INSERT INTO memory_fts(memory_fts) VALUES ('rebuild')")

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._initialize_lock:
            if not self._initialized:
                await asyncio.to_thread(self._initialize_sync)
                self._initialized = True

    @staticmethod
    def _decode(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord.model_validate_json(row["record_json"])

    def _add_sync(self, record: MemoryRecord) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM memory_records WHERE memory_id = ?", (str(record.memory_id),)
            ).fetchone():
                raise ValueError(f"memory {record.memory_id} already exists")
            replacements: list[MemoryRecord] = []
            for superseded_id in record.provenance.supersedes:
                row = connection.execute(
                    "SELECT record_json FROM memory_records WHERE memory_id = ?",
                    (str(superseded_id),),
                ).fetchone()
                if row is None:
                    raise ValueError(f"superseded memory {superseded_id} does not exist")
                old = self._decode(row)
                if (old.channel, old.chat_id) != (record.channel, record.chat_id):
                    raise MemoryScopeError("a memory cannot supersede another conversation")
                if not old.active:
                    raise ValueError(f"superseded memory {superseded_id} is already inactive")
                replacements.append(old)
            for old in replacements:
                inactive = old.model_copy(update={"active": False})
                connection.execute(
                    "UPDATE memory_records SET active = 0, record_json = ? WHERE memory_id = ?",
                    (inactive.model_dump_json(), str(old.memory_id)),
                )
            connection.execute(
                """
                INSERT INTO memory_records(
                    memory_id, channel, chat_id, kind, text, active,
                    importance, created_at, record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(record.memory_id),
                    record.channel,
                    record.chat_id,
                    record.kind.value,
                    record.text,
                    int(record.active),
                    record.importance,
                    record.created_at.astimezone(UTC).isoformat(),
                    record.model_dump_json(),
                ),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def add(self, record: MemoryRecord) -> None:
        await self.initialize()
        async with self._write_lock:
            await asyncio.to_thread(self._add_sync, record)

    def _get_sync(self, memory_id: UUID, channel: str, chat_id: str) -> MemoryRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT record_json FROM memory_records "
                "WHERE memory_id = ? AND channel = ? AND chat_id = ?",
                (str(memory_id), channel, chat_id),
            ).fetchone()
        return None if row is None else self._decode(row)

    async def get(
        self,
        memory_id: UUID,
        *,
        channel: str,
        chat_id: str,
    ) -> MemoryRecord | None:
        if not channel or not chat_id:
            raise MemoryScopeError("channel and chat_id are mandatory")
        await self.initialize()
        return await asyncio.to_thread(self._get_sync, memory_id, channel, chat_id)

    @staticmethod
    def _fts_query(query: str) -> str | None:
        tokens: list[str] = []
        for match in _FTS_TOKEN_RE.finditer(query):
            token = match.group(0).casefold()
            candidates: Iterable[str]
            if all("\u3400" <= character <= "\u9fff" for character in token):
                candidates = (token[index : index + 3] for index in range(len(token) - 2))
            else:
                candidates = (token,)
            for candidate in candidates:
                if candidate not in tokens:
                    tokens.append(candidate)
                # Bound MATCH expression complexity for adversarial chat text.
                if len(tokens) >= 64:
                    break
            if len(tokens) >= 64:
                break
        if not tokens:
            return None
        return " OR ".join(f'"{token}"' for token in tokens)

    def _search_sync(
        self,
        *,
        channel: str,
        chat_id: str,
        query: str,
        kinds: Sequence[MemoryKind] | None,
        limit: int,
    ) -> tuple[SearchHit, ...]:
        clauses = ["records.channel = ?", "records.chat_id = ?", "records.active = 1"]
        parameters: list[object] = [channel, chat_id]
        if kinds is not None:
            placeholders = ",".join("?" for _ in kinds)
            clauses.append(f"records.kind IN ({placeholders})")
            parameters.extend(kind.value for kind in kinds)
        match = self._fts_query(query)
        with closing(self._connect()) as connection:
            if match:
                fts_statement = f"""
                    SELECT records.record_json, bm25(memory_fts) AS fts_rank
                    FROM memory_fts
                    JOIN memory_records AS records ON records.rowid = memory_fts.rowid
                    WHERE memory_fts MATCH ? AND {" AND ".join(clauses)}
                    ORDER BY fts_rank ASC, records.created_at DESC
                    LIMIT ?
                    """
                rows = connection.execute(
                    fts_statement,
                    [match, *parameters, limit],
                ).fetchall()
            else:
                scan_statement = f"""
                    SELECT records.record_json, 0.0 AS fts_rank
                    FROM memory_records AS records
                    WHERE {" AND ".join(clauses)}
                    ORDER BY records.created_at DESC
                    LIMIT ?
                    """
                rows = connection.execute(
                    scan_statement,
                    [*parameters, min(500, max(limit * 10, limit))],
                ).fetchall()
        now = datetime.now(UTC)
        query_terms = _terms(query)
        hits: list[SearchHit] = []
        for row in rows:
            record = self._decode(row)
            baseline = InMemoryMemoryStore._score(record, query_terms, now)
            rank = float(row["fts_rank"])
            hits.append(
                SearchHit(
                    record=record,
                    score=baseline.score + max(0.0, -rank),
                    matched_terms=baseline.matched_terms,
                )
            )
        if not match and query_terms:
            hits = [hit for hit in hits if hit.matched_terms]
        hits.sort(key=lambda hit: (hit.score, hit.record.created_at), reverse=True)
        return tuple(hits[:limit])

    async def search(
        self,
        *,
        channel: str,
        chat_id: str,
        query: str,
        kinds: Sequence[MemoryKind] | None = None,
        limit: int = 10,
    ) -> tuple[SearchHit, ...]:
        if not channel or not chat_id:
            raise MemoryScopeError("channel and chat_id are mandatory")
        if limit < 1 or limit > 100:
            raise ValueError("search limit must be between 1 and 100")
        if kinds is not None and not kinds:
            return ()
        await self.initialize()
        return await asyncio.to_thread(
            self._search_sync,
            channel=channel,
            chat_id=chat_id,
            query=query,
            kinds=kinds,
            limit=limit,
        )
