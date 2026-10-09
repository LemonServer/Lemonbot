"""Foreground-only, canary-only agent-wechat HTTP transport."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NoReturn, TypeGuard, cast
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

import httpx

from lemonbot.connectors.base import Connector
from lemonbot.domain.models import (
    ConnectorHealth,
    DeliveryReceipt,
    DeliveryStatus,
    InboundEvent,
    OutboundMessage,
    utc_now,
)

CHANNEL = "wechat_agent_lab"
API_URL = "http://127.0.0.1:6174"
COMMAND = re.compile(r"LB26_E2E_([A-F0-9]{24})")
ACK = re.compile(r"LB26_ACK_([A-F0-9]{24})")
TITLE = re.compile(r"testing(?:\s*\([0-9]+\))?")
PAGE_SIZE = 100
MAX_PAGES = 10


class LabPaused(RuntimeError):
    """Contains a fixed diagnostic code, never upstream response text."""


class AgentWechatHTTP:
    def __init__(self, token: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not token or any(c.isspace() for c in token):
            raise LabPaused("InvalidToken")
        self.client = httpx.AsyncClient(
            base_url=API_URL,
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=10,
        )
        self.post_count = 0
        self.last_send_seconds = 0.0

    async def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        sending = method == "POST" and path == "/api/messages/send"
        # The upstream send endpoint waits for its UI execution plan to finish.
        # Use the same bounded wait as the historical reproduction client. A
        # timeout still means UNKNOWN and must never cause a second POST.
        timeout = httpx.Timeout(25, connect=10) if sending else httpx.Timeout(10)
        stage = "Send" if sending else "Read"
        started = asyncio.get_running_loop().time()
        if sending:
            self.post_count += 1
        try:
            response = await self.client.request(method, path, json=body, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except httpx.TimeoutException:
            raise LabPaused(f"APIUnavailableOrInvalid:{stage}:Timeout") from None
        except httpx.HTTPStatusError as error:
            raise LabPaused(
                f"APIUnavailableOrInvalid:{stage}:HTTP{error.response.status_code}"
            ) from None
        except httpx.HTTPError:
            raise LabPaused(f"APIUnavailableOrInvalid:{stage}:Transport") from None
        except ValueError:
            raise LabPaused(f"APIUnavailableOrInvalid:{stage}:JSON") from None
        finally:
            if sending:
                self.last_send_seconds = asyncio.get_running_loop().time() - started

    async def close(self) -> None:
        await self.client.aclose()


def reference(key: bytes, kind: str, *parts: str) -> str:
    payload = json.dumps([kind, *parts], separators=(",", ":")).encode()
    return kind + "_" + hmac.new(key, payload, hashlib.sha256).hexdigest()


def account_id(auth: Any) -> str:
    if not isinstance(auth, dict) or auth.get("status") != "logged_in":
        raise LabPaused("NotLoggedIn")
    account = auth.get("loggedInUser")
    if not isinstance(account, str) or not account or len(account) > 512:
        raise LabPaused("AccountIdentityMissing")
    return account


async def unique_testing(http: AgentWechatHTTP) -> str:
    matches = set()
    for page in range(MAX_PAGES):
        rows = await http.request("GET", f"/api/chats?limit={PAGE_SIZE}&offset={page * PAGE_SIZE}")
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
            raise LabPaused("InvalidChatList")
        for row in rows:
            if not isinstance(row, dict):
                raise LabPaused("InvalidChatList")
            if row.get("name") == "testing":
                identifier = row.get("id") or row.get("username")
                if isinstance(identifier, str) and identifier.endswith("@chatroom"):
                    matches.add(identifier)
        if len(rows) < PAGE_SIZE:
            break
    else:
        raise LabPaused("ChatListLimit")
    if len(matches) != 1:
        raise LabPaused("TestingNotUnique")
    return matches.pop()


def title_candidates(
    payload: Any, title_pattern: re.Pattern[str] = TITLE
) -> list[tuple[tuple[int, ...], tuple[str, ...]]]:
    """Enroll only a title above/right of the fixed client's semantic Chats list."""
    if not isinstance(payload, dict) or not isinstance(payload.get("tree"), dict):
        raise LabPaused("InvalidUITree")
    nodes: list[tuple[dict[str, Any], tuple[int, ...], tuple[str, ...]]] = []
    blocked = {
        "list",
        "list item",
        "list-item",
        "tree",
        "tree item",
        "tree-item",
        "table",
        "menu",
        "dialog",
    }

    def visit(node: Any, path: tuple[int, ...], roles: tuple[str, ...]) -> None:
        if len(nodes) >= 5000 or len(path) > 64 or not isinstance(node, dict):
            raise LabPaused("InvalidUITree")
        role = node.get("role")
        states = node.get("states", [])
        if not isinstance(role, str) or not isinstance(states, list):
            raise LabPaused("InvalidUITree")
        role = role.lower()
        lineage = (*roles, role)
        if role in {"dialog", "alert", "popup menu", "popup-menu"}:
            raise LabPaused("PopupPresent")
        if any(
            s.lower() in {"hidden", "invisible", "defunct"} for s in states if isinstance(s, str)
        ):
            return
        nodes.append((node, path, lineage))
        children = node.get("children") or []
        if not isinstance(children, list):
            raise LabPaused("InvalidUITree")
        for i, child in enumerate(children):
            visit(child, (*path, i), lineage)

    visit(payload["tree"], (), ())
    by_path = {path: node for node, path, _ in nodes}

    def frame_for(path: tuple[int, ...]) -> tuple[int, ...] | None:
        for depth in range(len(path), -1, -1):
            ancestor = path[:depth]
            if by_path[ancestor]["role"].lower() == "frame":
                return ancestor
        return None

    def valid_bounds(bounds: Any) -> TypeGuard[dict[str, Any]]:
        return (
            isinstance(bounds, dict)
            and all(
                type(bounds.get(k)) in {int, float} and math.isfinite(bounds[k])
                for k in ("x", "y", "width", "height")
            )
            and bounds["width"] > 0
            and bounds["height"] > 0
        )

    chat_lists = [
        (n, path) for n, path, roles in nodes if roles[-1] == "list" and n.get("name") == "Chats"
    ]
    if len(chat_lists) != 1:
        raise LabPaused("ChatListStructureMissing")
    chat_list, chat_path = chat_lists[0]
    list_bounds = chat_list.get("bounds")
    if not valid_bounds(list_bounds):
        raise LabPaused("ChatListStructureMissing")
    frame_path = frame_for(chat_path)
    if frame_path is None:
        raise LabPaused("FrameStructureMissing")
    frame_bounds = by_path[frame_path].get("bounds")
    if not valid_bounds(frame_bounds):
        raise LabPaused("FrameStructureMissing")
    list_right = list_bounds["x"] + list_bounds["width"]
    frame_right = frame_bounds["x"] + frame_bounds["width"]
    frame_bottom = frame_bounds["y"] + frame_bounds["height"]
    if not (
        frame_bounds["x"] <= list_bounds["x"] < list_right <= frame_right
        and frame_bounds["y"] <= list_bounds["y"]
        and list_bounds["y"] + list_bounds["height"] <= frame_bottom
    ):
        raise LabPaused("ChatListStructureMissing")
    found = []
    for node, path, roles in nodes:
        bounds = node.get("bounds")
        if (
            roles[-1] == "label"
            and not blocked.intersection(roles[:-1])
            and frame_for(path) == frame_path
            and isinstance(node.get("name"), str)
            and title_pattern.fullmatch(node["name"])
            and isinstance(bounds, dict)
            and valid_bounds(bounds)
            and list_right < bounds["x"]
            and bounds["x"] + bounds["width"] <= frame_right
            and 0 <= bounds["y"] - frame_bounds["y"] < 70
            and bounds["y"] + bounds["height"] <= min(frame_bounds["y"] + 70, frame_bottom)
        ):
            found.append((path, roles))

    # Qt exposes the same heading on a label wrapper and its child. Collapse
    # only testing titles (with an optional member count) in a nested, contained
    # label; siblings remain
    # ambiguous and cannot authorize a send.
    def contains_duplicate(parent: tuple[int, ...], child: tuple[int, ...]) -> bool:
        if len(child) <= len(parent) or child[: len(parent)] != parent:
            return False
        outer, inner = by_path[parent], by_path[child]
        a, b = outer["bounds"], inner["bounds"]
        return bool(
            title_pattern.fullmatch(outer["name"])
            and title_pattern.fullmatch(inner["name"])
            and a["x"] <= b["x"]
            and a["y"] <= b["y"]
            and b["x"] + b["width"] <= a["x"] + a["width"]
            and b["y"] + b["height"] <= a["y"] + a["height"]
        )

    return [
        (path, roles)
        for path, roles in found
        if not any(contains_duplicate(path, child) for child, _ in found)
    ]


@dataclass(frozen=True)
class Binding:
    account_ref: str
    chat_id: str
    chat_ref: str
    header_path: tuple[int, ...]
    header_roles: tuple[str, ...]


async def enroll(http: AgentWechatHTTP, key: bytes) -> Binding:
    account = account_id(await http.request("GET", "/api/status/auth"))
    chat = await unique_testing(http)
    candidates = title_candidates(await http.request("GET", "/api/debug/a11y"))
    if len(candidates) != 1:
        raise LabPaused("TestingHeaderNotUnique")
    if account_id(await http.request("GET", "/api/status/auth")) != account:
        raise LabPaused("AccountChanged")
    path, roles = candidates[0]
    return Binding(
        reference(key, "account", account),
        chat,
        reference(key, "chat", account, chat),
        path,
        roles,
    )


class AgentWechatConnector(Connector):
    """Bounded polling with a fresh startup baseline and explicit pause on drift."""

    channel = CHANNEL

    def __init__(
        self,
        http: AgentWechatHTTP,
        binding: Binding,
        key: bytes,
        *,
        poll_seconds: float = 2,
        readback_seconds: float = 10,
        run_id: str = "unassigned",
        diagnostic: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.http = http
        self.binding = binding
        self.key = key
        self.poll_seconds = poll_seconds
        self.readback_seconds = readback_seconds
        self.paused: str | None = None
        self.anchors: set[int] | None = None
        self.closed = False
        self.attempts: dict[str, DeliveryReceipt] = {}
        self.reply_texts: dict[str, str] = {}
        self.delivery_lock = asyncio.Lock()
        if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id) is None:
            raise LabPaused("InvalidRunIdentifier")
        self.run_id = run_id
        self.diagnostic = diagnostic

    def send_diagnostic(
        self,
        stage: str,
        message: OutboundMessage,
        rows: list[dict[str, Any]],
    ) -> None:
        if self.diagnostic is None:
            return
        # Only exact synthetic ACK matches reach this callback. Upstream names,
        # account/chat IDs, message contents, and exception text never do.
        matches = [r for r in rows if r.get("content") == message.text]
        self.diagnostic(
            {
                "run_id": self.run_id,
                "stage": stage,
                "message_ref": str(message.message_id),
                "post_count": self.http.post_count,
                "send_seconds": round(self.http.last_send_seconds, 3),
                "match_count": len(matches),
                "records": [
                    {
                        "local_ref": reference(self.key, "local", str(r["localId"])),
                        "server_ref": reference(self.key, "server", str(r.get("serverId"))),
                        "timestamp_ref": reference(self.key, "time", str(r.get("timestamp"))),
                        "is_self": r.get("isSelf") if type(r.get("isSelf")) is bool else None,
                    }
                    for r in matches
                ],
            }
        )

    def pause(self, code: str) -> NoReturn:
        self.paused = code
        raise LabPaused(code)

    async def guard(self) -> None:
        if self.paused:
            raise LabPaused(self.paused)
        try:
            account = account_id(await self.http.request("GET", "/api/status/auth"))
            if reference(self.key, "account", account) != self.binding.account_ref:
                raise LabPaused("AccountChanged")
            if await unique_testing(self.http) != self.binding.chat_id:
                raise LabPaused("TestingBindingChanged")
            candidates = title_candidates(await self.http.request("GET", "/api/debug/a11y"))
            if candidates != [(self.binding.header_path, self.binding.header_roles)]:
                raise LabPaused("TestingNotForeground")
            if account_id(await self.http.request("GET", "/api/status/auth")) != account:
                raise LabPaused("AccountChanged")
        except LabPaused as error:
            self.pause(str(error))

    async def page(self, offset: int = 0) -> list[dict[str, Any]]:
        path = f"/api/messages/{quote(self.binding.chat_id, safe='')}"
        rows = await self.http.request("GET", f"{path}?limit={PAGE_SIZE}&offset={offset}")
        return self.validate_page(rows)

    def validate_page(self, rows: Any) -> list[dict[str, Any]]:
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
            self.pause("InvalidMessageList")
        for row in rows:
            if not isinstance(row, dict):
                self.pause("InvalidMessageList")
            if row.get("chatId") != self.binding.chat_id:
                self.pause("WrongChatMessage")
            if type(row.get("localId")) is not int or row["localId"] <= 0:
                self.pause("MessageIdentityMissing")
        if len({r["localId"] for r in rows}) != len(rows):
            self.pause("DuplicatePageIdentity")
        return cast(list[dict[str, Any]], rows)

    async def baseline(self) -> None:
        await self.guard()
        self.anchors = {r["localId"] for r in await self.page()}

    def event(self, row: dict[str, Any]) -> InboundEvent | None:
        # Only synthetic text commands may enter the experimental database.
        text = row.get("content")
        if row.get("type") != 1 or not isinstance(text, str) or not COMMAND.fullmatch(text):
            return None
        if type(row.get("isSelf")) is not bool:
            self.pause("DirectionMissing")
        sender = row.get("sender")
        if not isinstance(sender, str) or not sender or len(sender) > 512:
            self.pause("SenderMissing")
        if row["isSelf"]:
            return None
        try:
            timestamp = datetime.fromisoformat(row["timestamp"])
            if timestamp.tzinfo is None:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            self.pause("TimestampMissing")
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
            sender_id=reference(self.key, "sender", self.binding.account_ref, sender),
            text=text,
            occurred_at=timestamp,
        )

    async def poll(self) -> list[InboundEvent]:
        try:
            await self.guard()
            if self.anchors is None:
                await self.baseline()
                return []
            first = await self.page()
            rows: list[dict[str, Any]] = []
            seen: set[int] = set()
            page = first
            for index in range(MAX_PAGES):
                overlap = [i for i, r in enumerate(page) if r["localId"] in self.anchors]
                end = max(overlap) + 1 if overlap else len(page)
                for row in page[:end]:
                    identifier = row["localId"]
                    if identifier not in self.anchors and identifier not in seen:
                        rows.append(row)
                        seen.add(identifier)
                if overlap or (not self.anchors and len(page) < PAGE_SIZE):
                    break
                if len(page) < PAGE_SIZE or index == MAX_PAGES - 1:
                    self.pause("AnchorLost")
                page = await self.page((index + 1) * PAGE_SIZE)
            # Offset pagination must not silently skip arrivals while reading pages.
            if [r["localId"] for r in await self.page()] != [r["localId"] for r in first]:
                self.pause("MessageWindowChanged")
            events = [e for row in reversed(rows) if (e := self.event(row)) is not None]
            self.anchors = {r["localId"] for r in first}
            return events
        except LabPaused as error:
            self.pause(str(error))

    async def events(self) -> AsyncIterator[InboundEvent]:
        await self.baseline()
        while not self.closed:
            for event in await self.poll():
                yield event
            await asyncio.sleep(self.poll_seconds)

    async def deliver(self, message: OutboundMessage) -> DeliveryReceipt:
        async with self.delivery_lock:
            return await self._deliver(message)

    def valid_reply(self, message: OutboundMessage) -> bool:
        return bool(ACK.fullmatch(message.text))

    async def before_send_guard(self) -> None:
        await self.guard()

    async def readback_guard(self) -> None:
        await self.guard()

    def existing_reply_blocks_send(self) -> bool:
        return True

    async def _deliver(self, message: OutboundMessage) -> DeliveryReceipt:
        identifier = str(message.message_id)
        if identifier in self.attempts:
            if self.reply_texts[identifier] != message.text:
                self.pause("ReplyIdentityRebound")
            return self.attempts[identifier]
        if (
            message.channel != CHANNEL
            or message.chat_id != self.binding.chat_ref
            or not self.valid_reply(message)
        ):
            return DeliveryReceipt(
                message_id=message.message_id,
                status=DeliveryStatus.FAILED,
                detail="InvalidLabReply",
            )
        await self.before_send_guard()  # failure here is known to precede POST
        before = await self.page()
        self.send_diagnostic("before_send", message, before)
        if self.existing_reply_blocks_send() and any(
            r.get("content") == message.text for r in before
        ):
            self.pause("AckAlreadyPresent")
        previous = {r["localId"] for r in before}
        unknown = DeliveryReceipt(
            message_id=message.message_id,
            status=DeliveryStatus.UNKNOWN,
            detail="SendNotConfirmedNoRetry",
        )
        self.attempts[identifier] = unknown
        self.reply_texts[identifier] = message.text
        try:
            result = await self.http.request(
                "POST", "/api/messages/send", {"chatId": self.binding.chat_id, "text": message.text}
            )
            self.send_diagnostic("post_returned", message, [])
            if not isinstance(result, dict) or result.get("success") is not True:
                self.pause("SendNotConfirmedNoRetry")
            deadline = asyncio.get_running_loop().time() + self.readback_seconds
            while True:
                await self.readback_guard()
                page = await self.page()
                self.send_diagnostic("readback", message, page)
                matches = [
                    r
                    for r in page
                    if r["localId"] not in previous and r.get("content") == message.text
                ]
                if len(matches) > 1:
                    self.pause("AmbiguousReadback")
                if matches:
                    row = matches[0]
                    if row.get("isSelf") is not True or not row.get("sender"):
                        self.pause("InvalidSelfReadback")
                    receipt = DeliveryReceipt(
                        message_id=message.message_id,
                        status=DeliveryStatus.ACKNOWLEDGED,
                        external_id=reference(
                            self.key,
                            "receipt",
                            self.binding.account_ref,
                            self.binding.chat_id,
                            str(row["localId"]),
                        ),
                        acknowledged_at=utc_now(),
                    )
                    self.attempts[identifier] = receipt
                    return receipt
                if asyncio.get_running_loop().time() >= deadline:
                    self.pause("ReadbackTimeoutNoRetry")
                await asyncio.sleep(min(self.poll_seconds, self.readback_seconds))
        except LabPaused as error:
            self.paused = str(error)
            self.send_diagnostic("unconfirmed", message, [])
            return unknown

    async def health(self) -> ConnectorHealth:
        try:
            await self.guard()
        except LabPaused:
            return ConnectorHealth(healthy=False, detail=self.paused)
        return ConnectorHealth(healthy=True, detail="ExperimentalTestingForeground")

    async def close(self) -> None:
        self.closed = True
        await self.http.close()


def reply_for(event: InboundEvent) -> OutboundMessage:
    match = COMMAND.fullmatch(event.text or "")
    if event.channel != CHANNEL or match is None:
        raise LabPaused("InvalidLabEvent")
    return OutboundMessage(
        message_id=uuid5(NAMESPACE_URL, f"lemonbot:agent-lab:{event.event_id}"),
        channel=CHANNEL,
        chat_id=event.chat_id,
        text="LB26_ACK_" + match[1],
        reply_to_event_id=event.event_id,
    )
