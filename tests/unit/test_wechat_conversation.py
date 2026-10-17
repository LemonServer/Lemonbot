from __future__ import annotations

import asyncio
import base64
import io
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

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
from lemonbot.tools.attachments import AttachmentStore

KEY = b"x" * 32


class TwoChats(SimulatedWechat):
    def __init__(self) -> None:
        super().__init__()
        self.group = self.chat
        self.peer = "synthetic-peer"
        self.names = {self.group: "testing", self.peer: "contact"}
        self.messages: dict[str, list[dict[str, Any]]] = {k: [] for k in self.names}
        self.reads = 0
        self.opens = 0
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
                self.opens += 1
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


async def test_quote_reply_uses_current_sender_and_current_group_mention(tmp_path: Path) -> None:
    mock = TwoChats()
    transport = await setup(mock, tmp_path)
    try:
        group, peer = mock.targets()
        private = mock.add(mock.peer, "引用")
        private.update(type=49, quoteReply=True, quotedText="earlier text")
        event = transport.children[peer.binding.chat_ref].event(private)
        assert event is not None
        assert event.text == (
            "This is a quote reply to an earlier message.\nCurrent message:\n引用"
            "\n\nQuoted message (context only):\nearlier text"
        )
        assert event.text.endswith("earlier text")
        private["quotedText"] = None
        unavailable = transport.children[peer.binding.chat_ref].event(private)
        assert unavailable is not None
        assert "quote reply" in unavailable.text
        assert unavailable.text.endswith("[Quoted content unavailable or non-text]")
        private["isSelf"] = True
        assert transport.children[peer.binding.chat_ref].event(private) is None
        private.update(isSelf=False, quoteReply=False)
        assert transport.children[peer.binding.chat_ref].event(private) is None
        row = mock.add(mock.group, "reply without real mention")
        row.update(type=49, quoteReply=True, quotedText="@self old quote")
        child = transport.children[group.binding.chat_ref]
        assert child.event(row) is None
        row["_mentionedSelf"] = True
        assert child.event(row) is not None
    finally:
        await transport.close()


async def test_self_quote_with_same_text_is_not_a_plain_text_send_receipt(tmp_path: Path) -> None:
    mock = TwoChats()
    original = mock.handle

    def handle(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.url.path == "/api/messages/send":
            mock.rows[0].update(type=49, quoteReply=True)
        return response

    mock.handle = handle
    transport = await setup(mock, tmp_path)
    try:
        target = mock.targets()[0]
        message = OutboundMessage(
            channel=CHANNEL, chat_id=target.binding.chat_ref, text="same text"
        )
        receipt = await transport.deliver(message)
        assert receipt.status is DeliveryStatus.UNKNOWN
        assert (await transport.deliver(message)).status is DeliveryStatus.UNKNOWN
        assert mock.posts == 1
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


async def test_missing_table_pauses_only_affected_target_and_explicit_resume_keeps_it(tmp_path):
    mock = TwoChats()
    original = mock.metadata
    missing = True

    async def metadata(request):
        response = await original(request)
        if request.url.path == "/read-cycle" and missing:
            body = response.json()
            for target in body["targets"]:
                if target["chatId"] == mock.peer:
                    target.update(error="MessageTableMissing", rows=[], anchors=[])
            return httpx.Response(200, json=body)
        return response

    mock.metadata = metadata
    transport = await setup(mock, tmp_path)
    try:
        group_ref, peer_ref = (t.binding.chat_ref for t in mock.targets())
        assert transport.target_pauses == {peer_ref: "MessageTableMissing"}
        assert transport.children[peer_ref].anchors is None
        mock.add(mock.group, "healthy group", mention=True)
        transport._notifications.set()
        events = await transport.poll()
        assert [e.chat_id for e in events] == [group_ref]
        assert transport.paused is None
        transport.prepare_resume()
        await transport.baseline()
        assert transport.target_pauses == {peer_ref: "MessageTableMissing"}
        missing = False
        assert transport.target_pauses  # Recovery never happens just on poll/restart.
        transport.prepare_resume()
        await transport.baseline()
        assert transport.target_pauses == {}
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


async def test_pending_work_prevents_notification_reads_until_delivery(tmp_path: Path) -> None:
    from lemonbot.connectors.wechat_agent_runtime import RuntimeAgentWechatConnector
    from lemonbot.orchestration import EventPipeline, FakeModelBackend, PipelineStatus
    from lemonbot.policy import DeterministicPolicy, PolicyConfig
    from lemonbot.storage import CoreRepository, Database

    mock = TwoChats()
    transport = await setup(mock, tmp_path)
    db = Database.from_path(tmp_path / "priority.db")
    await db.initialise()
    repo = CoreRepository(db)
    wrapper = RuntimeAgentWechatConnector(transport, repo, tmp_path)
    wrapper.ready = True
    for ref in transport.children:
        await repo.set_allowlisted(CHANNEL, ref)
    pipeline = EventPipeline(
        repo,
        DeterministicPolicy(repo, config=PolicyConfig(quiet_enabled=False)),
        FakeModelBackend(["first answer", "second answer"]),
    )
    stream = wrapper.events()
    pending = None
    try:
        mock.add(mock.group, "first question", mention=True)
        first = await asyncio.wait_for(anext(stream), 2)
        await pipeline.ingest(first)
        mock.add(mock.peer, "second question")
        await asyncio.sleep(0.01)
        before = mock.reads
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.05)
        assert mock.reads == before and not pending.done()
        assert await repo.has_active_inbox(CHANNEL)
        async with wrapper.processing_operation():
            assert (await pipeline.process_once(CHANNEL)).status is PipelineStatus.COMPLETED
        assert await repo.has_active_outbox(CHANNEL)
        await asyncio.sleep(0.05)
        assert mock.reads == before and not pending.done()
        assert (
            await pipeline.dispatch_once(wrapper, channel=CHANNEL)
        ).status is PipelineStatus.ACKNOWLEDGED
        wrapper.work_settled.set()
        second = await asyncio.wait_for(pending, 2)
        assert second.text == "second question"
        assert mock.posts == 1
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()
        await wrapper.close()
        await db.close()


async def test_already_foreground_skips_open_but_still_checks_ambiguity(tmp_path: Path) -> None:
    mock = TwoChats()
    transport = await setup(mock, tmp_path)
    try:
        group = next(t for t in mock.targets() if t.is_group)
        mock.title = group.name
        message = OutboundMessage(channel=CHANNEL, chat_id=group.binding.chat_ref, text="reply")
        assert (await transport.deliver(message)).status is DeliveryStatus.ACKNOWLEDGED
        assert mock.opens == 0 and mock.posts == 1
        mock.duplicate_title = True
        second = OutboundMessage(channel=CHANNEL, chat_id=group.binding.chat_ref, text="next")
        assert (await transport.deliver(second)).status is DeliveryStatus.FAILED
        assert mock.opens == 0 and mock.posts == 1
    finally:
        await transport.close()


async def test_needed_navigation_opens_once_and_verifies_target(tmp_path: Path) -> None:
    mock = TwoChats()
    transport = await setup(mock, tmp_path)
    try:
        private = next(t for t in mock.targets() if not t.is_group)
        mock.title = "testing"
        message = OutboundMessage(channel=CHANNEL, chat_id=private.binding.chat_ref, text="reply")
        assert (await transport.deliver(message)).status is DeliveryStatus.ACKNOWLEDGED
        assert mock.opens == 1 and mock.posts == 1 and mock.title == private.name
    finally:
        await transport.close()


@pytest.mark.parametrize("mismatch", [None, "chatId", "localId", "account", "source"])
async def test_native_media_intake_uses_exact_bound_message_and_group_quote_trigger(
    tmp_path: Path,
    mismatch: str | None,
) -> None:
    mock = TwoChats()
    original = mock.metadata
    stream = io.BytesIO()
    Image.new("RGB", (10, 10), "red").save(stream, format="PNG")
    paths = []

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/media/"):
            paths.append(request.url.path)
            payload = {
                "type": "image", "format": "png",
                "data": base64.b64encode(stream.getvalue()).decode(),
                "accountVerified": True, "account": mock.account,
                "chatId": mock.peer, "localId": int(request.url.path.rsplit("/", 1)[1]),
                "source": "validated_wal_snapshot",
            }
            if mismatch:
                payload[mismatch] = 98765 if mismatch == "localId" else "wrong"
            return httpx.Response(
                200, json=payload,
            )
        return await original(request)

    mock.metadata = handle
    transport = await setup(mock, tmp_path)
    store = AttachmentStore(tmp_path / "a.db", tmp_path / "objects", minimum_free_bytes=0)
    await store.initialize()
    try:
        private_target, group_target = mock.targets()[1], mock.targets()[0]
        peer = transport.children[private_target.binding.chat_ref]
        group = transport.children[group_target.binding.chat_ref]
        peer.attachments = group.attachments = store
        row = mock.add(mock.peer, "picture")
        row.update(type=3, content="")
        event = peer.event(row)
        assert event is not None and event.kind.value == "image"
        event = await peer.intake_image(event)
        if mismatch:
            assert "attachment_ids" not in event.metadata
            assert event.metadata["image_failure_code"] == "ImageEnvelopeInvalid"
        else:
            assert len(event.metadata["attachment_ids"]) == 1
            assert event.metadata["image_source"] == "validated_wal_snapshot"
        assert paths == [f"/media/{mock.peer}/{row['localId']}"]
        assert "_media_local_id" not in event.metadata
        row = mock.add(mock.group, "picture", mention=True)
        row.update(type=3)
        assert group.event(row) is None
        quote = mock.add(mock.group, "look", mention=False)
        quote.update(type=49, quoteReply=True, quotedImage=True, quotedImageLocalId=row["localId"])
        assert group.event(quote) is None
        quote["_mentionedSelf"] = True
        event = group.event(quote)
        assert event is not None and event.metadata["_media_local_id"] == row["localId"]
        quote["quotedImageLocalId"] = None
        event = group.event(quote)
        assert event is not None and event.metadata["image_unavailable"] is True
    finally:
        await transport.close()
