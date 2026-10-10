"""Resolve operator-configured display names into verified private bindings."""

from __future__ import annotations

import json
import os
import re
import stat
import tomllib
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote

import httpx
from pydantic import Field, ValidationError, model_validator

from lemonbot.config.settings import StrictModel
from lemonbot.connectors.wechat_agent import (
    MAX_PAGES,
    PAGE_SIZE,
    AgentWechatHTTP,
    Binding,
    LabPaused,
    account_id,
    reference,
    title_candidates,
)
from lemonbot.connectors.wechat_agent_conversation import (
    ConversationTarget,
    ConversationTransport,
    load_targets,
)
from lemonbot.connectors.wechat_agent_state import private_read, private_write


class WechatWhitelist(StrictModel):
    version: int = 1
    groups: tuple[str, ...] = Field(default=(), max_length=20)
    contacts: tuple[str, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def validate_names(self) -> WechatWhitelist:
        names = (*self.groups, *self.contacts)
        if self.version != 1 or not 1 <= len(names) <= 20:
            raise ValueError("whitelist requires 1 to 20 names and version=1")
        if any(not n or n != n.strip() or len(n) > 512 for n in names):
            raise ValueError("names must be nonempty exact display names")
        # The send guard requires globally unique display titles, even across kinds.
        if len(set(names)) != len(names):
            raise ValueError("whitelist names must be unique")
        return self


def read_whitelist(path: Path) -> WechatWhitelist:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o022
                or info.st_size > 16384
            ):
                raise ValueError
            return WechatWhitelist.model_validate(tomllib.loads(stream.read()))
    except (OSError, ValueError, ValidationError):
        raise LabPaused("InvalidWhitelistConfig") from None


async def sync_whitelist(
    http: AgentWechatHTTP,
    anchor: Binding,
    key: bytes,
    state: Path,
    config: WechatWhitelist,
    metadata: httpx.AsyncClient,
) -> tuple[ConversationTarget, ...]:
    """No messages sent. Resolve all names before navigation or binding changes.

    The original account binding remains the trust anchor. Configuring names
    explicitly authorizes these targets, but never authorizes account changes.
    """
    account = account_id(await http.request("GET", "/api/status/auth"))
    if reference(key, "account", account) != anchor.account_ref:
        raise LabPaused("AccountChanged")
    requested = {**dict.fromkeys(config.groups, True), **dict.fromkeys(config.contacts, False)}
    matches: dict[str, dict[str, tuple[str, bool]]] = {name: {} for name in requested}
    for page in range(MAX_PAGES):
        rows = await http.request("GET", f"/api/chats?limit={PAGE_SIZE}&offset={page * PAGE_SIZE}")
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
            raise LabPaused("InvalidChatList")
        for row in rows:
            if not isinstance(row, dict):
                raise LabPaused("InvalidChatList")
            if row.get("name") not in requested:
                continue
            identifier = row.get("id") or row.get("username")
            kind = row.get("isGroup")
            if (
                not isinstance(identifier, str)
                or not identifier
                or len(identifier) > 512
                or type(kind) is not bool
                or kind != identifier.endswith("@chatroom")
            ):
                raise LabPaused("InvalidChatList")
            matches[row["name"]][identifier] = (identifier, kind)
        if len(rows) < PAGE_SIZE:
            break
    else:
        raise LabPaused("ChatListLimit")
    selected = []
    for name, is_group in requested.items():
        if not matches[name]:
            raise LabPaused("WhitelistTargetNotFound")
        if len(matches[name]) != 1:
            raise LabPaused("WhitelistTargetAmbiguous")
        identifier, kind = next(iter(matches[name].values()))
        if kind != is_group:
            raise LabPaused("WhitelistTargetKindMismatch")
        selected.append((identifier, name, kind))
    if len({row[0] for row in selected}) != len(selected):
        raise LabPaused("WhitelistTargetAmbiguous")

    # Reuse semantic bindings when both identity and display name are unchanged.
    # A corrupt previous binding fails closed; it is never silently overwritten.
    previous_path = state / "conversation-binding.json"
    previous = (
        {t.binding.chat_id: t for t in load_targets(previous_path, anchor.account_ref, key)}
        if previous_path.exists()
        else {}
    )
    for identifier, name, _kind in selected:
        if any(t.name == name and t.binding.chat_id != identifier for t in previous.values()):
            raise LabPaused("WhitelistTargetIdentityChanged")
    targets = []
    for identifier, name, kind in selected:
        target = previous.get(identifier)
        if target is None or target.name != name or target.is_group != kind:
            result = await http.request(
                "POST", "/api/chats/" + quote(identifier, safe="") + "/open?clearUnreads=false"
            )
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise LabPaused("ConversationNavigationFailed")
            suffix = r"(?:\s*\([0-9]+\))?" if kind else ""
            titles = title_candidates(
                await http.request("GET", "/api/debug/a11y"), re.compile(re.escape(name) + suffix)
            )
            if len(titles) != 1:
                raise LabPaused("ConversationTitleAmbiguous")
            path, roles = titles[0]
            target = ConversationTarget(
                Binding(
                    anchor.account_ref,
                    identifier,
                    reference(key, "chat", anchor.account_ref, identifier),
                    path,
                    roles,
                ),
                name,
                kind,
            )
            await ConversationTransport(http, target, key, metadata).readback_guard()
        else:
            await ConversationTransport(http, target, key, metadata).guard()
        targets.append(target)
    if account_id(await http.request("GET", "/api/status/auth")) != account:
        raise LabPaused("AccountChanged")

    # Bridge must acknowledge the same account and exact ID set before local commit.
    try:
        response = await metadata.post(
            "/bindings", json={"account": account, "chat_ids": [t.binding.chat_id for t in targets]}
        )
        response.raise_for_status()
        body = response.json()
        identity = body.get("self_identity")
        if (
            body.get("accountVerified") is not True
            or body.get("account") != account
            or set(body.get("chat_ids", [])) != {t.binding.chat_id for t in targets}
            or not isinstance(identity, str)
            or not identity
            or (account != identity and not account.startswith(identity + "_"))
        ):
            raise ValueError
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        raise LabPaused("WhitelistBridgeSyncFailed") from None
    private_write(
        state / "metadata-binding.json",
        json.dumps(
            {
                "account": account,
                "chat_ids": [t.binding.chat_id for t in targets],
                "self_identity": identity,
            }
        ).encode(),
    )
    private_write(
        previous_path,
        json.dumps(
            {
                "account_ref": anchor.account_ref,
                "operator_confirmed": True,
                "targets": [
                    {"binding": asdict(t.binding), "name": t.name, "is_group": t.is_group}
                    for t in targets
                ],
            }
        ).encode(),
    )
    pauses = state / "targets-paused.json"
    if pauses.exists():
        old = json.loads(private_read(pauses))
        allowed = {t.binding.chat_ref for t in targets}
        private_write(pauses, json.dumps({k: v for k, v in old.items() if k in allowed}).encode())
    return tuple(targets)
