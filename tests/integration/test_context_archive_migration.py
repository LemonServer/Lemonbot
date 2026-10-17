from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from lemonbot.config.paths import RuntimePaths
from lemonbot.connectors.wechat_agent import CHANNEL, Binding, reference
from lemonbot.connectors.wechat_agent_conversation import ConversationTarget
from lemonbot.connectors.wechat_agent_state import private_write, save_binding
from lemonbot.domain.models import (
    DeliveryReceipt,
    DeliveryStatus,
    InboundEvent,
    OutboundMessage,
)
from lemonbot.memory.unified.archive import ArchiveError, SQLiteMessageArchive
from lemonbot.memory.unified.cli import execute
from lemonbot.memory.unified.migration import migrate_legacy_copy
from lemonbot.memory.unified.models import SourceRef
from lemonbot.memory.unified.wechat import archive_scope
from lemonbot.storage import CoreRepository, Database
from lemonbot.storage.migrate import upgrade_database

KEY = b"s" * 32
ACCOUNT = reference(KEY, "account", "synthetic-account")
CHAT = "synthetic-group@chatroom"
TARGET = ConversationTarget(
    Binding(ACCOUNT, CHAT, reference(KEY, "chat", ACCOUNT, CHAT), (1,), ("label",)),
    "synthetic-group",
    True,
)
NOW = datetime(2026, 10, 15, tzinfo=UTC)


def inbound(identifier: str, *, sender: str = "alice", **metadata: Any) -> InboundEvent:
    sender_ref = reference(KEY, "sender", ACCOUNT, sender)
    return InboundEvent(
        channel=CHANNEL,
        chat_id=TARGET.binding.chat_ref,
        sender_id=sender_ref,
        event_id=identifier,
        text="same synthetic text",
        occurred_at=NOW,
        metadata={
            "wechat_mode": "conversation",
            "is_group": True,
            "context_scope": reference(KEY, "context", TARGET.binding.chat_ref, sender_ref),
            **metadata,
        },
    )


async def create_legacy(path: Path) -> dict[str, str]:
    upgrade_database(path)
    database = Database.from_path(path)
    repository = CoreRepository(database)
    ids = {}
    try:
        for identifier, sender, status in (
            ("event-ack", "alice", DeliveryStatus.ACKNOWLEDGED),
            ("event-unknown", "bob", DeliveryStatus.UNKNOWN),
        ):
            await repository.record_inbound(inbound(identifier, sender=sender))
            item = await repository.create_outbox(
                OutboundMessage(
                    channel=CHANNEL,
                    chat_id=TARGET.binding.chat_ref,
                    reply_to_event_id=identifier,
                    text="synthetic reply " + sender,
                )
            )
            assert await repository.reserve_next_outbox(CHANNEL) is not None
            assert await repository.mark_dispatching(item.id)
            receipt_id = "receipt-" + identifier
            await repository.apply_receipt(
                item.id,
                DeliveryReceipt(
                    message_id=item.message.message_id,
                    status=status,
                    external_id=receipt_id,
                    acknowledged_at=NOW if status is DeliveryStatus.ACKNOWLEDGED else None,
                ),
            )
            ids[identifier] = receipt_id
        await repository.set_paused(channel=CHANNEL, paused=True)
    finally:
        await database.close()
    return ids


def legacy_snapshot(path: Path) -> dict[str, list[tuple[Any, ...]]]:
    with sqlite3.connect(path) as db:
        return {
            table: db.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall()  # noqa: S608
            for table in ("messages", "inbox", "outbox", "runtime_state", "audit_log")
        }


async def test_legacy_import_is_idempotent_attributed_and_preserves_unknown_pause(
    tmp_path: Path,
) -> None:
    path = tmp_path / "copy.db"
    receipts = await create_legacy(path)
    archive = SQLiteMessageArchive(path)
    before = legacy_snapshot(path)
    first = await migrate_legacy_copy(archive, targets=(TARGET,), key=KEY)
    assert first.inserted == 3 and first.excluded == {"unconfirmed_assistant": 1}
    second = await migrate_legacy_copy(archive, targets=(TARGET,), key=KEY)
    assert second.inserted == 0 and second.existing == 3
    assert second.excluded == {"unconfirmed_assistant": 1}
    assert legacy_snapshot(path) == before
    scope = archive_scope(TARGET.binding, is_group=True)
    reopened = SQLiteMessageArchive(path)
    messages = await reopened.list_messages(scope=scope, before_message_id=None, limit=20)
    assert len(messages) == 3
    authors = {m.sender_id for m in messages if m.direction == "inbound"}
    assert len(authors) == 2
    assert (
        await reopened.get_message(SourceRef(scope=scope, message_id=receipts["event-unknown"]))
        is None
    )
    assistant = await reopened.get_message(SourceRef(scope=scope, message_id=receipts["event-ack"]))
    assert assistant is not None and assistant.sender_id == ACCOUNT
    assert all(m.transport_message_id is None for m in messages)
    assert all(
        m.parts[-1].unavailable_reason == "legacy_original_structure_unavailable"
        for m in messages
        if m.direction == "inbound"
    )


async def test_legacy_unknown_account_or_sender_scope_is_excluded(tmp_path: Path) -> None:
    path = tmp_path / "copy.db"
    await create_legacy(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE messages SET metadata_json='{}' WHERE external_id='event-ack'")
    before = legacy_snapshot(path)
    report = await migrate_legacy_copy(SQLiteMessageArchive(path), targets=(TARGET,), key=KEY)
    assert report.inserted == 1
    assert report.excluded == {
        "unverified_inbound": 1,
        "assistant_source_unverified": 1,
        "unconfirmed_assistant": 1,
    }
    assert legacy_snapshot(path) == before
    wrong_account = ConversationTarget(
        Binding("different-account", CHAT, "different-chat-ref", (1,), ("label",)),
        "synthetic-group",
        True,
    )
    report = await migrate_legacy_copy(
        SQLiteMessageArchive(path), targets=(wrong_account,), key=KEY
    )
    assert report.inserted == 0 and report.excluded == {"unbound_scope": 4}


async def test_copy_cli_requires_confirmation_and_uses_verified_private_bindings(
    tmp_path: Path,
) -> None:
    paths = RuntimePaths(root=tmp_path / "runtime", profile="lab")
    paths.ensure()
    path = paths.backups / "stage2-copy.db"
    await create_legacy(path)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    private_write(state / "comparison-key", KEY)
    save_binding(state / "binding.json", TARGET.binding)
    private_write(
        state / "conversation-binding.json",
        json.dumps(
            {
                "operator_confirmed": True,
                "account_ref": ACCOUNT,
                "targets": [
                    {"binding": asdict(TARGET.binding), "name": TARGET.name, "is_group": True}
                ],
            }
        ).encode(),
    )
    config = tmp_path / "config.toml"
    config.write_text(
        'profile="lab"\n[runtime]\ndata_root='
        + json.dumps(str(paths.root))
        + "\n[wechat_agent]\nstate_dir="
        + json.dumps(str(state))
        + "\n",
        encoding="utf-8",
    )
    args = argparse.Namespace(
        command="migrate-copy", config=config, database=path, confirm_copy=False
    )
    with pytest.raises(ArchiveError, match="ExplicitBackupCopyConfirmationRequired"):
        await execute(args)
    args.confirm_copy = True
    report = await execute(args)
    assert report["inserted"] == 3 and report["mode"] == "offline_backup_copy"
    assert await execute(args) == {
        "mode": "offline_backup_copy",
        "inserted": 0,
        "existing": 3,
        "excluded": {"unconfirmed_assistant": 1},
    }
    assert path.stat().st_mode & 0o077 == 0
    assert not paths.database.exists()


def test_explicit_migration_copy_path_cannot_be_redirected_by_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copy = tmp_path / "copy.db"
    decoy = tmp_path / "must-not-create.db"
    monkeypatch.setenv("LEMONBOT_DATABASE_URL", "sqlite:///" + str(decoy))
    upgrade_database(copy)
    assert copy.exists() and not decoy.exists()
    with sqlite3.connect(copy) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "20261015_0008",
        )


def test_upgrade_from_previous_head_preserves_all_legacy_tables(tmp_path: Path) -> None:
    from importlib.resources import as_file, files

    from alembic import command
    from alembic.config import Config

    path = tmp_path / "old.db"
    with as_file(files("lemonbot.storage.migrations")) as scripts:
        config = Config()
        config.set_main_option("script_location", str(scripts))
        config.set_main_option("sqlalchemy.url", "sqlite:///" + str(path))
        config.attributes["explicit_database_path"] = True
        command.upgrade(config, "20260825_0006")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO runtime_state VALUES ('paused', '{\"paused\":true}', '2026-10-15')")
        before = list(db.iterdump())
    upgrade_database(path)
    upgrade_database(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value_json FROM runtime_state").fetchone() == ('{"paused":true}',)
        assert db.execute("SELECT count(*) FROM context_archive_messages").fetchone() == (0,)
        after = list(db.iterdump())
    assert any('"runtime_state"' in line for line in before)
    assert any('"runtime_state"' in line for line in after)
