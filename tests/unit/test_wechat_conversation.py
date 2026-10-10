from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from lemonbot.connectors.wechat_agent import (
    CHANNEL,
    AgentWechatHTTP,
    Binding,
    LabPaused,
    reference,
)
from lemonbot.connectors.wechat_agent_conversation import (
    ConversationMultiplexer,
    ConversationTarget,
)
from lemonbot.domain.models import DeliveryStatus, OutboundMessage
from lemonbot.research.agent_wechat_lab import SimulatedWechat

KEY = b"x" * 32


class TwoChats(SimulatedWechat):
    def __init__(self) -> None:
        super().__init__()
        self.group = self.chat
        self.peer = "synthetic-peer"
        self.names = {self.group: "testing", self.peer: "contact"}
        self.messages: dict[str, list[dict[str, Any]]] = {k: [] for k in self.names}
        self.reads = 0
        self.navigation_failure = False
        self.duplicate_title = False
        self.duplicate_reply = False
        self.inject_on_read = False
        self.changed = asyncio.Event()
        self.sequence = 0

    def add(
        self, chat: str, text: str, *, mention: bool = False, self_message: bool = False
    ) -> dict[str, Any]:
        self.chat = chat
        self.rows = self.messages[chat]
        row = self.append(text, is_self=self_message)
        row["_mentionedSelf"] = mention
        self.sequence += 1
        self.changed.set()
        return row

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/chats":
            data = [{"id": k, "name": v, "isGroup": k == self.group} for k, v in self.names.items()]
            if self.duplicate_title:
                data.append({"id": "different-chat", "name": "testing", "isGroup": True})
            return httpx.Response(200, json=data)
        if path.startswith("/api/chats/"):
            identifier = path.removeprefix("/api/chats/").removesuffix("/open")
            if identifier not in self.names:
                return httpx.Response(404)
            if path.endswith("/open"):
                if not self.navigation_failure:
                    self.title = self.names[identifier]
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(
                200,
                json={
                    "id": identifier,
                    "name": self.names[identifier],
                    "isGroup": identifier == self.group,
                },
            )
        if path == "/api/messages/send":
            body = json.loads(request.content)
            self.chat = body["chatId"]
            self.rows = self.messages[self.chat]
            response = super().handle(request)
            if self.duplicate_reply:
                self.append(body["text"], is_self=True)
            return response
        if path.startswith("/api/messages/"):
            self.reads += 1
            identifier = path.removeprefix("/api/messages/")
            self.rows = self.messages[identifier]
            response = super().handle(request)
            if self.inject_on_read:
                self.inject_on_read = False
                self.add(identifier, "arrival during read", mention=True)
            return response
        return super().handle(request)

    async def metadata(self, request: httpx.Request) -> httpx.Response:
        assert not request.url.path.startswith("/mentions/"), (
            "mentions must share the text snapshot"
        )
        if request.url.path == "/read-cycle":
            anchors = json.loads(request.content)["anchors"]
            targets = []
            for chat, ids in anchors.items():
                self.reads += 1
                rows = sorted(self.messages[chat], key=lambda r: r["localId"], reverse=True)[:1000]
                latest = [r["localId"] for r in rows[:100]]
                overlap = [i for i, r in enumerate(rows) if r["localId"] in (ids or [])]
                error = (
                    "AnchorLost"
                    if ids is not None and not overlap and (ids or len(rows) >= 100)
                    else None
                )
                selected = [] if ids is None else rows[: max(overlap) + 1] if overlap else rows
                targets.append(
                    {
                        "chatId": chat,
                        "anchors": latest,
                        "error": error,
                        "rows": []
                        if error
                        else [
                            {**r, "mentionedSelf": r.get("_mentionedSelf", False)}
                            for r in selected
                            if r["localId"] not in (ids or [])
                        ],
                    }
                )
            if self.inject_on_read:
                self.inject_on_read = False
                self.add(self.group, "arrival during read", mention=True)
            return httpx.Response(
                200,
                json={
                    "accountVerified": True,
                    "account": self.account,
                    "targets": targets,
                },
            )
        if request.url.path.startswith("/messages/"):
            identifier = request.url.path.removeprefix("/messages/")
            offset = request.url.params.get("offset", "0")
            rows = self.handle(
                httpx.Request(
                    "GET",
                    f"http://127.0.0.1:6174/api/messages/{identifier}?limit=100&offset={offset}",
                )
            ).json()
            return httpx.Response(
                200,
                json={
                    "accountVerified": True,
                    "rows": [{**r, "mentionedSelf": r.get("_mentionedSelf", False)} for r in rows],
                },
            )
        if request.url.path == "/changes":
            epoch = "f" * 32
            after = int(request.url.params.get("after", "0"))
            if request.url.params.get("epoch") == epoch and after == self.sequence:
                self.changed.clear()
                await self.changed.wait()
            return httpx.Response(
                200,
                json={"epoch": epoch, "sequence": self.sequence, "changed": True, "ready": True},
            )
        identifier = request.url.path.removeprefix("/mentions/")
        offset = int(request.url.params.get("offset", "0"))
        ids = {int(i) for i in request.url.params.get("ids", "").split(",") if i}
        return httpx.Response(
            200,
            json={
                "accountVerified": True,
                "rows": [
                    {"localId": r["localId"], "mentionedSelf": r.get("_mentionedSelf", False)}
                    for r in self.messages[identifier][offset : offset + 100]
                    if r["localId"] in ids
                ],
            },
        )

    def targets(self) -> tuple[ConversationTarget, ...]:
        account = reference(KEY, "account", self.account)
        return tuple(
            ConversationTarget(
                Binding(
                    account, chat, reference(KEY, "chat", account, chat), (1,), ("frame", "label")
                ),
                name,
                chat == self.group,
            )
            for chat, name in self.names.items()
        )


async def setup(mock: TwoChats, state: Path) -> ConversationMultiplexer:
    transport = ConversationMultiplexer(
        AgentWechatHTTP("test", transport=httpx.MockTransport(mock.handle)),
        mock.targets(),
        KEY,
        "test",
        state,
        readback_seconds=0,
    )
    await transport.metadata.aclose()
    transport.metadata = httpx.AsyncClient(
        base_url="http://127.0.0.1:6175", transport=httpx.MockTransport(mock.metadata)
    )
    for child in transport.children.values():
        child.metadata = transport.metadata
    await transport.baseline()
    await asyncio.sleep(0)
    await transport.poll()  # consume initial notifier wake
    return transport


async def test_idle_does_not_poll_and_notification_reads_both_bound_chats(tmp_path: Path) -> None:
    mock = TwoChats()
    transport = await setup(mock, tmp_path)
    try:
        before = mock.reads
        for _ in range(5):
            assert await transport.poll() == []
        assert mock.reads == before
        mock.add(mock.group, "ordinary group text")
        mock.add(mock.group, "group question", mention=True)
        mock.add(mock.peer, "private question")
        await asyncio.wait_for(transport.wait_for_changes(), timeout=1)
        events = await transport.poll()
        assert sorted(e.text for e in events) == ["group question", "private question"]
        assert len({e.metadata["context_scope"] for e in events}) == 2
        assert await transport.poll() == []
    finally:
        await transport.close()


async def test_missing_atomic_mention_metadata_pauses_group(tmp_path: Path) -> None:
    mock = TwoChats()
    transport = await setup(mock, tmp_path)
    child = next(c for c in transport.children.values() if c.target.is_group)

    async def missing(request: httpx.Request) -> httpx.Response:
        response = await mock.metadata(request)
        payload = response.json()
        for row in payload["rows"]:
            row.pop("mentionedSelf", None)
        return httpx.Response(200, json=payload)

    try:
        mock.add(mock.group, "@self 请记住青柠", mention=True)
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:6175", transport=httpx.MockTransport(missing)
        ) as metadata:
            child.metadata = metadata
            with pytest.raises(LabPaused, match="MentionMetadataUnavailable"):
                await child.page()
        assert mock.posts == 0
    finally:
        await transport.close()


async def test_reconciliation_catches_a_missing_notification_and_restart_baselines(
    tmp_path: Path,
) -> None:
    mock = TwoChats()
    transport = await setup(mock, tmp_path)
    try:
        # Change data without firing a notification.
        mock.chat, mock.rows = mock.peer, mock.messages[mock.peer]
        mock.append("a missed notification")
        assert await transport.poll() == []
        transport._next_reconciliation = 0
        events = await transport.poll()
        assert [e.text for e in events] == ["a missed notification"]
        stable_id = events[0].sender_id
        await transport.close()
        transport = await setup(mock, tmp_path)
        assert await transport.poll() == []
        mock.add(mock.peer, "after restart")
        await asyncio.wait_for(transport.wait_for_changes(), 1)
        assert (await transport.poll())[0].sender_id == stable_id
    finally:
        await transport.close()


async def test_send_navigates_and_accepts_new_same_text_without_body_dedup(tmp_path: Path) -> None:
    mock = TwoChats()
    transport = await setup(mock, tmp_path)
    try:
        for target in mock.targets():
            mock.add(target.binding.chat_id, "same reply", self_message=True)
            message = OutboundMessage(
                channel=CHANNEL, chat_id=target.binding.chat_ref, text="same reply"
            )
            receipt = await transport.deliver(message)
            assert receipt.status is DeliveryStatus.ACKNOWLEDGED
            assert mock.title == target.name
        assert mock.posts == 2
    finally:
        await transport.close()


@pytest.mark.parametrize(
    "fault", ["navigation_failure", "duplicate_title", "duplicate_reply", "send_timeout"]
)
async def test_faults_never_guess_or_resend(tmp_path: Path, fault: str) -> None:
    mock = TwoChats()
    transport = await setup(mock, tmp_path)
    try:
        setattr(mock, fault, True)
        mock.title = "contact"
        target = mock.targets()[0]
        message = OutboundMessage(channel=CHANNEL, chat_id=target.binding.chat_ref, text="reply")
        receipt = await transport.deliver(message)
        if fault in {"navigation_failure", "duplicate_title"}:
            assert receipt.status is DeliveryStatus.FAILED
            assert mock.posts == 0
        else:
            assert receipt.status is DeliveryStatus.UNKNOWN
            assert (await transport.deliver(message)).status is DeliveryStatus.UNKNOWN
            assert mock.posts == 1
    finally:
        await transport.close()


async def test_identity_change_pauses_globally(tmp_path: Path) -> None:
    mock = TwoChats()
    transport = await setup(mock, tmp_path)
    try:
        mock.account = "other-account"
        transport._notifications.set()
        with pytest.raises(LabPaused, match="AccountChanged"):
            await transport.poll()
    finally:
        await transport.close()


async def test_arrival_during_snapshot_retries_only_reads_without_dropping_events(
    tmp_path: Path,
) -> None:
    mock = TwoChats()
    mock.add(mock.group, "old group baseline")
    transport = await setup(mock, tmp_path)
    try:
        child = transport.children[mock.targets()[0].binding.chat_ref]
        mock.add(mock.group, "first arrival", mention=True)
        mock.inject_on_read = True
        events = await child.poll()
        assert [e.text for e in events] == ["first arrival", "arrival during read"]
        assert child.paused is None
        assert mock.posts == 0
        assert await child.poll() == []
    finally:
        await transport.close()
