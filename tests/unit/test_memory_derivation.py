from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lemonbot.domain import MessageRole
from lemonbot.memory import (
    ConversationTurn,
    InMemoryMemoryStore,
    MemoryDerivationService,
    MemoryKind,
)
from lemonbot.orchestration import FakeModelBackend


async def test_rolling_summary_supersedes_only_within_the_same_conversation() -> None:
    first_json = (
        '{"memories":[],"summary":{"text":"用户计划周五交报告。",'
        '"confidence":0.9}}'
    )
    second_json = (
        '{"memories":[],"summary":{"text":"用户改为周四交报告。",'
        '"confidence":0.95}}'
    )
    backend = FakeModelBackend([first_json, second_json])
    store = InMemoryMemoryStore()
    service = MemoryDerivationService(store=store, backend=backend)
    now = datetime.now(UTC)

    first = await service.derive(
        turns=(
            ConversationTurn(
                message_id="m1",
                event_id="e1",
                channel="fake",
                chat_id="chat-a",
                role=MessageRole.USER,
                content="我计划周五交报告",
                occurred_at=now,
            ),
            ConversationTurn(
                message_id="m2",
                event_id="e1",
                channel="fake",
                chat_id="chat-a",
                role=MessageRole.ASSISTANT,
                content="好的",
                occurred_at=now + timedelta(seconds=1),
            ),
        ),
        include_summary=True,
        maximum_context_tokens=8_000,
    )
    old_summary = first[0]
    second = await service.derive(
        turns=(
            ConversationTurn(
                message_id="m3",
                event_id="e2",
                channel="fake",
                chat_id="chat-a",
                role=MessageRole.USER,
                content="改成周四",
                occurred_at=now + timedelta(minutes=1),
            ),
            ConversationTurn(
                message_id="m4",
                event_id="e2",
                channel="fake",
                chat_id="chat-a",
                role=MessageRole.ASSISTANT,
                content="已更新",
                occurred_at=now + timedelta(minutes=1, seconds=1),
            ),
        ),
        include_summary=True,
        maximum_context_tokens=8_000,
    )
    new_summary = second[0]

    stored_old = await store.get(
        old_summary.memory_id,
        channel=old_summary.channel,
        chat_id=old_summary.chat_id,
    )
    assert stored_old is not None and stored_old.active is False
    assert new_summary.provenance.supersedes == (old_summary.memory_id,)
    assert new_summary.provenance.source_message_ids == ("m1", "m2", "m3", "m4")
    active = await store.search(
        channel="fake",
        chat_id="chat-a",
        query="",
        kinds=(MemoryKind.SUMMARY,),
    )
    assert [hit.record.memory_id for hit in active] == [new_summary.memory_id]
