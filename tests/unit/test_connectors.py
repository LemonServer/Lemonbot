from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from lemonbot.connectors import (
    AtspiEnrollment,
    AtspiObserveConnector,
    AtspiTranscriptItem,
    Connector,
    FakeConnector,
)
from lemonbot.connectors.atspi_protocol import AtspiHealth, AtspiSnapshot
from lemonbot.connectors.wechat_atspi import AtspiCursor
from lemonbot.domain import DeliveryStatus, InboundEvent, OutboundMessage
from lemonbot.domain.protocols import Connector as ConnectorProtocol


class SnapshotSource:
    def __init__(self, snapshots: tuple[AtspiSnapshot, ...]) -> None:
        self.values = snapshots
        self.closed = False

    async def snapshots(self) -> AsyncIterator[AtspiSnapshot]:
        for snapshot in self.values:
            yield snapshot

    async def health(self) -> AtspiHealth:
        return AtspiHealth(healthy=not self.closed, detail_code="ready")

    async def close(self) -> None:
        self.closed = True


def enrollment(kind: str = "private") -> AtspiEnrollment:
    return AtspiEnrollment.model_validate(
        {
            "account_fingerprint": "a" * 64,
            "ui_signature": "b" * 64,
            "targets": [
                {
                    "target_ref": "test_chat",
                    "chat_kind": kind,
                    "header_selector": [0, 1],
                    "header_fingerprint": "c" * 64,
                    "transcript_selector": [0, 2],
                    "self_item_signature": "d" * 64,
                    "inbound_item_signature": "e" * 64,
                    "self_body_relative_path": [0],
                    "inbound_body_relative_path": [0],
                    "sender_relative_path": [1] if kind == "group" else None,
                    "sender_attribute_key": "accessible-id" if kind == "group" else None,
                }
            ],
        }
    )


def item(
    text: str, *, direction: str = "inbound", sender: str | None = "peer"
) -> AtspiTranscriptItem:
    return AtspiTranscriptItem.model_validate(
        {
            "direction": direction,
            "sender_ref": sender,
            "text": text,
            "occurred_at": datetime(2026, 8, 29, tzinfo=UTC),
            "structure_fingerprint": "e" * 64 if direction == "inbound" else "d" * 64,
        }
    )


def snapshot(
    *items: AtspiTranscriptItem, kind: str = "private", generation: int = 1
) -> AtspiSnapshot:
    return AtspiSnapshot(
        target_ref="test_chat",
        chat_kind=kind,  # type: ignore[arg-type]
        header_fingerprint="c" * 64,
        generation=generation,
        items=items,
    )


def test_connector_is_abstract_and_fake_matches_protocol() -> None:
    with pytest.raises(TypeError):
        Connector()
    assert isinstance(FakeConnector(), ConnectorProtocol)


def test_fake_connector_deduplicates_and_delivers_once() -> None:
    async def scenario() -> None:
        event = InboundEvent(
            channel="fake", event_id="evt-1", chat_id="chat-1", sender_id="user-1", text="hi"
        )
        connector = FakeConnector()
        assert await connector.push(event)
        assert not await connector.push(event)
        assert await anext(connector.events()) == event
        outbound = OutboundMessage(channel="fake", chat_id="chat-1", text="hello")
        assert (await connector.deliver(outbound)).status is DeliveryStatus.ACKNOWLEDGED
        assert await connector.deliver(outbound) == await connector.deliver(outbound)

    asyncio.run(scenario())


def test_atspi_observe_baselines_then_emits_only_new_inbound() -> None:
    async def scenario() -> None:
        old = item("old")
        source = SnapshotSource(
            (
                snapshot(old),
                snapshot(
                    old,
                    item("mine", direction="self", sender=None),
                    item("new"),
                    generation=2,
                ),
            )
        )
        connector = AtspiObserveConnector(
            source,
            enrollment(),
            allow_target_refs=frozenset({"test_chat"}),
        )
        events = [event async for event in connector.events()]
        assert [event.text for event in events] == ["new"]
        assert events[0].channel == "wechat_personal_lab"
        assert events[0].event_id.startswith("atspi-v1:test_chat:")
        receipt = await connector.deliver(
            OutboundMessage(channel="wechat_personal_lab", chat_id="test_chat", text="blocked")
        )
        assert receipt.status is DeliveryStatus.FAILED
        assert "observe_only" in (receipt.detail or "")

    asyncio.run(scenario())


def test_atspi_group_without_sender_fails_closed() -> None:
    async def scenario() -> None:
        old = item("old", sender="peer")
        source = SnapshotSource(
            (
                snapshot(old, kind="group"),
                snapshot(old, item("new", sender=None), kind="group", generation=2),
            )
        )
        connector = AtspiObserveConnector(
            source,
            enrollment("group"),
            allow_target_refs=frozenset({"test_chat"}),
        )
        assert [event async for event in connector.events()] == []
        health = await connector.health()
        assert not health.healthy
        assert health.detail == "group_sender_unproven"

    asyncio.run(scenario())


def test_atspi_ambiguous_tail_pauses_instead_of_guessing() -> None:
    async def scenario() -> None:
        repeated = item("same")
        source = SnapshotSource(
            (
                snapshot(repeated),
                snapshot(repeated, repeated, item("new"), generation=2),
            )
        )
        connector = AtspiObserveConnector(
            source,
            enrollment(),
            allow_target_refs=frozenset({"test_chat"}),
        )
        assert [event async for event in connector.events()] == []
        assert (await connector.health()).detail == "transcript_alignment_ambiguous"

    asyncio.run(scenario())


def test_atspi_cursor_survives_restart_and_reset_forces_a_fresh_baseline() -> None:
    async def scenario() -> None:
        old, new = item("old"), item("new")
        cursors: dict[str, AtspiCursor] = {}

        async def load(target_ref: str) -> AtspiCursor | None:
            return cursors.get(target_ref)

        async def save(target_ref: str, cursor: AtspiCursor) -> None:
            cursors[target_ref] = cursor

        first = AtspiObserveConnector(
            SnapshotSource((snapshot(old), snapshot(old, new, generation=2))),
            enrollment(),
            allow_target_refs=frozenset({"test_chat"}),
            load_cursor=load,
            save_cursor=save,
        )
        emitted = [event async for event in first.events()]
        assert [event.text for event in emitted] == ["new"]
        assert "test_chat" in cursors

        restarted = AtspiObserveConnector(
            SnapshotSource((snapshot(old, new, generation=3),)),
            enrollment(),
            allow_target_refs=frozenset({"test_chat"}),
            load_cursor=load,
            save_cursor=save,
        )
        assert [event async for event in restarted.events()] == []

        cursors.clear()  # emergency resume deliberately forgets the old boundary
        resumed = AtspiObserveConnector(
            SnapshotSource((snapshot(old, new, item("during-stop"), generation=4),)),
            enrollment(),
            allow_target_refs=frozenset({"test_chat"}),
            load_cursor=load,
            save_cursor=save,
        )
        assert [event async for event in resumed.events()] == []

    asyncio.run(scenario())
