from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lemonbot.config.paths import RuntimePaths
from lemonbot.config.settings import AppSettings
from lemonbot.domain.models import DeliveryReceipt, DeliveryStatus, OutboundMessage, OutboxState
from lemonbot.memory.unified.archive import ArchiveError, SQLiteMessageArchive
from lemonbot.memory.unified.cli import execute, validate_copy
from lemonbot.memory.unified.interfaces import MessageArchive
from lemonbot.memory.unified.models import (
    ConversationScope,
    Message,
    MessagePart,
    Observation,
    SourceRef,
)
from lemonbot.storage import CoreRepository, Database
from lemonbot.storage.migrate import upgrade_database

NOW = datetime(2026, 10, 15, tzinfo=UTC)
SCOPE = ConversationScope(
    channel="synthetic", account_id="account-a", conversation_id="group-a", kind="group"
)


def message(identifier: str = "m1", **changes: Any) -> Message:
    return Message.model_validate(
        {
            "scope": SCOPE,
            "message_id": identifier,
            "event_id": identifier,
            "sender_id": "alice",
            "direction": "inbound",
            "occurred_at": NOW,
            "parts": (MessagePart(part_id="text", kind="text", text="synthetic unchanged body"),),
            **changes,
        }
    )


def source(identifier: str = "m1", **changes: Any) -> SourceRef:
    return SourceRef.model_validate({"scope": SCOPE, "message_id": identifier, **changes})


@pytest.fixture
def archive(tmp_path: Path) -> SQLiteMessageArchive:
    path = tmp_path / "archive.db"
    upgrade_database(path)
    return SQLiteMessageArchive(path)


async def test_archive_roundtrip_idempotence_different_ids_and_concurrent_insert(
    archive: SQLiteMessageArchive,
) -> None:
    contract: MessageArchive = archive
    assert await contract.put_message(message())
    assert not await contract.put_message(message())
    assert await contract.put_message(message("m2"))
    assert sorted(
        await asyncio.gather(*(archive.put_message(message("concurrent")) for _ in range(3)))
    ) == [False, False, True]
    reopened = SQLiteMessageArchive(archive.path)
    assert await reopened.get_message(source()) == message()
    with pytest.raises(ArchiveError, match="ArchiveMessageConflict"):
        await archive.put_message(message(sender_id="bob"))
    with pytest.raises(ArchiveError, match="ArchiveMessageConflict"):
        await archive.put_message(
            message(parts=(MessagePart(part_id="text", kind="text", text="new"),))
        )


async def test_archive_requires_full_scope_and_exact_part(archive: SQLiteMessageArchive) -> None:
    await archive.put_message(message())
    assert await archive.get_message(source(part_id="missing")) is None
    assert await archive.get_message(source(part_id="text")) == message()
    for changed in (
        {"account_id": "account-b"},
        {"conversation_id": "group-b"},
        {"kind": "private"},
        {"channel": "other"},
    ):
        foreign = ConversationScope.model_validate({**SCOPE.model_dump(), **changed})
        assert await archive.get_message(source(scope=foreign)) is None
        assert await archive.list_messages(scope=foreign, before_message_id=None, limit=10) == ()


async def test_archive_preserves_exact_quotes_and_unresolved_sources(
    archive: SQLiteMessageArchive,
) -> None:
    quoted = message("quoted", references=(source("original", part_id="image"),))
    assert await archive.put_message(quoted)
    assert await archive.get_message(source("original")) is None
    original = message(
        "original",
        parts=(MessagePart(part_id="image", kind="image", unavailable_reason="not_loaded"),),
    )
    await archive.put_message(original)
    saved = await archive.get_message(source("quoted"))
    assert saved is not None and saved.parts[0].text == quoted.parts[0].text
    assert await archive.get_message(saved.references[0]) == original


async def test_archive_batch_conflict_rolls_back_before_anchor_commit(
    archive: SQLiteMessageArchive,
) -> None:
    await archive.put_message(message())
    with pytest.raises(ArchiveError, match="ArchiveMessageConflict"):
        await archive.put_observed_batch(
            [(message("new"), None), (message(sender_id="different"), None)]
        )
    assert await archive.get_message(source("new")) is None


async def test_pagination_is_scoped_stable_and_bounded(archive: SQLiteMessageArchive) -> None:
    for identifier in ("m1", "m3", "m2"):
        await archive.put_message(message(identifier))
    await archive.put_message(message("newest", occurred_at=NOW + timedelta(seconds=1)))
    first = await archive.list_messages(scope=SCOPE, before_message_id=None, limit=2)
    second = await archive.list_messages(
        scope=SCOPE, before_message_id=first[-1].message_id, limit=2
    )
    assert [m.message_id for m in (*first, *second)] == ["newest", "m3", "m2", "m1"]
    with pytest.raises(ArchiveError, match="ArchiveCursorMissing"):
        await archive.list_messages(scope=SCOPE, before_message_id="absent", limit=10)
    with pytest.raises(ArchiveError, match="ArchiveQueryBounds"):
        await archive.list_messages(scope=SCOPE, before_message_id=None, limit=1001)


async def test_observations_require_existing_eligible_exact_source(
    archive: SQLiteMessageArchive,
) -> None:
    fields = dict(
        observation_id="obs-1",
        source=source(part_id="card"),
        kind="page_body",
        status="unavailable",
        error_code="verification_required",
        extractor_version="reader/v1",
        created_at=NOW,
    )
    observation = Observation(**fields)
    with pytest.raises(ArchiveError, match="ArchiveObservationSourceMissing"):
        await archive.put_observation(observation)
    await archive.put_message(
        message(parts=(MessagePart(part_id="card", kind="card", url="https://example.org/card"),))
    )
    assert await archive.put_observation(observation)
    assert not await archive.put_observation(observation)
    assert await archive.get_observations(source()) == (observation,)
    assert observation.text is None
    with pytest.raises(ArchiveError, match="ArchiveObservationConflict"):
        await archive.put_observation(Observation(**{**fields, "error_code": "different"}))
    with pytest.raises(ArchiveError, match="ArchiveObservationPartMismatch"):
        await archive.put_observation(
            Observation(
                **{
                    **fields,
                    "observation_id": "obs-2",
                    "kind": "ocr",
                }
            )
        )
    assert await archive.get_observations(source(part_id="missing")) == ()


async def confirmed_outbox(
    archive: SQLiteMessageArchive,
    *,
    status: DeliveryStatus = DeliveryStatus.ACKNOWLEDGED,
    reply_to: str = "m1",
    text: str = "answer",
) -> OutboundMessage:
    database = Database.from_path(archive.path)
    repository = CoreRepository(database)
    outbound = OutboundMessage(
        channel=SCOPE.channel,
        chat_id=SCOPE.conversation_id,
        reply_to_event_id=reply_to,
        text=text,
    )
    try:
        item = await repository.create_outbox(outbound)
        assert await repository.reserve_next_outbox(SCOPE.channel) is not None
        assert await repository.mark_dispatching(item.id)
        await repository.apply_receipt(
            item.id,
            DeliveryReceipt(
                message_id=outbound.message_id,
                status=status,
                external_id="receipt-exact",
                acknowledged_at=NOW if status is DeliveryStatus.ACKNOWLEDGED else None,
            ),
        )
    finally:
        await database.close()
    return outbound


@pytest.mark.parametrize("status", list(DeliveryStatus))
async def test_only_unique_confirmed_self_readback_enters_archive(
    archive: SQLiteMessageArchive,
    status: DeliveryStatus,
) -> None:
    await archive.put_message(message())
    outbound = await confirmed_outbox(archive, status=status)
    observed = message(
        "self-row",
        event_id=None,
        sender_id="bot",
        direction="outbound",
        delivery_state=OutboxState.ACKNOWLEDGED,
        parts=(MessagePart(part_id="text", kind="text", text="answer"),),
    )
    with pytest.raises(ArchiveError, match="ArchiveOutboundRequiresReceipt"):
        await archive.put_message(observed)
    assert await archive.put_observed_batch([(observed, "manual-same-body")]) == 0
    count = await archive.put_observed_batch([(observed, "receipt-exact")])
    if status is DeliveryStatus.ACKNOWLEDGED:
        assert count == 1
        assert await archive.put_observed_batch([(observed, "receipt-exact")]) == 0
        assert (
            await archive.apply_delivery_state(source("self-row"), OutboxState.ACKNOWLEDGED)
            == observed
        )
        with pytest.raises(ArchiveError, match="ArchiveDeliveryTransitionForbidden"):
            await archive.apply_delivery_state(source("self-row"), OutboxState.PENDING)
    else:
        assert count == 0
        assert await archive.get_message(source("self-row")) is None
    with sqlite3.connect(archive.path) as db:
        state, attempts = db.execute(
            "SELECT state,attempts FROM outbox WHERE message_id=?", (str(outbound.message_id),)
        ).fetchone()
    assert attempts == 1
    assert (
        state
        == {
            DeliveryStatus.ACKNOWLEDGED: "acknowledged",
            DeliveryStatus.UNKNOWN: "unknown",
            DeliveryStatus.FAILED: "dead",
        }[status]
    )


async def test_receipt_cannot_promote_another_account_or_mismatched_body(
    archive: SQLiteMessageArchive,
) -> None:
    await archive.put_message(message())
    await confirmed_outbox(archive)
    self_row = message(
        "self",
        event_id=None,
        direction="outbound",
        delivery_state=OutboxState.ACKNOWLEDGED,
        parts=(MessagePart(part_id="text", kind="text", text="wrong body"),),
    )
    with pytest.raises(ArchiveError, match="ArchiveReceiptMismatch"):
        await archive.put_observed_batch([(self_row, "receipt-exact")])
    foreign = ConversationScope.model_validate({**SCOPE.model_dump(), "account_id": "other"})
    self_row = message(
        "foreign-self",
        scope=foreign,
        direction="outbound",
        delivery_state=OutboxState.ACKNOWLEDGED,
        parts=(MessagePart(part_id="text", kind="text", text="answer"),),
    )
    with pytest.raises(ArchiveError, match="ArchiveReplySourceMissing"):
        await archive.put_observed_batch([(self_row, "receipt-exact")])


async def test_read_only_query_does_not_create_migrate_or_modify_database(
    archive: SQLiteMessageArchive,
    tmp_path: Path,
) -> None:
    await archive.put_message(message())

    # Check a coherent SQLite snapshot, including WAL contents, before and after.
    def fingerprint() -> str:
        with sqlite3.connect(archive.path) as db:
            return hashlib.sha256("\n".join(db.iterdump()).encode()).hexdigest()

    before = fingerprint()
    result = await execute(
        argparse.Namespace(
            command="inspect",
            database=archive.path,
            channel=SCOPE.channel,
            account=SCOPE.account_id,
            conversation=SCOPE.conversation_id,
            kind=SCOPE.kind,
            message_id=None,
            before=None,
            limit=20,
            include_content=False,
        )
    )
    assert result["count"] == 1 and "synthetic unchanged body" not in json.dumps(result)
    assert fingerprint() == before
    missing = tmp_path / "missing.db"
    with pytest.raises(ArchiveError, match="ArchiveDatabaseMissing"):
        await SQLiteMessageArchive(missing).get_message(source())
    assert not missing.exists()


def test_copy_guard_rejects_live_symlink_hardlink_and_non_backup_paths(tmp_path: Path) -> None:
    paths = RuntimePaths(root=tmp_path / "runtime", profile="lab")
    paths.ensure()
    paths.database.touch()
    copy = paths.backups / "offline.db"
    copy.touch()
    assert validate_copy(copy, paths) == copy
    with pytest.raises(ArchiveError, match="LiveDatabaseMigrationForbidden"):
        validate_copy(paths.database, paths)
    symlink = paths.backups / "alias.db"
    symlink.symlink_to(paths.database)
    with pytest.raises(ArchiveError, match="LiveDatabaseMigrationForbidden"):
        validate_copy(symlink, paths)
    hardlink = paths.backups / "hardlink.db"
    os.link(paths.database, hardlink)
    with pytest.raises(ArchiveError, match="MigrationRequiresIndependentCopy"):
        validate_copy(hardlink, paths)
    outside = tmp_path / "not-a-profile-backup.db"
    outside.touch()
    with pytest.raises(ArchiveError, match="MigrationCopyMustBeInProfileBackups"):
        validate_copy(outside, paths)


def test_archive_switch_is_explicit_off_and_bound_to_conversation_runtime() -> None:
    assert not AppSettings().context_memory.archive_enabled
    with pytest.raises(ValueError, match="unified archive requires the bound conversation runtime"):
        AppSettings.model_validate({"context_memory": {"archive_enabled": True}})


async def test_archive_detects_payload_tampering(archive: SQLiteMessageArchive) -> None:
    await archive.put_message(message())
    with sqlite3.connect(archive.path) as db:
        db.execute(
            "UPDATE context_archive_parts SET payload_json=json_set("
            "payload_json,'$.text','tampered')"
        )
    with pytest.raises(ArchiveError, match="ArchivePayloadCorrupt"):
        await archive.get_message(source())


async def test_ambiguous_receipt_never_picks_first_or_latest(
    archive: SQLiteMessageArchive,
) -> None:
    await archive.put_message(message())
    await archive.put_message(message("m2"))
    await confirmed_outbox(archive)
    await confirmed_outbox(archive, reply_to="m2")
    observed = message(
        "self-row",
        event_id=None,
        sender_id="bot",
        direction="outbound",
        delivery_state=OutboxState.ACKNOWLEDGED,
        parts=(MessagePart(part_id="text", kind="text", text="answer"),),
    )
    with pytest.raises(ArchiveError, match="ArchiveReceiptAmbiguous"):
        await archive.put_observed_batch([(observed, "receipt-exact")])
    assert await archive.get_message(source("self-row")) is None


async def test_observation_read_rejects_wrong_message_in_same_conversation(
    archive: SQLiteMessageArchive,
) -> None:
    await archive.put_message(message())
    observation = Observation(
        observation_id="obs",
        source=source(part_id="text"),
        kind="unavailable",
        status="unavailable",
        error_code="synthetic_failure",
        extractor_version="test/v1",
        created_at=NOW,
    )
    await archive.put_observation(observation)
    with sqlite3.connect(archive.path) as db:
        db.execute(
            "UPDATE context_archive_observations SET payload_json="
            "json_set(payload_json,'$.source.message_id','another-message')"
        )
    with pytest.raises(ArchiveError, match="ArchiveScopeConflict"):
        await archive.get_observations(source())


async def test_confirmed_link_reply_preserves_original_text_and_url_parts(
    archive: SQLiteMessageArchive,
) -> None:
    await archive.put_message(message())
    text = "资料：https://example.org/source"
    await confirmed_outbox(archive, text=text)
    observed = message(
        "self-link",
        event_id=None,
        direction="outbound",
        sender_id="bot",
        delivery_state=OutboxState.ACKNOWLEDGED,
        parts=(
            MessagePart(part_id="text", kind="text", text=text),
            MessagePart(part_id="url-0", kind="link", url="https://example.org/source"),
        ),
    )
    assert await archive.put_observed_batch([(observed, "receipt-exact")]) == 1
    assert await archive.get_message(source("self-link")) == observed
