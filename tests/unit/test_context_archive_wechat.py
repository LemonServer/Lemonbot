from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_wechat_conversation import KEY, TwoChats, setup

from lemonbot.connectors.wechat_agent import Binding, LabPaused, reference
from lemonbot.memory.unified.archive import ArchiveError, SQLiteMessageArchive
from lemonbot.memory.unified.models import SourceRef
from lemonbot.memory.unified.wechat import archive_scope, snapshot_message
from lemonbot.storage.migrate import upgrade_database

NOW = datetime(2026, 10, 15, tzinfo=UTC).isoformat()
ACCOUNT = reference(KEY, "account", "synthetic-account")
BINDING = Binding(ACCOUNT, "synthetic-group@chatroom", "synthetic-chat-ref", (1,), ("label",))


def row(**changes: Any) -> dict[str, Any]:
    return {
        "localId": 1,
        "chatId": BINDING.chat_id,
        "sender": "synthetic-alice",
        "type": 1,
        "content": "current text",
        "timestamp": NOW,
        "isSelf": False,
        **changes,
    }


def parse(raw: dict[str, Any]) -> Any:
    return snapshot_message(raw, binding=BINDING, key=KEY, is_group=True)[0]


def test_new_snapshot_separates_original_text_urls_cards_and_media_placeholders() -> None:
    text = "请读 https://example.org/a。然后看 https://example.org/b"
    message = parse(row(content=text))
    assert message.parts[0].text == text
    assert [part.url for part in message.parts[1:]] == [
        "https://example.org/a",
        "https://example.org/b",
    ]
    image = parse(row(type=3, content=""))
    assert image.parts[0].kind == "image" and image.parts[0].attachment is None
    assert image.transport_message_id == "1"
    card = parse(row(type=49, linkUrl="https://example.org/card", linkTitle="synthetic"))
    assert card.parts[0].kind == "card" and card.parts[0].title == "synthetic"
    assert parse(row(type=999)).parts[0].unavailable_reason == "unsupported_kind"


def test_exact_quote_preserves_current_text_and_never_guesses_recent_image() -> None:
    quoted = parse(
        row(
            localId=2,
            type=49,
            quoteReply=True,
            quotedText="quoted text, not current text",
            quotedImage=True,
            quotedMessageLocalId=1,
        )
    )
    assert quoted.parts[0].text == "current text"
    assert quoted.references[0].message_id == parse(row()).message_id
    assert quoted.references[0].part_id == "image"
    unresolved = parse(row(type=49, quoteReply=True, quotedImage=True, quotedMessageLocalId=None))
    assert unresolved.references == ()
    assert unresolved.parts[-1].unavailable_reason == "quoted_source_unavailable_or_ambiguous"


@pytest.mark.parametrize(
    "changes",
    [
        {"chatId": "wrong"},
        {"localId": True},
        {"sender": None},
        {"isSelf": None},
        {"timestamp": "2026-10-15T00:00:00"},
        {"type": True},
    ],
)
def test_snapshot_rejects_missing_identity_direction_and_timestamp(changes: dict[str, Any]) -> None:
    with pytest.raises(ArchiveError):
        parse(row(**changes))


def attach_archive(transport: Any, archive: SQLiteMessageArchive) -> None:
    for child in transport.children.values():
        child.archive = archive


async def wake_and_poll(transport: Any) -> list[Any]:
    await asyncio.wait_for(transport.wait_for_changes(), timeout=2)
    return await transport.poll()


async def test_group_normal_messages_archive_without_reply_download_or_navigation(
    tmp_path: Path,
) -> None:
    mock = TwoChats()
    mock.add(mock.group, "before baseline")
    upgrade_database(tmp_path / "archive.db")
    archive = SQLiteMessageArchive(tmp_path / "archive.db")
    transport = await setup(mock, tmp_path)
    attach_archive(transport, archive)
    try:
        normal = mock.add(mock.group, "ordinary message")
        image = mock.add(mock.group, "")
        image["type"] = 3
        mock.add(mock.group, "<@self>typed mention only")
        mock.add(mock.group, "genuine mention", mention=True)
        mock.add(mock.peer, "private text")
        opens = mock.opens
        events = await wake_and_poll(transport)
        assert sorted(event.text for event in events) == ["genuine mention", "private text"]
        group = transport.children[mock.targets()[0].binding.chat_ref]
        scope = archive_scope(group.binding, is_group=True)
        messages = await archive.list_messages(scope=scope, before_message_id=None, limit=20)
        assert len(messages) == 4
        assert not any(m.parts[0].text == "before baseline" for m in messages)
        assert any(m.transport_message_id == str(normal["localId"]) for m in messages)
        assert any(m.parts[0].kind == "image" and m.parts[0].attachment is None for m in messages)
        assert mock.opens == opens
        # An explicit new baseline must not archive/replay arrivals during downtime.
        mock.add(mock.group, "downtime")
        await transport._cycle(list(transport.children), baseline=True)
        assert len(await archive.list_messages(scope=scope, before_message_id=None, limit=20)) == 4
    finally:
        await transport.close()


async def test_disabled_archive_leaves_storage_empty(tmp_path: Path) -> None:
    mock = TwoChats()
    upgrade_database(tmp_path / "archive.db")
    transport = await setup(mock, tmp_path)
    try:
        mock.add(mock.group, "normal")
        mock.add(mock.group, "question", mention=True)
        assert len(await wake_and_poll(transport)) == 1
        with sqlite3.connect(tmp_path / "archive.db") as db:
            assert db.execute("SELECT count(*) FROM context_archive_messages").fetchone()[0] == 0
    finally:
        await transport.close()


async def test_archive_failure_keeps_anchors_and_persists_target_pause(tmp_path: Path) -> None:
    mock = TwoChats()
    upgrade_database(tmp_path / "archive.db")
    archive = SQLiteMessageArchive(tmp_path / "archive.db")
    transport = await setup(mock, tmp_path)
    attach_archive(transport, archive)
    try:
        target = mock.targets()[0]
        child = transport.children[target.binding.chat_ref]
        anchors = child.anchors
        valid = mock.add(mock.group, "valid")
        broken = mock.add(mock.group, "bad", mention=True)
        broken["sender"] = None
        assert await wake_and_poll(transport) == []
        assert child.anchors == anchors
        assert transport.target_pauses[target.binding.chat_ref] == "ArchiveSnapshotFailed"
        assert (tmp_path / "targets-paused.json").exists()
        scope = archive_scope(target.binding, is_group=True)
        assert (
            await archive.get_message(
                SourceRef(
                    scope=scope,
                    message_id=reference(
                        KEY,
                        "event",
                        target.binding.account_ref,
                        target.binding.chat_id,
                        str(valid["localId"]),
                    ),
                )
            )
            is None
        )
    finally:
        await transport.close()


async def test_standalone_transport_archives_only_after_stable_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = TwoChats()
    upgrade_database(tmp_path / "archive.db")
    archive = SQLiteMessageArchive(tmp_path / "archive.db")
    transport = await setup(mock, tmp_path)
    try:
        child = transport.children[mock.targets()[0].binding.chat_ref]
        child.archive = archive
        child.anchors = {1}
        first = row(chatId=child.binding.chat_id, localId=2)
        anchor = row(chatId=child.binding.chat_id, localId=1)
        changed = row(chatId=child.binding.chat_id, localId=3)
        pages = iter([[first, anchor], [changed, first, anchor]] * 3)

        async def page(_offset: int = 0) -> list[dict[str, Any]]:
            return next(pages)

        monkeypatch.setattr(child, "page", page)
        with pytest.raises(LabPaused, match="MessageWindowChanged"):
            await child.poll()
        scope = archive_scope(child.binding, is_group=True)
        assert await archive.list_messages(scope=scope, before_message_id=None, limit=20) == ()
    finally:
        await transport.close()


async def test_wrong_account_snapshot_cannot_write_archive(tmp_path: Path) -> None:
    mock = TwoChats()
    upgrade_database(tmp_path / "archive.db")
    transport = await setup(mock, tmp_path)
    attach_archive(transport, SQLiteMessageArchive(tmp_path / "archive.db"))
    original = mock.metadata

    async def wrong_account(request: httpx.Request) -> httpx.Response:
        response = await original(request)
        if request.url.path == "/read-cycle":
            return httpx.Response(200, json={**response.json(), "account": "different-account"})
        return response

    try:
        await transport.metadata.aclose()
        transport.metadata = httpx.AsyncClient(
            base_url="http://127.0.0.1:6175", transport=httpx.MockTransport(wrong_account)
        )
        for child in transport.children.values():
            child.metadata = transport.metadata
        with pytest.raises(LabPaused, match="AccountChanged"):
            await transport._cycle(list(transport.children))
        with sqlite3.connect(tmp_path / "archive.db") as db:
            assert db.execute("SELECT count(*) FROM context_archive_messages").fetchone()[0] == 0
    finally:
        await transport.close()
