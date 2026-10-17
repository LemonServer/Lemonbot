"""Offline stage-7 lifecycle/readiness rehearsals. No real config, data or APIs."""

from __future__ import annotations

import json
import sqlite3
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from test_context_archive_migration import (
    KEY,
    TARGET,
    create_legacy,
    legacy_snapshot,
)

from lemonbot.backup import BackupError, create_backup, restore_backup
from lemonbot.config.paths import RuntimePaths
from lemonbot.config.settings import AppSettings
from lemonbot.connectors.wechat_agent import CHANNEL
from lemonbot.data import DataOperationError, delete_conversation, export_profile_data
from lemonbot.memory.models import MemoryKind
from lemonbot.memory.unified.archive import SQLiteMessageArchive, scope_key
from lemonbot.memory.unified.index import SQLiteRetrievalIndex
from lemonbot.memory.unified.maintenance import UnifiedMemoryStore
from lemonbot.memory.unified.migration import migrate_legacy_copy
from lemonbot.memory.unified.models import MemoryEntry, Message, MessagePart, Observation, SourceRef
from lemonbot.memory.unified.operations import database_status, health, source_status
from lemonbot.memory.unified.retrieval import HybridContextRetriever
from lemonbot.memory.unified.wechat import archive_scope


async def populated(paths):
    paths.ensure()
    await create_legacy(paths.database)
    archive = SQLiteMessageArchive(paths.database)
    await migrate_legacy_copy(archive, targets=(TARGET,), key=KEY)
    scope = archive_scope(TARGET.binding, is_group=True)
    messages = await archive.list_messages(scope=scope, before_message_id=None, limit=20)
    old = next(m for m in messages if m.message_id == "event-ack")
    current = old.model_copy(
        update={
            "message_id": "correction",
            "event_id": "correction",
            "parts": (MessagePart(part_id="text", kind="text", text="new synthetic fact"),),
        }
    )
    await archive.put_message(current)
    store = UnifiedMemoryStore(archive)
    first = MemoryEntry(
        memory_id="old-memory",
        scope=scope,
        kind=MemoryKind.FACT,
        text="old synthetic fact",
        sources=(SourceRef(scope=scope, message_id=old.message_id),),
        author_ids=(old.sender_id,),
        created_at=datetime.now(UTC),
        derivation_version="test/v1",
    )
    await store.put_memory(first)
    second = first.model_copy(
        update={
            "memory_id": "new-memory",
            "sources": (SourceRef(scope=scope, message_id=current.message_id),),
            "supersedes": ("old-memory",),
            "text": "new synthetic fact",
            "created_at": datetime.now(UTC),
        }
    )
    await store.put_memory(second)
    page = old.model_copy(
        update={
            "message_id": "page",
            "event_id": "page",
            "parts": (MessagePart(part_id="link", kind="link", url="https://example.org"),),
            "references": (first.sources[0],),
        }
    )
    await archive.put_message(page)
    observation = Observation(
        observation_id="body",
        source=SourceRef(scope=scope, message_id="page", part_id="link"),
        kind="page_body",
        status="available",
        text="private synthetic page body marker",
        extractor_version="test/v1",
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    await archive.put_observation(observation)
    await store.cache_content(observation)
    await store.topics(scope)
    job = await store.claim_job(
        scope,
        ("event-ack", "page"),
        now=datetime.now(UTC),
        reserve=Decimal(".07"),
        daily_cap=Decimal(".50"),
        timezone="Asia/Shanghai",
    )
    assert job
    with sqlite3.connect(paths.database) as db:
        row = db.execute(
            "SELECT message_id,external_id FROM outbox WHERE state='acknowledged'"
        ).fetchone()
        assistant = next(m for m in messages if m.direction == "outbound")
        db.execute(
            "INSERT INTO unified_delivery_proofs VALUES (?,?,?)",
            (row[0], assistant.model_dump_json(), row[1]),
        )
        db.execute(
            "INSERT INTO unified_receipts VALUES (?,?,?)",
            (row[0], scope_key(scope), datetime.now(UTC).isoformat()),
        )
        db.execute(
            "INSERT INTO unified_stage_usage VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "synthetic-usage",
                "answer",
                datetime.now(UTC).isoformat(),
                1,
                10,
                5,
                3,
                ".01",
                "known",
            ),
        )
    retriever = HybridContextRetriever(
        archive,
        SQLiteRetrievalIndex(
            paths.root / "context-index" / paths.profile / "index.sqlite3", paths.database
        ),
    )
    await retriever.refresh(scope)
    await retriever.close()
    return archive, store, scope


async def test_read_only_diagnostics_are_redacted_and_detect_schema_pause_unknown(tmp_path):
    paths = RuntimePaths(root=tmp_path / "runtime", profile="lab")
    _archive, _store, scope = await populated(paths)
    before = paths.database.read_bytes()
    report = database_status(paths.database)
    assert report["ok"] and report["pause"]["paused_channels"] == 1
    assert report["outbox"]["unknown"] == 1
    assert all(report["counts"][t] > 0 for t in report["counts"])
    assert "private synthetic page body marker" not in json.dumps(report)
    assert scope.account_id not in json.dumps(report) and scope.conversation_id not in json.dumps(
        report
    )
    assert (await source_status(paths.database, scope, message_id="event-ack"))["corrections"][
        "count"
    ] == 1
    assert not (await source_status(paths.database, scope, memory_id="old-memory"))["active"]
    assert (await source_status(paths.database, scope, memory_id="new-memory"))["active"]
    with pytest.raises(ValueError):
        await source_status(
            paths.database, scope.model_copy(update={"account_id": "other"}), memory_id="new-memory"
        )
    assert paths.database.read_bytes() == before
    missing = tmp_path / "missing.db"
    with pytest.raises(OSError):
        database_status(missing)
    assert not missing.exists()
    settings = AppSettings.model_validate({"runtime": {"data_root": str(paths.root)}})
    assert health(settings, paths)["ok"] and not health(settings, paths)["enabled"]
    assert health(settings, paths)["embedding"]["device_probe"] == "not_run"
    with sqlite3.connect(paths.database) as db:
        db.execute("UPDATE alembic_version SET version_num='20260825_0006'")
    assert not database_status(paths.database)["schema_ready"]
    assert not health(settings, paths, database=paths.database)["ok"]


async def test_export_restore_preserves_all_context_unknown_pause_and_rebuilds_index(tmp_path):
    paths = RuntimePaths(root=tmp_path / "runtime", profile="lab")
    archive, store, scope = await populated(paths)
    before = database_status(paths.database)
    preserved = legacy_snapshot(paths.database)
    export = export_profile_data(paths, tmp_path / "export.zip")
    assert export.stat().st_mode & 0o077 == 0
    with zipfile.ZipFile(export) as z:
        assert set(z.namelist()) == {"manifest.json", "database/lab.db"}
        assert not any("context-index" in n for n in z.namelist())
    with sqlite3.connect(paths.database) as db:
        db.execute("DELETE FROM unified_topics")
    assert restore_backup(paths, export, preserve_current=False) is None
    assert database_status(paths.database) == before
    assert legacy_snapshot(paths.database) == preserved
    assert not (paths.root / "context-index" / "lab" / "index.sqlite3").exists()
    new = HybridContextRetriever(
        archive,
        SQLiteRetrievalIndex(
            paths.root / "context-index" / "lab" / "index.sqlite3", paths.database
        ),
    )
    await new.refresh(scope)
    assert (await store.get_memory(scope=scope, memory_id="new-memory")).active
    await new.close()


async def test_delete_preserves_budget_and_other_scope(
    tmp_path,
):
    paths = RuntimePaths(root=tmp_path / "runtime", profile="lab")
    archive, _store, scope = await populated(paths)
    other = scope.model_copy(update={"conversation_id": "other-private", "kind": "private"})
    await archive.put_message(
        Message(
            scope=other,
            message_id="other-message",
            event_id="other-message",
            sender_id="other-author",
            direction="inbound",
            occurred_at=datetime.now(UTC),
            parts=(MessagePart(part_id="text", kind="text", text="unrelated marker"),),
        )
    )
    result = delete_conversation(paths, channel=scope.channel, chat_id=scope.conversation_id)
    assert result.rows_deleted["context_archive_messages"] > 0
    assert result.rows_deleted["unified_delivery_proofs"] == 1
    assert result.rows_deleted["unified_memories"] == 2
    assert result.rows_redacted == {"unified_jobs": 1}
    assert not await archive.list_messages(scope=scope, before_message_id=None, limit=20)
    assert (await archive.list_messages(scope=other, before_message_id=None, limit=20))[
        0
    ].message_id == "other-message"
    with sqlite3.connect(paths.database) as db:
        row = db.execute("SELECT scope,sources,charged FROM unified_jobs").fetchone()
        assert (
            row[0].startswith("deleted/") and row[1] == "[]" and Decimal(row[2]) == Decimal(".07")
        )
        assert db.execute("SELECT count(*) FROM unified_stage_usage").fetchone() == (1,)
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    assert not (paths.root / "context-index" / "lab" / "index.sqlite3").exists()
    with pytest.raises(ValueError):
        await source_status(paths.database, scope, memory_id="new-memory")


@pytest.mark.parametrize("operation", ["restore", "delete"])
async def test_lifecycle_refuses_linked_index_without_touching_database(tmp_path, operation):
    paths = RuntimePaths(root=tmp_path / "runtime", profile="lab")
    _, _, scope = await populated(paths)
    backup = create_backup(paths, tmp_path / "backup.zip")
    index = paths.root / "context-index" / "lab" / "index.sqlite3"
    index.unlink()
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"private sentinel")
    index.symlink_to(sentinel)
    before = legacy_snapshot(paths.database)
    if operation == "delete":
        with pytest.raises(DataOperationError, match="unsafe context index"):
            delete_conversation(paths, channel=scope.channel, chat_id=scope.conversation_id)
    else:
        with pytest.raises(BackupError, match="unsafe context index"):
            restore_backup(paths, backup, preserve_current=False)
    assert legacy_snapshot(paths.database) == before
    assert sentinel.read_bytes() == b"private sentinel"


async def test_shared_archive_attachment_not_removed_by_conversation_deletion(tmp_path):
    import hashlib

    from lemonbot.memory.unified.models import AttachmentRef

    paths = RuntimePaths(root=tmp_path / "runtime", profile="lab")
    archive, _store, scope = await populated(paths)
    other = scope.model_copy(update={"conversation_id": "other-image-chat"})
    hashes = []
    for i in range(2):
        content = f"synthetic-object-{i}".encode()
        digest = hashlib.sha256(content).hexdigest()
        target = paths.objects / digest[:2] / digest
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        hashes.append(digest)
        part = MessagePart(
            part_id="image",
            kind="image",
            attachment=AttachmentRef(
                sha256=digest,
                size=len(content),
                media_type="image/jpeg",
                expires_at=datetime.now(UTC) + timedelta(days=7),
            ),
        )
        source = Message(
            scope=scope,
            message_id=f"image-{i}",
            direction="inbound",
            sender_id="alice",
            occurred_at=datetime.now(UTC),
            parts=(part,),
        )
        await archive.put_message(source)
        if i == 0:
            await archive.put_message(source.model_copy(update={"scope": other}))
    result = delete_conversation(paths, channel=scope.channel, chat_id=scope.conversation_id)
    assert result.objects_removed == 1
    assert (paths.objects / hashes[0][:2] / hashes[0]).exists()
    assert not (paths.objects / hashes[1][:2] / hashes[1]).exists()
    assert await archive.list_messages(scope=other, before_message_id=None, limit=20)


async def test_old_schema_backup_copy_migration_idempotence_and_config_rollback(
    tmp_path, monkeypatch
):
    import argparse
    from dataclasses import asdict
    from importlib.resources import as_file, files

    import test_context_archive_migration as legacy
    from alembic import command
    from alembic.config import Config

    from lemonbot.connectors.wechat_agent_state import private_write, save_binding
    from lemonbot.memory.unified.cli import execute
    from lemonbot.orchestration import EventPipeline, FakeModelBackend
    from lemonbot.policy import DeterministicPolicy
    from lemonbot.storage import CoreRepository, Database

    def old_schema(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with as_file(files("lemonbot.storage.migrations")) as scripts:
            config = Config()
            config.set_main_option("script_location", str(scripts))
            config.set_main_option("sqlalchemy.url", "sqlite:///" + str(path))
            config.attributes["explicit_database_path"] = True
            command.upgrade(config, "20260825_0006")

    monkeypatch.setattr(legacy, "upgrade_database", old_schema)
    paths = RuntimePaths(root=tmp_path / "runtime", profile="lab")
    paths.ensure()
    await legacy.create_legacy(paths.database)
    original = legacy_snapshot(paths.database)
    archive = create_backup(paths, tmp_path / "pre-upgrade.zip")
    copy = paths.backups / "migration-copy.db"
    with zipfile.ZipFile(archive) as z:
        copy.write_bytes(z.read("database/lab.db"))
    state = tmp_path / "synthetic-state"
    state.mkdir(mode=0o700)
    private_write(state / "comparison-key", KEY)
    save_binding(state / "binding.json", TARGET.binding)
    private_write(
        state / "conversation-binding.json",
        json.dumps(
            {
                "operator_confirmed": True,
                "account_ref": TARGET.binding.account_ref,
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
        + "\n"
    )
    args = argparse.Namespace(
        command="migrate-copy", config=config, database=copy, confirm_copy=True
    )
    first = await execute(args)
    assert first["inserted"] == 3
    assert (await execute(args))["inserted"] == 0
    assert database_status(copy)["ok"]
    assert legacy_snapshot(copy) == original
    assert legacy_snapshot(paths.database) == original
    # Configuration rollback uses the current schema-compatible legacy pipeline, no downgrade.
    db = Database.from_path(copy)
    repo = CoreRepository(db)
    model = FakeModelBackend()
    old_pipeline = EventPipeline(repo, DeterministicPolicy(repo), model, context_flow=None)
    try:
        assert old_pipeline.context_flow is None
        assert await repo.is_paused(CHANNEL)
        assert len(await repo.list_unknown_outbox()) == 1
        history = await repo.recent_messages(CHANNEL, TARGET.binding.chat_ref, confirmed_only=True)
        assert any(m.role.value == "assistant" for m in history)
        assert model.requests == []
    finally:
        await db.close()
    # Refuse schema downgrade; neither old UNKNOWN nor new archive is discarded.
    with as_file(files("lemonbot.storage.migrations")) as scripts:
        cfg = Config()
        cfg.set_main_option("script_location", str(scripts))
        cfg.set_main_option("sqlalchemy.url", "sqlite:///" + str(copy))
        cfg.attributes["explicit_database_path"] = True
        with pytest.raises(RuntimeError, match="downgrade"):
            command.downgrade(cfg, "20260825_0006")
    assert legacy_snapshot(copy) == original
    assert database_status(copy)["counts"]["context_archive_messages"] == 3


async def test_prepared_offline_migration_requires_confirmation_drain_lock_and_backups(tmp_path):
    import argparse
    from dataclasses import asdict

    from lemonbot.connectors.wechat_agent_state import private_write, save_binding
    from lemonbot.memory.unified.cli import execute
    from lemonbot.runtime_lock import AlreadyRunningError, RuntimeLock

    paths = RuntimePaths(root=tmp_path / "runtime", profile="lab")
    await populated(paths)
    state = tmp_path / "synthetic-state"
    state.mkdir(mode=0o700)
    private_write(state / "comparison-key", KEY)
    save_binding(state / "binding.json", TARGET.binding)
    private_write(
        state / "conversation-binding.json",
        json.dumps(
            {
                "operator_confirmed": True,
                "account_ref": TARGET.binding.account_ref,
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
        + "\n"
    )
    args = argparse.Namespace(command="migrate-offline", config=config, confirm_stopped=False)
    with pytest.raises(ValueError, match="ExplicitStoppedConfirmationRequired"):
        await execute(args)
    args.confirm_stopped = True
    with sqlite3.connect(paths.database) as db:
        db.execute("UPDATE inbox SET state='pending'")
    with pytest.raises(ValueError, match="PausedDrained"):
        await execute(args)
    assert not list(paths.backups.glob("context-before-*.zip"))
    with sqlite3.connect(paths.database) as db:
        db.execute("UPDATE inbox SET state='done'")
    before = legacy_snapshot(paths.database)
    with RuntimeLock(paths.lock_file):
        with pytest.raises(AlreadyRunningError):
            await execute(args)
    first = await execute(args)
    second = await execute(args)
    assert first["inserted"] == second["inserted"] == 0
    assert first["existing"] == second["existing"] == 3
    assert Path(first["backup"]).is_file() and Path(second["backup"]).is_file()
    assert Path(first["backup"]).stat().st_mode & 0o077 == 0
    assert first["backup"] != second["backup"]
    assert legacy_snapshot(paths.database) == before
    with sqlite3.connect(paths.database) as db:
        db.execute("DELETE FROM runtime_state WHERE key LIKE ?", ("pause:%",))
    with pytest.raises(ValueError, match="PausedDrained"):
        await execute(args)


async def test_operations_cli_default_redaction_and_missing_file_no_creation(
    tmp_path, monkeypatch, capsys
):
    import sys

    from lemonbot.memory.unified.operations import main

    paths = RuntimePaths(root=tmp_path / "runtime", profile="lab")
    await populated(paths)
    monkeypatch.setattr(sys, "argv", ["operations", "status", "--database", str(paths.database)])
    main()
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] and report["outbox"]["unknown"] == 1
    missing = tmp_path / "never-create.db"
    monkeypatch.setattr(sys, "argv", ["operations", "status", "--database", str(missing)])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1 and not missing.exists()
    assert str(missing) not in capsys.readouterr().err


async def test_receipt_provenance_and_retrieval_diagnostics_hide_correction_content(tmp_path):
    import argparse

    from lemonbot.memory.unified import retrieval_cli

    paths = RuntimePaths(root=tmp_path / "runtime", profile="lab")
    archive, _store, scope = await populated(paths)
    native = next(
        m
        for m in await archive.list_messages(scope=scope, before_message_id=None, limit=20)
        if m.direction == "outbound"
    )
    report = await source_status(paths.database, scope, message_id=native.message_id)
    assert report["receipt_verified"] and report["eligible"]
    await archive.put_message(
        Message(
            scope=scope,
            message_id="diagnostic-query",
            sender_id="bob",
            direction="inbound",
            occurred_at=datetime.now(UTC),
            parts=(MessagePart(part_id="text", kind="text", text="synthetic fact"),),
        )
    )
    config = tmp_path / "offline-config.toml"
    config.write_text("[runtime]\ndata_root=" + json.dumps(str(paths.root)) + "\n")
    args = argparse.Namespace(
        command="query",
        config=config,
        database=paths.database,
        index=tmp_path / "diagnostic-index.db",
        cuda=False,
        channel=scope.channel,
        account=scope.account_id,
        conversation=scope.conversation_id,
        kind=scope.kind,
        current_id="diagnostic-query",
        limit=20,
        include_content=False,
    )
    result = await retrieval_cli.execute(args)
    assert any(c["memory_ids"] for c in result["result"]["candidates"])
    assert "new synthetic fact" not in json.dumps(result)
    assert "private synthetic page body marker" not in json.dumps(result)
    assert all("summary" not in c for c in result["result"]["candidates"])
    args.include_content = True
    visible = await retrieval_cli.execute(args)
    assert "new synthetic fact" in json.dumps(visible)


async def test_enabled_doctor_reports_missing_cuda_assets_without_starting_worker_or_keyring(
    tmp_path, monkeypatch
):
    from unittest.mock import AsyncMock

    from lemonbot.config.settings import load_settings
    from lemonbot.embeddings.proxy import IsolatedEmbeddingWorker

    paths = RuntimePaths(root=tmp_path / "runtime", profile="lab")
    await populated(paths)
    raw = load_settings(Path("config/wechat-agent-conversation.example.toml")).model_dump()
    raw["runtime"].update(connector="wechat_agent", data_root=str(paths.root))
    raw["wechat_agent"]["enabled"] = True
    raw["models"]["provider"] = "deepseek"
    raw["context_memory"].update(enabled=True, archive_enabled=True)
    settings = AppSettings.model_validate(raw)
    factory = AsyncMock(side_effect=AssertionError("doctor must never start GPU worker"))
    monkeypatch.setattr(IsolatedEmbeddingWorker, "create", factory)
    report = health(settings, paths)
    assert not report["ok"]
    assert report["embedding"]["configured_backend"] == "tensorrt"
    assert report["embedding"]["device_probe"] == "not_run"
    assert report["database"]["schema_ready"]
    assert any(not c["ok"] and c["name"] == "embedding_artifacts" for c in report["checks"])
    factory.assert_not_called()

    # Match the actual worker layout: assets_root/model holds pinned source files.
    import hashlib

    from lemonbot.embeddings import artifacts

    model_root = paths.embedding_assets / "model"
    model_root.mkdir(parents=True)
    payload = b"synthetic pinned model"
    (model_root / "model.onnx").write_bytes(payload)
    monkeypatch.setattr(
        artifacts,
        "FILES",
        {"model.onnx": ("model.onnx", len(payload), hashlib.sha256(payload).hexdigest())},
    )
    paths.embedding_worker.parent.mkdir(parents=True)
    paths.embedding_worker.write_text("synthetic executable never run")
    paths.embedding_worker.chmod(0o700)
    assert health(settings, paths)["ok"]
    (model_root / "model.onnx").write_bytes(b"tampered")
    assert not health(settings, paths)["ok"]
    factory.assert_not_called()
