"""Explicitly bound text conversations over the shared local transport."""

from __future__ import annotations

import asyncio
import json
import re
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
    InboundEvent,
    OutboundMessage,
)


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
            or len(raw["targets"]) != 2
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
        if len({t.binding.chat_id for t in targets}) != 2 or sum(t.is_group for t in targets) != 1:
            raise ValueError
        if next(t.name for t in targets if t.is_group) != "testing":
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
        **kwargs: Any,
    ) -> None:
        super().__init__(http, target.binding, key, **kwargs)
        self.target = target
        self.metadata = metadata
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
        await self.guard()

    async def before_send_guard(self) -> None:
        await self.guard()
        result = await self.http.request(
            "POST",
            "/api/chats/" + quote(self.binding.chat_id, safe="") + "/open?clearUnreads=false",
        )
        if not isinstance(result, dict) or result.get("ok") is not True:
            self.pause("ConversationNavigationFailed")
        await self.readback_guard()

    async def page(self, offset: int = 0) -> list[dict[str, Any]]:
        rows = await super().page(offset)
        if self.target.is_group:
            try:
                response = await self.metadata.get(
                    "/mentions/" + quote(self.binding.chat_id, safe=""),
                    params={"ids": ",".join(str(r["localId"]) for r in rows)},
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("accountVerified") is not True:
                    raise ValueError
                mentions = {}
                for entry in payload["rows"]:
                    if (
                        type(entry.get("localId")) is not int
                        or type(entry.get("mentionedSelf")) is not bool
                    ):
                        raise ValueError
                    if entry["localId"] in mentions:
                        raise ValueError
                    mentions[entry["localId"]] = entry["mentionedSelf"]
                if set(mentions) != {r["localId"] for r in rows}:
                    raise ValueError
                rows = [{**r, "_mentionedSelf": mentions[r["localId"]]} for r in rows]
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                self.pause("MentionMetadataUnavailable")
        return rows

    def event(self, row: dict[str, Any]) -> InboundEvent | None:
        text = row.get("content")
        if row.get("type") != 1 or not isinstance(text, str) or not text.strip():
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
            occurred_at=timestamp,
            metadata={
                "wechat_mode": "conversation",
                "context_scope": scope,
                "is_group": self.target.is_group,
            },
        )

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
            t.binding.chat_ref: ConversationTransport(http, t, key, self.metadata, **kwargs)
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

    async def baseline(self) -> None:
        if self._notification_task is None or self._notification_task.done():
            self._notification_error = None
            self._notification_task = asyncio.create_task(self._listen())
        # Notifications arriving during baseline are retained for the next read.
        if self.resume_requested:
            for child in self.children.values():
                child.paused = None
            self.target_pauses.clear()
        try:
            for ref, child in self.children.items():
                if ref in self.target_pauses:
                    continue
                try:
                    await child.baseline()
                except LabPaused as error:
                    self.target_failure(ref, error)
            if self.resume_requested and self.target_pauses:
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
        # Clear before reading: changes during a read must wake the next pass.
        self._notifications.clear()
        self._next_reconciliation = now + 60
        refs = list(self.children)
        refs = refs[self.rotation :] + refs[: self.rotation]
        self.rotation = (self.rotation + 1) % len(refs)
        events = []
        for ref in refs:
            if ref in self.target_pauses:
                continue
            try:
                events.extend(await self.children[ref].poll())
            except LabPaused as error:
                self.target_failure(ref, error)
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
