"""Async SQLite engine setup with safety and search pragmas."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base


class Database:
    def __init__(self, url: str, *, echo: bool = False) -> None:
        if not url.startswith("sqlite+aiosqlite:///"):
            raise ValueError("Lemonbot core currently requires sqlite+aiosqlite")
        self.url = url
        self.engine: AsyncEngine = create_async_engine(url, echo=echo)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        self._install_pragmas()

    @classmethod
    def from_path(cls, path: str | Path, *, echo: bool = False) -> Database:
        path = Path(path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(f"sqlite+aiosqlite:///{path.as_posix()}", echo=echo)

    @classmethod
    def in_memory(cls, *, echo: bool = False) -> Database:
        return cls("sqlite+aiosqlite:///:memory:", echo=echo)

    def _install_pragmas(self) -> None:
        @event.listens_for(self.engine.sync_engine, "connect")
        def set_pragmas(dbapi_connection: object, _connection_record: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            # Commit-boundary rows (outbox dispatching, approvals executing,
            # model/tool call markers) must survive a VM or host power loss
            # before the corresponding external operation begins. Throughput
            # is secondary to at-most-once side-effect semantics here.
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    async def initialise(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            # External-content FTS preserves the canonical message row while
            # keeping search data transactionally in sync through triggers.
            try:
                await connection.execute(
                    text(
                        "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5("
                        "content, content='messages', content_rowid='id', tokenize='trigram')"
                    )
                )
            except Exception:
                # Older system SQLite builds may lack the trigram tokenizer.
                await connection.execute(
                    text(
                        "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5("
                        "content, content='messages', content_rowid='id', tokenize='unicode61')"
                    )
                )
            await connection.execute(
                text(
                    "CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN "
                    "INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content); END"
                )
            )
            await connection.execute(
                text(
                    "CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN "
                    "INSERT INTO messages_fts(messages_fts, rowid, content) "
                    "VALUES('delete', old.id, old.content); END"
                )
            )
            await connection.execute(
                text(
                    "CREATE TRIGGER IF NOT EXISTS messages_fts_au "
                    "AFTER UPDATE OF content ON messages BEGIN "
                    "INSERT INTO messages_fts(messages_fts, rowid, content) "
                    "VALUES('delete', old.id, old.content); "
                    "INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content); END"
                )
            )

    async def integrity_check(self) -> bool:
        async with self.engine.connect() as connection:
            value = await connection.scalar(text("PRAGMA quick_check"))
        return str(value) == "ok"

    async def close(self) -> None:
        await self.engine.dispose()
