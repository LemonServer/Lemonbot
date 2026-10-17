"""Explicitly bound text conversations over the shared local transport."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from lemonbot.connectors.wechat_agent import (
    CHANNEL,
    AgentWechatConnector,
    AgentWechatHTTP,
    Binding,
    LabPaused,
    account_id,
    reference,
    title_candidates,
    unique_testing,
)
from lemonbot.connectors.wechat_agent_state import private_read, private_write
from lemonbot.domain.models import (
    ConnectorHealth,
    DeliveryReceipt,
    DeliveryStatus,
    EventKind,
    InboundEvent,
    OutboundMessage,
)
from lemonbot.latency import measure, record
from lemonbot.memory.unified.archive import ArchiveError, SQLiteMessageArchive
from lemonbot.memory.unified.wechat import snapshot_message
from lemonbot.tools.attachments import AttachmentStore


@dataclass(frozen=True)
class ConversationTarget:
    binding: Binding
    name: str
    is_group: bool


async def enroll_targets(
    http: AgentWechatHTTP, group: Binding, peer: str, key: bytes
) -> tuple[ConversationTarget, ...]:
    """Verify explicit target IDs and their foreground headings before binding."""
    account = account_id(await http.request("GET", "/api/status/auth"))
    if reference(key, "account", account) != group.account_ref:
        raise LabPaused("AccountChanged")
    if await unique_testing(http) != group.chat_id or not peer or peer.endswith("@chatroom"):
        raise LabPaused("InvalidConversationTargets")
    targets = []
    for identifier in (peer, group.chat_id):
        row = await http.request("GET", "/api/chats/" + quote(identifier, safe=""))
        is_group = identifier == group.chat_id
        if (
            not isinstance(row, dict)
            or (row.get("id") or row.get("username")) != identifier
            or row.get("isGroup") is not is_group
            or not isinstance(row.get("name"), str)
            or not row["name"]
            or (is_group and row["name"] != "testing")
        ):
            raise LabPaused("InvalidConversationTargets")
        result = await http.request(
            "POST", "/api/chats/" + quote(identifier, safe="") + "/open?clearUnreads=false"
        )
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise LabPaused("ConversationNavigationFailed")
        suffix = r"(?:\s*\([0-9]+\))?" if is_group else ""
        candidates = title_candidates(
            await http.request("GET", "/api/debug/a11y"),
            re.compile(re.escape(row["name"]) + suffix),
        )
        if len(candidates) != 1:
            raise LabPaused("ConversationTitleAmbiguous")
        path, roles = candidates[0]
        target = ConversationTarget(
            Binding(
                group.account_ref,
                identifier,
                reference(key, "chat", group.account_ref, identifier),
                path,
                roles,
            ),
            row["name"],
            is_group,
        )
        async with httpx.AsyncClient(trust_env=False) as metadata:
            guard = ConversationTransport(http, target, key, metadata)
            await guard.readback_guard()
        targets.append(target)
    return tuple(targets)


def load_targets(path: Path, account_ref: str, key: bytes) -> tuple[ConversationTarget, ...]:
    try:
        raw = json.loads(private_read(path))
        if (
            raw.get("operator_confirmed") is not True
            or raw["account_ref"] != account_ref
            or not 1 <= len(raw["targets"]) <= 20
        ):
            raise ValueError
        targets = []
        for row in raw["targets"]:
            b = row["binding"]
            binding = Binding(
                account_ref=account_ref,
                chat_id=b["chat_id"],
                chat_ref=b["chat_ref"],
                header_path=tuple(b["header_path"]),
                header_roles=tuple(b["header_roles"]),
            )
            if (
                not isinstance(binding.chat_id, str)
                or not binding.chat_id
                or len(binding.chat_id) > 512
            ):
                raise ValueError
            if binding.chat_ref != reference(key, "chat", account_ref, binding.chat_id):
                raise ValueError
            name = row["name"]
            if (
                not isinstance(name, str)
                or not name
                or len(name) > 512
                or type(row["is_group"]) is not bool
            ):
                raise ValueError
            if row["is_group"] != binding.chat_id.endswith("@chatroom"):
                raise ValueError
            if not binding.header_path or not binding.header_roles:
                raise ValueError
            targets.append(ConversationTarget(binding, name, row["is_group"]))
        if len({t.binding.chat_id for t in targets}) != len(targets):
            raise ValueError
        return tuple(targets)
    except (ValueError, KeyError, TypeError):
        raise LabPaused("InvalidConversationBinding") from None


class ConversationTransport(AgentWechatConnector):
    def __init__(
        self,
        http: AgentWechatHTTP,
        target: ConversationTarget,
        key: bytes,
        metadata: httpx.AsyncClient,
        attachments: AttachmentStore | None = None,
        links_enabled: bool = False,
        archive: SQLiteMessageArchive | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(http, target.binding, key, **kwargs)
        self.target = target
        self.metadata = metadata
        self.attachments = attachments
        self.links_enabled = links_enabled
        self.archive = archive
        suffix = r"(?:\s*\([0-9]+\))?" if target.is_group else ""
        self.title = re.compile(re.escape(target.name) + suffix)

    async def guard(self) -> None:
        if self.paused:
            raise LabPaused(self.paused)
        account = account_id(await self.http.request("GET", "/api/status/auth"))
        if reference(self.key, "account", account) != self.binding.account_ref:
            self.pause("AccountChanged")
        chat = await self.http.request("GET", "/api/chats/" + quote(self.binding.chat_id, safe=""))
        if (
            not isinstance(chat, dict)
            or (chat.get("id") or chat.get("username")) != self.binding.chat_id
        ):
            self.pause("ConversationBindingChanged")
        if chat.get("isGroup") is not self.target.is_group or chat.get("name") != self.target.name:
            self.pause("ConversationBindingChanged")

    async def readback_guard(self) -> None:
        await self.guard()
        candidates = title_candidates(await self.http.request("GET", "/api/debug/a11y"), self.title)
        if candidates != [(self.binding.header_path, self.binding.header_roles)]:
            self.pause("ConversationNotForeground")
        await self._unique_title()
        await self.guard()

    async def _unique_title(self) -> None:
        # A matching heading alone cannot disambiguate a duplicate chat name.
        matches: list[dict[str, Any]] = []
        for offset in range(0, 1000, 100):
            rows = await self.http.request("GET", f"/api/chats?limit=100&offset={offset}")
            if not isinstance(rows, list):
                self.pause("InvalidChatList")
            matches.extend(r for r in rows if r.get("name") == self.target.name)
            if len(rows) < 100:
                break
        else:
            self.pause("ChatListLimit")
        if (
            len(matches) != 1
            or (matches[0].get("id") or matches[0].get("username")) != self.binding.chat_id
        ):
            self.pause("ConversationTitleAmbiguous")

    async def before_send_guard(self) -> None:
        await self.guard()
        candidates = title_candidates(await self.http.request("GET", "/api/debug/a11y"), self.title)
        if candidates == [(self.binding.header_path, self.binding.header_roles)]:
            await self._unique_title()
            await self.guard()
            return
        if len(candidates) > 1:
            self.pause("ConversationTitleAmbiguous")
        result = await self.http.request(
            "POST",
            "/api/chats/" + quote(self.binding.chat_id, safe="") + "/open?clearUnreads=false",
        )
        if not isinstance(result, dict) or result.get("ok") is not True:
            self.pause("ConversationNavigationFailed")
        await self.readback_guard()

    async def page(self, offset: int = 0) -> list[dict[str, Any]]:
        try:
            response = await self.metadata.get(
                "/messages/" + quote(self.binding.chat_id, safe=""), params={"offset": offset}
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("accountVerified") is not True:
                raise ValueError
            rows = self.validate_page(payload["rows"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            self.pause("MessageSnapshotUnavailable")
        if self.target.is_group:
            try:
                # Content and source metadata belong to the same validated
                # snapshot; a second query would add latency and a race.
                for row in rows:
                    if type(row.get("mentionedSelf")) is not bool:
                        raise ValueError
                rows = [{**r, "_mentionedSelf": r["mentionedSelf"]} for r in rows]
            except ValueError:
                self.pause("MentionMetadataUnavailable")
        return rows

    def event(self, row: dict[str, Any]) -> InboundEvent | None:
        text = row.get("content")
        is_quote = row.get("type") == 49 and row.get("quoteReply") is True
        is_image = row.get("type") == 3 and self.attachments is not None
        is_link = row.get("type") == 49 and bool(row.get("linkUrl")) and self.links_enabled
        if is_image and self.target.is_group:
            return None
        if is_image:
            text = "[The user sent an image. Respond naturally to its content.]"
        if is_link and not is_quote:
            text = "Shared webpage: " + str(row.get("linkTitle", "")) + "\n" + row["linkUrl"]
        if (
            (row.get("type") != 1 and not is_quote and not is_image and not is_link)
            or not isinstance(text, str)
            or not text.strip()
        ):
            return None
        if type(row.get("isSelf")) is not bool:
            self.pause("DirectionMissing")
        if row["isSelf"]:
            return None
        sender = row.get("sender")
        if not isinstance(sender, str) or not sender or len(sender) > 512:
            self.pause("SenderMissing")
        if self.target.is_group and row.get("_mentionedSelf") is not True:
            return None
        if not self.target.is_group and sender != self.binding.chat_id:
            self.pause("PrivateSenderMismatch")
        quoted = row.get("quotedText") if is_quote else None
        if is_quote:
            if quoted is not None and not isinstance(quoted, str):
                self.pause("InvalidQuotedText")
            text = (
                "This is a quote reply to an earlier message.\nCurrent message:\n"
                + text
                + "\n\nQuoted message (context only):\n"
                + (quoted if quoted is not None else "[Quoted content unavailable or non-text]")
            )
        if is_link and is_quote:
            text += "\nQuoted webpage URL: " + row["linkUrl"]
        media_id = (
            row["localId"]
            if is_image
            else (row.get("quotedImageLocalId") if is_quote and row.get("quotedImage") else None)
        )
        wants_image = is_image or (is_quote and row.get("quotedImage") is True)
        if wants_image and (self.attachments is None or type(media_id) is not int or media_id <= 0):
            text += "\n[Image unavailable: no verified original message or image feature disabled.]"
        if len(text) > 100_000:
            self.pause("TextTooLarge")
        try:
            timestamp = datetime.fromisoformat(row["timestamp"])
            if timestamp.tzinfo is None:
                raise ValueError
        except (KeyError, ValueError, TypeError):
            self.pause("TimestampMissing")
        sender_ref = reference(self.key, "sender", self.binding.account_ref, sender)
        scope = reference(self.key, "context", self.binding.chat_ref, sender_ref)
        return InboundEvent(
            channel=CHANNEL,
            event_id=reference(
                self.key,
                "event",
                self.binding.account_ref,
                self.binding.chat_id,
                str(row["localId"]),
            ),
            chat_id=self.binding.chat_ref,
            sender_id=sender_ref,
            text=text,
            kind=EventKind.IMAGE if is_image else EventKind.TEXT,
            occurred_at=timestamp,
            metadata={
                "wechat_mode": "conversation",
                "context_scope": scope,
                "is_group": self.target.is_group,
                **({"shared_link_url": row["linkUrl"]} if is_link else {}),
                **(
                    {"_media_local_id": media_id}
                    if wants_image
                    and self.attachments is not None
                    and type(media_id) is int
                    and media_id > 0
                    else {}
                ),
                **(
                    {"image_unavailable": True}
                    if wants_image
                    and (self.attachments is None or type(media_id) is not int or media_id <= 0)
                    else {}
                ),
            },
        )

    async def intake_image(self, event: InboundEvent) -> InboundEvent:
        metadata = dict(event.metadata)
        local_id = metadata.pop("_media_local_id", None)
        if local_id is None or self.attachments is None:
            return event.model_copy(update={"metadata": metadata})
        await self.guard()
        failure_code = "ImageDownloadFailed"
        try:
            path = (
                "/media/" + quote(self.binding.chat_id, safe="") + "/" + str(local_id)
            )
            raw = bytearray()
            async with self.metadata.stream("GET", path, timeout=30) as response:
                if response.is_error:
                    error_body = bytearray()
                    async for chunk in response.aiter_bytes():
                        error_body.extend(chunk)
                        if len(error_body) > 4096:
                            break
                    try:
                        code = json.loads(error_body).get("error")
                        if isinstance(code, str) and re.fullmatch(r"Image[A-Za-z]{1,80}", code):
                            failure_code = code
                    except (ValueError, AttributeError):
                        pass
                response.raise_for_status()
                media_type = response.headers.get("content-type", "").split(";", 1)[0]
                if media_type != "application/json":
                    raise ValueError("image bridge did not return a verified envelope")
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > (10 * 1024 * 1024 * 4 // 3 + 4096):
                        raise ValueError("image download exceeds limit")
            failure_code = "ImageEnvelopeInvalid"
            if media_type == "application/json":
                payload = json.loads(raw)
                if (
                    not isinstance(payload, dict)
                    or payload.get("type") != "image"
                    or payload.get("accountVerified") is not True
                    or payload.get("chatId") != self.binding.chat_id
                    or type(payload.get("localId")) is not int
                    or payload["localId"] != local_id
                    or payload.get("source") != "validated_wal_snapshot"
                    or not isinstance(payload.get("account"), str)
                    or reference(self.key, "account", payload["account"])
                    != self.binding.account_ref
                ):
                    raise ValueError("media response is not an image")
                encoded, fmt = payload.get("data"), payload.get("format")
                formats = {
                    "jpeg": "image/jpeg",
                    "jpg": "image/jpeg",
                    "png": "image/png",
                    "webp": "image/webp",
                }
                if (
                    not isinstance(encoded, str)
                    or not isinstance(fmt, str)
                    or fmt.lower() not in formats
                ):
                    raise ValueError("unsupported image envelope")
                raw = bytearray(base64.b64decode(encoded, validate=True))
                media_type = formats[fmt.lower()]
            if not raw or len(raw) > 10 * 1024 * 1024:
                raise ValueError("decoded image exceeds limit")
            await self.guard()
            failure_code = "ImageSaveFailed"
            attachment = await self.attachments.ingest(
                channel=event.channel,
                chat_id=event.chat_id,
                event_id=event.event_id,
                content=bytes(raw),
                media_type=media_type,
            )
            metadata["attachment_ids"] = [str(attachment.attachment_id)]
            metadata["image_source"] = "validated_wal_snapshot"
            metadata["image_source_ref"] = reference(
                self.key, "event", self.binding.account_ref, self.binding.chat_id, str(local_id)
            )
        except LabPaused:
            raise
        except (httpx.HTTPError, ValueError, OSError, RuntimeError, binascii.Error):
            await self.guard()
            metadata.update(
                image_unavailable=True, attachment_failures=1, image_failure_code=failure_code
            )
        return event.model_copy(update={"metadata": metadata})

    async def observe_rows(self, rows: list[dict[str, Any]]) -> None:
        if self.archive is None:
            return
        try:
            messages = [
                snapshot_message(
                    row, binding=self.binding, key=self.key, is_group=self.target.is_group
                )
                for row in rows
            ]
            await self.archive.put_observed_batch(messages)
        except (ArchiveError, ValueError, OSError, sqlite3.Error):
            self.pause("ArchiveSnapshotFailed")

    async def poll(self) -> list[InboundEvent]:
        # A notification can arrive while the two snapshots are being read.
        # Retry only reads, retaining the previous anchors until a stable pass.
        # Never retry model generation, navigation, or the message POST here.
        for attempt in range(3):
            try:
                return await super().poll()
            except LabPaused as error:
                if str(error) != "MessageWindowChanged" or attempt == 2:
                    raise
                self.paused = None
                await asyncio.sleep(0.05)
        raise AssertionError("unreachable")

    def valid_reply(self, message: OutboundMessage) -> bool:
        return bool(message.text.strip()) and len(message.text) <= 2000

    def existing_reply_blocks_send(self) -> bool:
        return False


class ConversationMultiplexer(AgentWechatConnector):
    """One runtime operation lock serializes navigation and shared sending."""

    def __init__(
        self,
        http: AgentWechatHTTP,
        targets: tuple[ConversationTarget, ...],
        key: bytes,
        token: str,
        state: Path,
        attachments: AttachmentStore | None = None,
        links_enabled: bool = False,
        archive: SQLiteMessageArchive | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(http, targets[0].binding, key, **kwargs)
        self.metadata = httpx.AsyncClient(
            base_url="http://127.0.0.1:6175",
            headers={"Authorization": "Bearer " + token},
            trust_env=False,
            timeout=30,
            follow_redirects=False,
        )
        self.children = {
            t.binding.chat_ref: ConversationTransport(
                http,
                t,
                key,
                self.metadata,
                attachments=attachments,
                links_enabled=links_enabled,
                archive=archive,
                **kwargs,
            )
            for t in targets
        }
        self.target_pause_file = state / "targets-paused.json"
        self.target_pauses: dict[str, str] = (
            json.loads(private_read(self.target_pause_file))
            if self.target_pause_file.exists()
            else {}
        )
        if any(
            k not in self.children
            or not isinstance(v, str)
            or not re.fullmatch(r"[A-Za-z0-9:_-]{1,160}", v)
            for k, v in self.target_pauses.items()
        ):
            raise LabPaused("InvalidTargetPauseState")
        for k, v in self.target_pauses.items():
            self.children[k].paused = v
        self.resume_requested = False
        self.chat_refs = tuple(self.children)
        self.rotation = 0
        self._notifications = asyncio.Event()
        self._notification_task: asyncio.Task[None] | None = None
        self._notification_error: str | None = None
        self._next_reconciliation = 0.0
        self._notified_at: float | None = None

    async def _listen(self) -> None:
        """Wait for DB changes, coalescing bursts without reading chat contents."""
        epoch: str | None = None
        sequence = 0
        failures = 0
        while not self.closed:
            try:
                params = {"after": str(sequence), "timeout": "20"}
                if epoch is not None:
                    params["epoch"] = epoch
                response = await self.metadata.get("/changes", params=params)
                response.raise_for_status()
                body = response.json()
                new_epoch, new_sequence = body["epoch"], body["sequence"]
                if (
                    not isinstance(new_epoch, str)
                    or not re.fullmatch(r"[a-f0-9]{32}", new_epoch)
                    or type(new_sequence) is not int
                    or new_sequence < 0
                    or type(body.get("changed")) is not bool
                    or body.get("ready") is not True
                    or (epoch == new_epoch and new_sequence < sequence)
                ):
                    raise ValueError
                if body["changed"] or new_epoch != epoch or new_sequence != sequence:
                    if not self._notifications.is_set():
                        self._notified_at = time.monotonic()
                    self._notifications.set()
                epoch, sequence, failures = new_epoch, new_sequence, 0
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                failures += 1
                if failures >= 3:
                    self._notification_error = "MessageNotificationsUnavailable"
                    self._notifications.set()
                    return
                await asyncio.sleep(1)

    async def wait_for_changes(self) -> None:
        """Called outside the navigation lock so waiting cannot block sending."""
        timeout = max(0.0, self._next_reconciliation - asyncio.get_running_loop().time())
        try:
            await asyncio.wait_for(self._notifications.wait(), timeout=timeout)
        except TimeoutError:
            pass

    def prepare_resume(self) -> None:
        self.resume_requested = True

    def can_process(self, event: InboundEvent) -> bool:
        return (
            not self.paused
            and event.chat_id in self.children
            and event.chat_id not in self.target_pauses
        )

    def target_failure(self, chat_ref: str, error: LabPaused) -> None:
        reason = str(error)
        if reason in {
            "AccountChanged",
            "NotLoggedIn",
            "AccountIdentityMissing",
            "SenderMissing",
            "DirectionMissing",
            "PrivateSenderMismatch",
        }:
            self.pause(reason)
        self.target_pauses[chat_ref] = reason
        private_write(self.target_pause_file, json.dumps(self.target_pauses).encode())

    async def _cycle(self, refs: list[str], *, baseline: bool = False) -> list[InboundEvent]:
        if not refs:
            return []
        try:
            with measure("receive.snapshot_cycle"):
                response = await self.metadata.post(
                    "/read-cycle",
                    json={
                        "anchors": {
                            self.children[ref].binding.chat_id: None
                            if baseline
                            else sorted(self.children[ref].anchors or set())
                            for ref in refs
                        }
                    },
                )
                response.raise_for_status()
                payload = response.json()
            if payload.get("accountVerified") is not True:
                raise ValueError
            if reference(self.key, "account", payload["account"]) != self.binding.account_ref:
                self.pause("AccountChanged")
            targets = payload["targets"]
            expected = {self.children[ref].binding.chat_id: ref for ref in refs}
            if not isinstance(targets, list) or len(targets) != len(expected):
                raise ValueError
            if {t["chatId"] for t in targets} != set(expected):
                raise ValueError
            staged = []
            events = []
            by_chat = {t["chatId"]: t for t in targets}
            for ref in refs:
                child = self.children[ref]
                result = by_chat[child.binding.chat_id]
                if result.get("error") in {
                    "AnchorLost",
                    "MessageTableMissing",
                    "MessageTableAmbiguous",
                }:
                    self.target_failure(ref, LabPaused(result["error"]))
                    continue
                if result.get("error") is not None:
                    raise ValueError
                anchors, rows = result["anchors"], result["rows"]
                if (
                    not isinstance(anchors, list)
                    or len(anchors) > 100
                    or any(type(i) is not int or i <= 0 for i in anchors)
                    or len(set(anchors)) != len(anchors)
                    or not isinstance(rows, list)
                    or len(rows) > 1000
                ):
                    raise ValueError
                for offset in range(0, len(rows), 100):
                    child.validate_page(rows[offset : offset + 100])
                if len({r["localId"] for r in rows}) != len(rows):
                    raise ValueError
                if baseline and rows:
                    raise ValueError
                if child.archive is not None:
                    if child.target.is_group and any(
                        type(row.get("mentionedSelf")) is not bool for row in rows
                    ):
                        raise ValueError
                    try:
                        await child.observe_rows(list(reversed(rows)))
                    except LabPaused as error:
                        self.target_failure(ref, error)
                        continue
                for row in reversed(rows):
                    if child.target.is_group:
                        if type(row.get("mentionedSelf")) is not bool:
                            raise ValueError
                        row = {**row, "_mentionedSelf": row["mentionedSelf"]}
                    try:
                        event = child.event(row)
                    except LabPaused as error:
                        self.target_failure(ref, error)
                        break
                    if event is not None:
                        events.append(await child.intake_image(event))
                else:
                    staged.append((child, set(anchors)))
            for child, anchors in staged:
                child.anchors = anchors
            return events
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            self.pause("MessageSnapshotUnavailable")

    async def baseline(self) -> None:
        if self._notification_task is None or self._notification_task.done():
            self._notification_error = None
            self._notification_task = asyncio.create_task(self._listen())
        if self.resume_requested:
            for child in self.children.values():
                child.paused = None
            self.target_pauses.clear()
        try:
            refs = []
            for ref, child in self.children.items():
                if ref in self.target_pauses:
                    continue
                try:
                    await child.guard()
                    refs.append(ref)
                except LabPaused as error:
                    self.target_failure(ref, error)
            await self._cycle(refs, baseline=True)
            if self.resume_requested and any(
                reason not in {"MessageTableMissing", "MessageTableAmbiguous"}
                for reason in self.target_pauses.values()
            ):
                raise LabPaused("TargetResumeVerificationFailed")
            if len(self.target_pauses) == len(self.children):
                raise LabPaused("AllTargetsPaused")
            private_write(self.target_pause_file, json.dumps(self.target_pauses).encode())
        finally:
            self.resume_requested = False
        self._next_reconciliation = asyncio.get_running_loop().time() + 60

    async def poll(self) -> list[InboundEvent]:
        if self.paused:
            raise LabPaused(self.paused)
        if self._notification_error:
            self.pause(self._notification_error)
        now = asyncio.get_running_loop().time()
        if not self._notifications.is_set() and now < self._next_reconciliation:
            return []
        self._notifications.clear()
        if self._notified_at is not None:
            record("notification.to_read", self._notified_at)
            self._notified_at = None
        self._next_reconciliation = now + 60
        refs = list(self.children)
        refs = refs[self.rotation :] + refs[: self.rotation]
        self.rotation = (self.rotation + 1) % len(refs)
        events = await self._cycle([ref for ref in refs if ref not in self.target_pauses])
        if len(self.target_pauses) == len(self.children):
            self.pause("AllTargetsPaused")
        return events

    async def deliver(self, message: OutboundMessage) -> DeliveryReceipt:
        child = self.children.get(message.chat_id)
        if child is None or message.chat_id in self.target_pauses:
            return DeliveryReceipt(
                message_id=message.message_id,
                status=DeliveryStatus.FAILED,
                detail="TargetPausedOrNotBound",
            )
        try:
            receipt = await child.deliver(message)
        except LabPaused as error:
            self.target_failure(message.chat_id, error)
            return DeliveryReceipt(
                message_id=message.message_id,
                status=DeliveryStatus.FAILED,
                detail="TargetVerificationFailedBeforeSend",
            )
        if receipt.status is DeliveryStatus.UNKNOWN:
            self.paused = child.paused or "UnconfirmedSend"
        return receipt

    async def health(self) -> ConnectorHealth:
        if self.paused or len(self.target_pauses) == len(self.children):
            return ConnectorHealth(healthy=False, detail=self.paused or "AllTargetsPaused")
        for ref, child in self.children.items():
            if ref not in self.target_pauses:
                try:
                    await child.guard()
                except LabPaused as error:
                    self.target_failure(ref, error)
        return ConnectorHealth(
            healthy=not bool(self.paused) and len(self.target_pauses) < len(self.children),
            detail="BoundConversations",
        )

    async def close(self) -> None:
        self.closed = True
        self._notifications.set()
        if self._notification_task is not None:
            self._notification_task.cancel()
            await asyncio.gather(self._notification_task, return_exceptions=True)
        await self.metadata.aclose()
        await self.http.close()
