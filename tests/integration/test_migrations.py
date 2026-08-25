from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from lemonbot.storage.migrate import upgrade_database


def test_initial_migration_creates_all_durable_subsystems(tmp_path: Path) -> None:
    database = tmp_path / "fresh.db"
    upgrade_database(database)
    with closing(sqlite3.connect(database)) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert {
        "inbox",
        "outbox",
        "drafts",
        "approvals",
        "tool_executions",
        "messages",
        "messages_fts",
        "memory_records",
        "memory_fts",
        "model_budget_ledger",
        "audit_log",
        "allowlist",
        "runtime_state",
        "proactive_jobs",
        "attachments",
    }.issubset(tables)
    assert revision == ("20260825_0006",)
