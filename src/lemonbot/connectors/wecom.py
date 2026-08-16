"""Official Enterprise WeChat (WeCom) AI Bot connector.

This adapter uses the vendor's WebSocket SDK through a lazy import.  It does
not implement, import, or fall back to client hooks, protocol emulation, or
browser automation.
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import inspect
import logging
import re
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

from lemonbot.domain.models import (
    ConnectorHealth,
    DeliveryReceipt,
    DeliveryStatus,
    EventKind,
    InboundEvent,
    OutboundMessage,
    utc_now,
)
from lemonbot.tools import PinnedHTTPSDownload, pinned_https_get

from ._dedup import BoundedDeduplicator
from .base import Connector
from .errors import ConnectorDependencyError

_STOP = object()
_SENSITIVE_FIELD = re.compile(
    r'(?i)(["\']?(?:secret|aeskey|response_url)["\']?\s*[:=]\s*)'
    r'(["\']?)[^"\'\s,}]+\2'
)
_URL = re.compile(r"https?://[^\s\"']+")

AttachmentSink = Callable[[InboundEvent, bytes, str, str | None], Awaitable[str]]
MediaFetcher = Callable[[str, int], Awaitable[PinnedHTTPSDownload]]
MediaDecryptor = Callable[[bytes, str], bytes]


def _redact_diagnostic(value: object) -> str:
    text = str(value)
    text = _SENSITIVE_FIELD.sub(r"\1[redacted]", text)
    text = _URL.sub("[redacted-url]", text)
    return text[:1_900]


class _RedactingSDKLogger:
    """SDK logger that never emits callback payloads or credential-bearing URLs."""

    def __init__(self) -> None:
        self._logger = logging.getLogger("lemonbot.connectors.wecom.sdk")

    def debug(self, message: str, *args: object) -> None:
        # The upstream SDK debug stream includes complete callback frames.  Do
        # not emit it even after redaction because fields may be added upstream.
        return None

    def info(self, message: str, *args: object) -> None:
        self._logger.info("%s", _redact_diagnostic(message))

    def warn(self, message: str, *args: object) -> None:
        self._logger.warning("%s", _redact_diagnostic(message))

    def error(self, message: str, *args: object) -> None:
        self._logger.error("%s", _redact_diagnostic(message))


@dataclass(frozen=True, slots=True)
class WeComConfig:
    bot_id: str
    secret: str = field(repr=False)
    channel: str = "wecom"
    allowed_chat_ids: frozenset[str] = frozenset()
    welcome_text: str | None = None
    queue_size: int = 1_000
    dedup_capacity: int = 20_000
    reply_frame_capacity: int = 5_000
    receipt_capacity: int = 20_000
    max_text_chars: int = 3_000
    max_media_bytes: int = 10 * 1024 * 1024
    max_media_items: int = 3
    reconnect_interval_ms: int = 1_000
    max_reconnect_attempts: int = 10

    def __post_init__(self) -> None:
        if not self.bot_id.strip():
            raise ValueError("WeCom bot_id must not be empty")
        if not self.secret:
            raise ValueError("WeCom secret must not be empty")
        if not self.channel.strip():
            raise ValueError("WeCom channel must not be empty")
        if self.queue_size < 1 or self.dedup_capacity < 1:
            raise ValueError("WeCom queue and dedup capacities must be positive")
        if (
            self.reply_frame_capacity < 1
            or self.receipt_capacity < 1
            or self.max_text_chars < 1
            or self.max_media_bytes < 1
            or self.max_media_bytes > 50 * 1024 * 1024 - 64
            or self.max_media_items < 0
        ):
            raise ValueError("WeCom reply capacities must be positive")
        if self.welcome_text is not None and not self.welcome_text.strip():
            raise ValueError("welcome_text must be non-empty when configured")


def _nonempty_string(value: object) -> str | None:
    return value if isinstance(value, str) and bool(value.strip()) else None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _timestamp(body: Mapping[str, Any]) -> datetime:
    raw = body.get("create_time", body.get("createtime", body.get("timestamp")))
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.astimezone(UTC)
            raw = float(raw)
        except (TypeError, ValueError):
            return utc_now()
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        seconds = float(raw)
        if seconds > 10_000_000_000:
            seconds /= 1_000
        try:
            return datetime.fromtimestamp(seconds, UTC)
        except (OverflowError, OSError, ValueError):
            pass
    return utc_now()


def _mixed_text(body: Mapping[str, Any]) -> str | None:
    items = _mapping(body.get("mixed")).get("msg_item", ())
    if not isinstance(items, list):
        return None
    parts: list[str] = []
    for item in items:
        item_map = _mapping(item)
        if item_map.get("msgtype") == "text":
            content = _nonempty_string(_mapping(item_map.get("text")).get("content"))
            if content:
                parts.append(content)
    return "\n".join(parts) or None


def _media_parts(body: Mapping[str, Any]) -> tuple[tuple[Mapping[str, Any], str], ...]:
    """Return bounded media descriptors without copying secrets into domain models."""

    message_type = _nonempty_string(body.get("msgtype"))
    if message_type in {"image", "file", "video"}:
        part = _mapping(body.get(message_type))
        return ((part, message_type),) if part else ()
    if message_type != "mixed":
        return ()
    items = _mapping(body.get("mixed")).get("msg_item", ())
    if not isinstance(items, list):
        return ()
    result: list[tuple[Mapping[str, Any], str]] = []
    for raw_item in items:
        item = _mapping(raw_item)
        item_type = _nonempty_string(item.get("msgtype"))
        if item_type in {"image", "file", "video"}:
            part = _mapping(item.get(item_type))
            if part:
                result.append((part, item_type))
    return tuple(result)


def _sniff_media_type(content: bytes, declared_kind: str) -> str:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if declared_kind == "video":
        return "video/mp4"
    return "application/octet-stream"


def map_wecom_frame(
    frame: Mapping[str, Any], *, channel: str = "wecom"
) -> InboundEvent | None:
    """Map a vendor callback to a safe domain event.

    Response URLs, AES keys and the raw callback are deliberately excluded from
    persisted metadata.  Malformed callbacks are ignored rather than assigned a
    fabricated identity that could defeat durable deduplication.
    """

    body = _mapping(frame.get("body"))
    if not body:
        return None
    event_info = _mapping(body.get("event"))
    event_type = _nonempty_string(event_info.get("eventtype"))
    msg_type = _nonempty_string(body.get("msgtype"))
    message_id = _nonempty_string(body.get("msgid"))
    request_id = _nonempty_string(_mapping(frame.get("headers")).get("req_id"))
    if event_type:
        event_id = message_id or (
            f"event:{event_type}:{request_id}" if request_id is not None else None
        )
    else:
        event_id = message_id
    if event_id is None:
        return None

    sender = _mapping(body.get("from"))
    sender_id = (
        _nonempty_string(sender.get("userid"))
        or _nonempty_string(event_info.get("userid"))
    )
    chat_type = _nonempty_string(body.get("chattype")) or "single"
    chat_id = _nonempty_string(body.get("chatid"))
    if chat_type == "single":
        chat_id = chat_id or sender_id
    if sender_id is None or chat_id is None:
        return None

    text: str | None = None
    kind = EventKind.SYSTEM
    if event_type == "enter_chat":
        kind = EventKind.ENTER_CHAT
    elif event_type:
        kind = EventKind.SYSTEM
    elif msg_type == "text":
        text = _nonempty_string(_mapping(body.get("text")).get("content"))
        kind = EventKind.TEXT
    elif msg_type == "voice":
        text = _nonempty_string(_mapping(body.get("voice")).get("content"))
        kind = EventKind.TEXT
    elif msg_type == "mixed":
        text = _mixed_text(body)
        kind = EventKind.TEXT if text is not None else EventKind.IMAGE
    elif msg_type == "image":
        kind = EventKind.IMAGE
    elif msg_type in {"file", "video"}:
        kind = EventKind.FILE
    else:
        return None

    metadata: dict[str, Any] = {
        "vendor": "wecom_aibot",
        "chat_type": chat_type,
        "message_type": msg_type or "event",
    }
    if event_type:
        metadata["event_type"] = event_type
    if msg_type in {"image", "file", "video", "mixed"}:
        metadata["encrypted_media_available"] = True
    if msg_type == "voice":
        metadata["untrusted_vendor_transcription"] = True

    return InboundEvent(
        channel=channel,
        event_id=event_id,
        chat_id=chat_id,
        sender_id=sender_id,
        text=text,
        kind=kind,
        occurred_at=_timestamp(body),
        metadata=metadata,
    )


def _load_official_sdk() -> ModuleType:
    try:
        module = importlib.import_module("aibot")
    except (ImportError, ModuleNotFoundError) as exc:
        raise ConnectorDependencyError(
            "Enterprise WeChat is configured but the official SDK is unavailable. "
            "Install the pinned 'wecom-aibot-python-sdk' optional dependency; "
            "Lemonbot will not fall back to hooks, injection, or protocol emulation."
        ) from exc
    missing = [
        name
        for name in ("WSClient", "WSClientOptions", "generate_req_id")
        if not hasattr(module, name)
    ]
    if missing:
        raise ConnectorDependencyError(
            "The installed 'wecom-aibot-python-sdk' is incompatible; missing: "
            + ", ".join(missing)
        )
    return module


class WeComConnector(Connector):
    """Adapter around the official ``wecom-aibot-python-sdk`` client."""

    def __init__(
        self,
        config: WeComConfig,
        *,
        client: Any | None = None,
        request_id_factory: Callable[[str], str] | None = None,
        attachment_sink: AttachmentSink | None = None,
        media_fetcher: MediaFetcher | None = None,
        media_decryptor: MediaDecryptor | None = None,
    ) -> None:
        self.config = config
        self._client = client
        self._request_id_factory = request_id_factory
        self._attachment_sink = attachment_sink
        self._media_fetcher = media_fetcher
        self._media_decryptor = media_decryptor
        self._queue: asyncio.Queue[InboundEvent | object] = asyncio.Queue(
            maxsize=config.queue_size
        )
        self._seen = BoundedDeduplicator(config.dedup_capacity)
        self._reply_frames: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._receipts: OrderedDict[UUID, DeliveryReceipt] = OrderedDict()
        self._replied_events = BoundedDeduplicator(config.reply_frame_capacity)
        self._started = False
        self._closed = False
        self._connected = False
        self._authenticated = False
        self._last_error: str | None = None
        self._start_lock = asyncio.Lock()

    def _make_client(self) -> None:
        sdk = _load_official_sdk()
        options = sdk.WSClientOptions(
            bot_id=self.config.bot_id,
            secret=self.config.secret,
            reconnect_interval=self.config.reconnect_interval_ms,
            max_reconnect_attempts=self.config.max_reconnect_attempts,
            logger=_RedactingSDKLogger(),
        )
        self._client = sdk.WSClient(options)
        self._request_id_factory = sdk.generate_req_id

    def _register_handlers(self) -> None:
        assert self._client is not None
        self._client.on("connected", self._on_connected)
        self._client.on("authenticated", self._on_authenticated)
        self._client.on("disconnected", self._on_disconnected)
        self._client.on("error", self._on_error)
        self._client.on("message", self._on_message)
        self._client.on("event.enter_chat", self._on_enter_chat)

    async def start(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            if self._closed:
                raise RuntimeError("WeCom connector is closed")
            if self._client is None:
                self._make_client()
            if self._request_id_factory is None:
                self._request_id_factory = lambda prefix: f"{prefix}:{uuid4()}"
            self._register_handlers()
            assert self._client is not None
            self._started = True
            try:
                result = self._client.connect()
                if inspect.isawaitable(result):
                    await result
                self._connected = bool(
                    getattr(self._client, "is_connected", self._connected)
                )
            except Exception as exc:
                self._last_error = _redact_diagnostic(exc)
                self._started = False
                raise

    def _on_connected(self) -> None:
        self._connected = True
        self._last_error = None

    def _on_authenticated(self) -> None:
        self._connected = True
        self._authenticated = True
        self._last_error = None

    def _on_disconnected(self, reason: object = "disconnected") -> None:
        self._connected = False
        self._authenticated = False
        self._last_error = _redact_diagnostic(reason)

    def _on_error(self, error: object) -> None:
        self._last_error = _redact_diagnostic(error)

    async def _on_message(self, frame: Mapping[str, Any]) -> None:
        await self.ingest_frame(frame)

    async def _on_enter_chat(self, frame: Mapping[str, Any]) -> None:
        # A welcome is still an external side effect. Capture the callback
        # token, then let the durable inbox/policy/outbox path decide and send.
        await self.ingest_frame(frame)

    async def ingest_frame(self, frame: Mapping[str, Any]) -> bool:
        """Validate, deduplicate and enqueue a callback from the SDK."""

        event = map_wecom_frame(frame, channel=self.config.channel)
        if event is None or not self._seen.add(event.event_id):
            return False
        allowlisted = event.chat_id in self.config.allowed_chat_ids
        if self._attachment_sink is not None and allowlisted:
            event = await self._materialize_attachments(event, frame)
        metadata = dict(event.metadata)
        metadata["connector_allowlisted"] = allowlisted
        event = event.model_copy(update={"metadata": metadata})
        reply_header = _reply_header(frame) if allowlisted else None
        if reply_header is not None:
            self._reply_frames[event.event_id] = copy.deepcopy(reply_header)
            self._reply_frames.move_to_end(event.event_id)
            if len(self._reply_frames) > self.config.reply_frame_capacity:
                self._reply_frames.popitem(last=False)
        await self._queue.put(event)
        return True

    async def _materialize_attachments(
        self,
        event: InboundEvent,
        frame: Mapping[str, Any],
    ) -> InboundEvent:
        """Download/decrypt media once, then retain only scoped attachment IDs.

        Provider URLs and AES keys exist only in this callback stack.  They are
        never copied to the inbox event, audit trail, or logs.
        """

        parts = _media_parts(_mapping(frame.get("body")))
        if not parts:
            return event
        attachment_sink = self._attachment_sink
        if attachment_sink is None:
            return event
        attachment_ids: list[str] = []
        failures = 0
        if len(parts) > self.config.max_media_items:
            failures += len(parts) - self.config.max_media_items
        for part, declared_kind in parts[: self.config.max_media_items]:
            url = _nonempty_string(part.get("url"))
            aes_key = _nonempty_string(part.get("aeskey"))
            if url is None or aes_key is None or self._client is None:
                failures += 1
                continue
            try:
                if self._media_fetcher is None:
                    downloaded = await pinned_https_get(
                        url,
                        maximum_bytes=self.config.max_media_bytes + 64,
                    )
                else:
                    downloaded = await self._media_fetcher(
                        url,
                        self.config.max_media_bytes + 64,
                    )
                decryptor = self._media_decryptor
                if decryptor is None:
                    sdk = _load_official_sdk()
                    decryptor = sdk.decrypt_file
                content = decryptor(downloaded.content, aes_key)
                filename = downloaded.filename
                if not isinstance(content, bytes) or not content:
                    raise ValueError("empty media payload")
                if len(content) > self.config.max_media_bytes:
                    raise ValueError("media payload exceeds configured limit")
                attachment_id = await attachment_sink(
                    event,
                    content,
                    _sniff_media_type(content, declared_kind),
                    filename if isinstance(filename, str) else None,
                )
                if not attachment_id:
                    raise ValueError("attachment sink returned an empty identifier")
                attachment_ids.append(str(attachment_id))
            except Exception:
                # The callback may contain credentials.  Record only a bounded
                # categorical failure and never stringify the exception.
                failures += 1
        metadata = dict(event.metadata)
        if attachment_ids:
            metadata["attachment_ids"] = attachment_ids
        if failures:
            metadata["attachment_failures"] = failures
        return event.model_copy(update={"metadata": metadata})

    async def events(self) -> AsyncIterator[InboundEvent]:
        await self.start()
        while True:
            item = await self._queue.get()
            if item is _STOP:
                return
            assert isinstance(item, InboundEvent)
            yield item

    async def deliver(self, message: OutboundMessage) -> DeliveryReceipt:
        previous = self._receipts.get(message.message_id)
        if previous is not None:
            return previous
        precondition = self._delivery_precondition(message)
        if precondition is not None:
            return self._remember(
                DeliveryReceipt(
                    message_id=message.message_id,
                    status=DeliveryStatus.FAILED,
                    detail=precondition,
                )
            )
        try:
            await self.start()
        except Exception as exc:
            return self._remember(
                DeliveryReceipt(
                    message_id=message.message_id,
                    status=DeliveryStatus.FAILED,
                    detail="connector did not start: " + _redact_diagnostic(exc),
                )
            )
        assert self._client is not None
        try:
            if message.reply_to_event_id is not None:
                frame = self._reply_frames.get(message.reply_to_event_id)
                if frame is None:
                    return self._remember(
                        DeliveryReceipt(
                            message_id=message.message_id,
                            status=DeliveryStatus.FAILED,
                            detail=(
                                "reply callback is unavailable; refusing to convert "
                                "the reply into a proactive send"
                            ),
                        )
                    )
                if message.reply_to_event_id in self._replied_events:
                    return self._remember(
                        DeliveryReceipt(
                            message_id=message.message_id,
                            status=DeliveryStatus.FAILED,
                            detail="this inbound event already has a final reply",
                        )
                    )
                # Reserve before touching the SDK.  An exception after this point
                # has an unknown side-effect state and must never trigger retry.
                self._replied_events.add(message.reply_to_event_id)
                assert self._request_id_factory is not None
                if message.metadata.get("welcome") is True:
                    ack = await self._client.reply_welcome(
                        frame,
                        {
                            "msgtype": "text",
                            "text": {"content": message.text},
                        },
                    )
                else:
                    ack = await self._client.reply_stream(
                        frame,
                        self._request_id_factory("lemonbot-final"),
                        message.text,
                        True,
                    )
            else:
                ack = await self._client.send_message(
                    message.chat_id,
                    {"msgtype": "markdown", "markdown": {"content": message.text}},
                )
        except Exception as exc:
            return self._remember(
                DeliveryReceipt(
                    message_id=message.message_id,
                    status=DeliveryStatus.UNKNOWN,
                    detail=(
                        "SDK call may have reached WeCom; do not retry automatically: "
                        + _redact_diagnostic(exc)
                    ),
                )
            )
        return self._remember(_receipt_from_ack(message.message_id, ack))

    def _delivery_precondition(self, message: OutboundMessage) -> str | None:
        if message.channel != self.config.channel:
            return (
                f"message channel {message.channel!r} does not match "
                f"connector channel {self.config.channel!r}"
            )
        if self._closed:
            return "WeCom connector is closed"
        if message.chat_id not in self.config.allowed_chat_ids:
            return "target chat is not enrolled in the connector allowlist"
        if len(message.text) > self.config.max_text_chars:
            return f"message exceeds connector limit ({self.config.max_text_chars} chars)"
        return None

    def _remember(self, receipt: DeliveryReceipt) -> DeliveryReceipt:
        self._receipts[receipt.message_id] = receipt
        self._receipts.move_to_end(receipt.message_id)
        if len(self._receipts) > self.config.receipt_capacity:
            self._receipts.popitem(last=False)
        return receipt

    async def health(self) -> ConnectorHealth:
        sdk_connected = bool(
            getattr(self._client, "is_connected", False)
            if self._client is not None
            else False
        )
        connected = self._connected or sdk_connected
        if self._closed:
            detail = "closed"
        elif self._last_error:
            detail = self._last_error
        elif not self._started:
            detail = "not started"
        elif not connected:
            detail = "waiting for WebSocket connection"
        elif not self._authenticated:
            detail = "connected; waiting for authentication"
        else:
            detail = "authenticated"
        return ConnectorHealth(
            healthy=(
                not self._closed
                and connected
                and self._authenticated
                and self._last_error is None
            ),
            detail=detail,
            account_id=self.config.bot_id,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connected = False
        self._authenticated = False
        if self._client is not None and self._started:
            result = self._client.disconnect()
            if inspect.isawaitable(result):
                await result
        await self._queue.put(_STOP)


def _reply_header(frame: Mapping[str, Any]) -> dict[str, Any] | None:
    req_id = _nonempty_string(_mapping(frame.get("headers")).get("req_id"))
    return {"headers": {"req_id": req_id}} if req_id is not None else None


def _receipt_from_ack(message_id: UUID, ack: object) -> DeliveryReceipt:
    if not isinstance(ack, Mapping):
        return DeliveryReceipt(
            message_id=message_id,
            status=DeliveryStatus.UNKNOWN,
            detail="SDK returned a malformed acknowledgement; do not retry automatically",
        )
    errcode = ack.get("errcode")
    external_id = _nonempty_string(_mapping(ack.get("headers")).get("req_id"))
    if external_id is None:
        external_id = _nonempty_string(_mapping(ack.get("body")).get("msgid"))
    if errcode == 0:
        return DeliveryReceipt(
            message_id=message_id,
            status=DeliveryStatus.ACKNOWLEDGED,
            external_id=external_id,
            acknowledged_at=utc_now(),
        )
    if isinstance(errcode, int):
        return DeliveryReceipt(
            message_id=message_id,
            status=DeliveryStatus.FAILED,
            external_id=external_id,
            detail=f"WeCom rejected the message (errcode={errcode})",
        )
    return DeliveryReceipt(
        message_id=message_id,
        status=DeliveryStatus.UNKNOWN,
        external_id=external_id,
        detail="WeCom acknowledgement had no errcode; do not retry automatically",
    )
