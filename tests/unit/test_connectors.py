from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from lemonbot.connectors import (
    Connector,
    ConnectorDependencyError,
    ConnectorDisabledError,
    FakeConnector,
    PersonalWeChatConfig,
    PersonalWeChatConnector,
    PersonalWeChatStage,
    UIASendAttempt,
    UIASnapshot,
    WeComConfig,
    WeComConnector,
    map_wecom_frame,
)
from lemonbot.domain.models import (
    DeliveryStatus,
    EventKind,
    InboundEvent,
    OutboundMessage,
)
from lemonbot.domain.protocols import Connector as ConnectorProtocol
from lemonbot.tools import PinnedHTTPSDownload


def _load_fixture_module() -> ModuleType:
    path = Path(__file__).parents[1] / "fixtures" / "wecom_events.py"
    spec = importlib.util.spec_from_file_location("lemonbot_wecom_fixtures", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WECOM = _load_fixture_module()


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def _credential() -> str:
    return "synthetic-credential-for-tests"


def test_connector_is_abstract_and_fake_matches_structural_protocol() -> None:
    with pytest.raises(TypeError):
        Connector()
    fake = FakeConnector()
    assert isinstance(fake, ConnectorProtocol)


def test_fake_connector_deduplicates_events_and_delivery_ids() -> None:
    async def scenario() -> None:
        event = InboundEvent(
            channel="fake",
            event_id="evt-1",
            chat_id="chat-1",
            sender_id="user-1",
            text="hello",
        )
        connector = FakeConnector()
        assert await connector.push(event)
        assert not await connector.push(event)
        stream = connector.events()
        assert (await anext(stream)) == event

        message = OutboundMessage(channel="fake", chat_id="chat-1", text="world")
        first = await connector.deliver(message)
        second = await connector.deliver(message)
        assert first == second
        assert first.status is DeliveryStatus.ACKNOWLEDGED
        assert connector.delivered_messages == (message,)
        await connector.close()

    run(scenario())


def test_map_wecom_direct_group_and_media_without_sensitive_metadata() -> None:
    direct = map_wecom_frame(WECOM.direct_text_frame())
    assert direct is not None
    assert direct.chat_id == "user-alice"
    assert direct.sender_id == "user-alice"
    assert direct.text == "你好, Lemonbot"
    assert direct.kind is EventKind.TEXT

    group = map_wecom_frame(WECOM.group_text_frame())
    assert group is not None
    assert group.chat_id == "group-stable-42"
    assert group.sender_id == "user-bob"

    image = map_wecom_frame(WECOM.image_frame())
    assert image is not None and image.kind is EventKind.IMAGE
    serialized = image.model_dump_json()
    assert "fixture-aes-key" not in serialized
    assert "response_url" not in serialized
    assert "example.invalid" not in serialized


def test_map_wecom_rejects_callbacks_without_stable_identity() -> None:
    frame = WECOM.direct_text_frame()
    frame["body"].pop("msgid")
    assert map_wecom_frame(frame) is None


class _FakeOfficialSDKClient:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}
        self.is_connected = False
        self.reply_calls: list[tuple[Any, ...]] = []
        self.send_calls: list[tuple[Any, ...]] = []
        self.welcome_calls: list[tuple[Any, ...]] = []
        self.download_calls: list[tuple[str, str]] = []

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler

    async def connect(self) -> None:
        self.is_connected = True
        self.handlers["connected"]()
        self.handlers["authenticated"]()

    def disconnect(self) -> None:
        self.is_connected = False

    async def reply_stream(self, *args: Any) -> dict[str, Any]:
        self.reply_calls.append(args)
        return {"errcode": 0, "headers": {"req_id": "ack-reply"}}

    async def send_message(self, *args: Any) -> dict[str, Any]:
        self.send_calls.append(args)
        return {"errcode": 0, "headers": {"req_id": "ack-proactive"}}

    async def reply_welcome(self, *args: Any) -> dict[str, Any]:
        self.welcome_calls.append(args)
        return {"errcode": 0}

    async def download_file(self, url: str, aes_key: str) -> tuple[bytes, str]:
        self.download_calls.append((url, aes_key))
        return b"\x89PNG\r\n\x1a\nsynthetic", "picture.png"


def test_wecom_dedup_and_one_shot_final_reply() -> None:
    async def scenario() -> None:
        sdk = _FakeOfficialSDKClient()
        connector = WeComConnector(
            WeComConfig(
                bot_id="bot",
                secret=_credential(),
                allowed_chat_ids=frozenset({"user-alice"}),
            ),
            client=sdk,
            request_id_factory=lambda prefix: f"{prefix}:fixed",
        )
        await connector.start()
        frame = WECOM.direct_text_frame()
        assert await connector.ingest_frame(frame)
        assert not await connector.ingest_frame(WECOM.duplicate(frame))
        event = await anext(connector.events())

        message = OutboundMessage(
            channel="wecom",
            chat_id=event.chat_id,
            text="最终回复",
            reply_to_event_id=event.event_id,
        )
        receipt = await connector.deliver(message)
        assert receipt.status is DeliveryStatus.ACKNOWLEDGED
        assert len(sdk.reply_calls) == 1
        _, stream_id, content, finish = sdk.reply_calls[0]
        assert stream_id == "lemonbot-final:fixed"
        assert content == "最终回复"
        assert finish is True

        # The durable outbox may ask for the same message again; the adapter
        # returns the stored receipt without touching the network.
        assert await connector.deliver(message) == receipt
        assert len(sdk.reply_calls) == 1

        second = OutboundMessage(
            channel="wecom",
            chat_id=event.chat_id,
            text="不应发送第二次",
            reply_to_event_id=event.event_id,
        )
        assert (await connector.deliver(second)).status is DeliveryStatus.FAILED
        assert len(sdk.reply_calls) == 1
        await connector.close()

    run(scenario())


def test_wecom_proactive_send_and_welcome_callback_has_no_direct_side_effect() -> None:
    async def scenario() -> None:
        sdk = _FakeOfficialSDKClient()
        connector = WeComConnector(
            WeComConfig(
                bot_id="bot",
                secret=_credential(),
                allowed_chat_ids=frozenset({"user-alice"}),
                welcome_text="我是 AI 助手",
            ),
            client=sdk,
        )
        await connector.start()
        await sdk.handlers["event.enter_chat"](WECOM.enter_chat_frame())
        assert not sdk.welcome_calls
        welcome = OutboundMessage(
            channel="wecom",
            chat_id="user-alice",
            text="我是 AI 助手",
            reply_to_event_id="event:enter_chat:callback-enter-001",
            metadata={"welcome": True},
        )
        assert (await connector.deliver(welcome)).status is DeliveryStatus.ACKNOWLEDGED
        assert len(sdk.welcome_calls) == 1
        proactive = OutboundMessage(
            channel="wecom", chat_id="user-alice", text="已订阅的提醒"
        )
        receipt = await connector.deliver(proactive)
        assert receipt.status is DeliveryStatus.ACKNOWLEDGED
        assert sdk.send_calls == [
            (
                "user-alice",
                {
                    "msgtype": "markdown",
                    "markdown": {"content": "已订阅的提醒"},
                },
            )
        ]
        await connector.close()

    run(scenario())


def test_wecom_media_is_materialized_to_scoped_ids_without_persisting_credentials() -> None:
    async def scenario() -> None:
        sdk = _FakeOfficialSDKClient()
        received: list[tuple[str, bytes, str, str | None]] = []
        fetched: list[tuple[str, int]] = []

        async def fetch(url: str, maximum_bytes: int) -> PinnedHTTPSDownload:
            fetched.append((url, maximum_bytes))
            return PinnedHTTPSDownload(
                content=b"encrypted-test-payload",
                content_type="application/octet-stream",
                filename="picture.png",
            )

        async def sink(
            event: InboundEvent,
            content: bytes,
            media_type: str,
            filename: str | None,
        ) -> str:
            received.append((event.event_id, content, media_type, filename))
            return "00000000-0000-0000-0000-000000000123"

        connector = WeComConnector(
            WeComConfig(
                bot_id="bot",
                secret=_credential(),
                allowed_chat_ids=frozenset({"user-alice"}),
            ),
            client=sdk,
            attachment_sink=sink,
            media_fetcher=fetch,
            media_decryptor=lambda _content, _key: b"\x89PNG\r\n\x1a\nsynthetic",
        )
        await connector.start()
        assert await connector.ingest_frame(WECOM.image_frame())
        event = await anext(connector.events())
        assert event.metadata["attachment_ids"] == [
            "00000000-0000-0000-0000-000000000123"
        ]
        assert received == [
            (
                "msg-image-001",
                b"\x89PNG\r\n\x1a\nsynthetic",
                "image/png",
                "picture.png",
            )
        ]
        assert fetched == [
            (
                "https://example.invalid/encrypted/image",
                10 * 1024 * 1024 + 64,
            )
        ]
        serialized = event.model_dump_json()
        assert "fixture-aes-key" not in serialized
        assert "example.invalid" not in serialized
        await connector.close()

    run(scenario())


def test_wecom_non_allowlisted_media_and_welcome_have_no_connector_side_effect() -> None:
    async def scenario() -> None:
        sdk = _FakeOfficialSDKClient()
        fetched = False

        async def fetch(_url: str, _maximum: int) -> PinnedHTTPSDownload:
            nonlocal fetched
            fetched = True
            raise AssertionError("non-allowlisted media must not be fetched")

        async def sink(
            _event: InboundEvent,
            _content: bytes,
            _media_type: str,
            _filename: str | None,
        ) -> str:
            raise AssertionError("non-allowlisted media must not be stored")

        connector = WeComConnector(
            WeComConfig(
                bot_id="bot",
                secret=_credential(),
                allowed_chat_ids=frozenset({"someone-else"}),
                welcome_text="fixed welcome",
            ),
            client=sdk,
            attachment_sink=sink,
            media_fetcher=fetch,
        )
        await connector.start()
        assert await connector.ingest_frame(WECOM.image_frame())
        image_event = await anext(connector.events())
        assert image_event.metadata["connector_allowlisted"] is False
        assert not fetched
        await sdk.handlers["event.enter_chat"](WECOM.enter_chat_frame())
        assert not sdk.welcome_calls
        await connector.close()

    run(scenario())


def test_wecom_missing_official_sdk_has_clear_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        import lemonbot.connectors.wecom as module

        def missing(name: str) -> ModuleType:
            raise ModuleNotFoundError(name)

        monkeypatch.setattr(module.importlib, "import_module", missing)
        connector = WeComConnector(
            WeComConfig(bot_id="bot", secret=_credential())
        )
        with pytest.raises(ConnectorDependencyError, match="official SDK"):
            await connector.start()

    run(scenario())


class _FakeUIABackend:
    def __init__(self, events: list[InboundEvent]) -> None:
        self.inbound = events
        self.sent: list[tuple[str, str]] = []
        self.signature = "selectors-v1"
        self.executable_path = r"C:\Program Files\Tencent\WeChat\WeChat.exe"
        self.executable_sha256 = "c" * 64
        self.readback_override: str | None = None
        self.inspect_calls = 0
        self.prepare_calls = 0

    def events(self) -> AsyncIterator[InboundEvent]:
        async def generate() -> AsyncIterator[InboundEvent]:
            for event in self.inbound:
                yield event

        return generate()

    def snapshot(self, target: str | None = None) -> UIASnapshot:
        return UIASnapshot(
            windows_user="vm-user",
            session_locked=False,
            process_name="WeChat.exe",
            process_count=1,
            account_id="lab-account",
            client_version="4.1.0-test",
            window_handle=42,
            selector_signature=self.signature,
            executable_path=self.executable_path,
            executable_sha256=self.executable_sha256,
            target_chat_id=target,
            target_match_count=1 if target else 0,
        )

    async def inspect(self) -> UIASnapshot:
        self.inspect_calls += 1
        return self.snapshot()

    async def prepare_target(self, chat_id: str) -> UIASnapshot:
        self.prepare_calls += 1
        return self.snapshot(chat_id)

    async def send_text(self, chat_id: str, text: str) -> UIASendAttempt:
        self.sent.append((chat_id, text))
        return UIASendAttempt(
            attempted=True,
            post_snapshot=self.snapshot(chat_id),
            readback_text=self.readback_override or text,
            external_id="uia-readback-1",
        )

    async def close(self) -> None:
        return None


def _personal_config(stage: PersonalWeChatStage) -> PersonalWeChatConfig:
    return PersonalWeChatConfig(
        enabled=True,
        stage=stage,
        expected_executable_path=r"C:\Program Files\Tencent\WeChat\WeChat.exe",
        expected_executable_sha256="c" * 64,
        expected_windows_user="vm-user",
        expected_account_id="lab-account",
        enrolled_client_version="4.1.0-test",
        enrolled_selector_signature="selectors-v1",
        allowed_chat_ids=frozenset({"chat-stable-1"}),
    )


def test_personal_wechat_is_disabled_by_default() -> None:
    async def scenario() -> None:
        connector = PersonalWeChatConnector()
        health = await connector.health()
        assert health.healthy
        assert "disabled by default" in (health.detail or "")
        with pytest.raises(ConnectorDisabledError):
            await anext(connector.events())

    run(scenario())


def test_personal_wechat_reply_stage_requires_observed_allowlisted_event() -> None:
    async def scenario() -> None:
        inbound = InboundEvent(
            channel="wechat_personal_lab",
            event_id="uia-event-1",
            chat_id="chat-stable-1",
            sender_id="contact-stable-1",
            text="在吗?",
        )
        backend = _FakeUIABackend([inbound])
        connector = PersonalWeChatConnector(
            _personal_config(PersonalWeChatStage.REPLY),
            backend=backend,
            platform_name="Windows",
        )
        assert await anext(connector.events()) == inbound
        reply = OutboundMessage(
            channel="wechat_personal_lab",
            chat_id=inbound.chat_id,
            text="在的, 我是 AI 助手。",
            reply_to_event_id=inbound.event_id,
        )
        assert (await connector.deliver(reply)).status is DeliveryStatus.ACKNOWLEDGED
        assert backend.sent == [(inbound.chat_id, reply.text)]

        proactive = OutboundMessage(
            channel="wechat_personal_lab",
            chat_id=inbound.chat_id,
            text="不应主动发送",
        )
        assert (await connector.deliver(proactive)).status is DeliveryStatus.FAILED
        assert len(backend.sent) == 1

    run(scenario())


def test_personal_wechat_selector_drift_and_readback_uncertainty_fail_closed() -> None:
    async def scenario() -> None:
        inbound = InboundEvent(
            channel="wechat_personal_lab",
            event_id="uia-event-2",
            chat_id="chat-stable-1",
            sender_id="contact-stable-1",
            text="测试",
        )
        backend = _FakeUIABackend([inbound])
        connector = PersonalWeChatConnector(
            _personal_config(PersonalWeChatStage.PROACTIVE),
            backend=backend,
            platform_name="Windows",
        )
        backend.signature = "selectors-drifted"
        blocked = await connector.deliver(
            OutboundMessage(
                channel="wechat_personal_lab",
                chat_id="chat-stable-1",
                text="不会发送",
            )
        )
        assert blocked.status is DeliveryStatus.FAILED
        assert not backend.sent
        assert backend.inspect_calls == 1
        assert backend.prepare_calls == 0

        backend.signature = "selectors-v1"
        backend.readback_override = "无法确认的其他文本"
        uncertain = await connector.deliver(
            OutboundMessage(
                channel="wechat_personal_lab",
                chat_id="chat-stable-1",
                text="只尝试一次",
            )
        )
        assert uncertain.status is DeliveryStatus.UNKNOWN
        assert backend.sent == [("chat-stable-1", "只尝试一次")]

    run(scenario())


def test_personal_wechat_executable_drift_blocks_before_target_navigation() -> None:
    async def scenario() -> None:
        backend = _FakeUIABackend([])
        backend.executable_sha256 = "d" * 64
        connector = PersonalWeChatConnector(
            _personal_config(PersonalWeChatStage.PROACTIVE),
            backend=backend,
            platform_name="Windows",
        )

        receipt = await connector.deliver(
            OutboundMessage(
                channel="wechat_personal_lab",
                chat_id="chat-stable-1",
                text="不会进入会话搜索",
            )
        )

        assert receipt.status is DeliveryStatus.FAILED
        assert backend.inspect_calls == 1
        assert backend.prepare_calls == 0
        assert backend.sent == []

    run(scenario())
