"""Operator-requested startup summary; names only, no IDs or message bodies."""

from __future__ import annotations

import json
import re
from urllib.parse import quote

from lemonbot.connectors.wechat_agent import AgentWechatHTTP, account_id, reference
from lemonbot.connectors.wechat_agent_conversation import ConversationMultiplexer


def emit_startup_report(rows: list[dict[str, object]], *, startup_failed: bool = False) -> None:
    # JSON escapes control characters in operator-configured display names.
    for row in rows:
        print("wechat_startup " + json.dumps(row, ensure_ascii=False), flush=True)
    print(
        "wechat_startup "
        + json.dumps(
            {
                "summary": True,
                "startup_failed": startup_failed,
                "total": len(rows),
                "resolved": sum(r.get("resolved") is True for r in rows),
                "readable": sum(r.get("readable") is True for r in rows),
                "all_readable": bool(rows) and all(r.get("readable") is True for r in rows),
                "enabled": sum(r.get("enabled") is True for r in rows),
                "all_available": bool(rows) and all(r.get("enabled") is True for r in rows),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


async def report_sync_failure(
    http: AgentWechatHTTP,
    requested: list[tuple[str, bool]],
    code: str,
) -> None:
    rows: list[dict[str, object]] = []
    matches: dict[str, list[bool]] | None = None
    if code in {
        "WhitelistTargetNotFound",
        "WhitelistTargetAmbiguous",
        "WhitelistTargetKindMismatch",
    }:
        try:
            matches = {name: [] for name, _ in requested}
            for offset in range(0, 1000, 100):
                chats = await http.request("GET", f"/api/chats?limit=100&offset={offset}")
                for chat in chats:
                    if chat.get("name") in matches:
                        matches[chat["name"]].append(chat.get("isGroup"))
                if len(chats) < 100:
                    break
            else:
                matches = None
        except Exception:
            matches = None
    for name, group in requested:
        found = matches[name] if matches is not None else None
        reason = "StartupBlockedByWhitelist"
        resolved = None
        if found is not None:
            resolved = len(found) == 1 and found[0] is group
            if not found:
                reason = "WhitelistTargetNotFound"
            elif len(found) != 1:
                reason = "WhitelistTargetAmbiguous"
            elif found[0] is not group:
                reason = "WhitelistTargetKindMismatch"
        elif re.fullmatch(r"[A-Za-z0-9:_-]{1,160}", code):
            reason = code
        rows.append(
            {
                "name": name,
                "kind": "group" if group else "private",
                "resolved": resolved,
                "readable": None,
                "enabled": False,
                "reason": reason,
            }
        )
    emit_startup_report(rows, startup_failed=True)


async def report_conversations(
    transport: ConversationMultiplexer,
    *,
    ready: bool,
    pause_reason: str | None,
) -> None:
    reasons: dict[str, str | None] = {}
    try:
        account = account_id(await transport.http.request("GET", "/api/status/auth"))
        if reference(transport.key, "account", account) != transport.binding.account_ref:
            raise ValueError
        response = await transport.metadata.post(
            "/read-cycle",
            json={"anchors": {c.binding.chat_id: None for c in transport.children.values()}},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("accountVerified") is not True:
            raise ValueError
        results = payload["targets"]
        expected = {c.binding.chat_id for c in transport.children.values()}
        if len(results) != len(expected) or {r["chatId"] for r in results} != expected:
            raise ValueError
        for result in results:
            error = result.get("error")
            if error not in {None, "MessageTableMissing", "MessageTableAmbiguous"}:
                raise ValueError
            reasons[result["chatId"]] = error
    except Exception:
        reasons = {c.binding.chat_id: "StartupReadCheckFailed" for c in transport.children.values()}
    rows: list[dict[str, object]] = []
    for ref, child in transport.children.items():
        error = reasons[child.binding.chat_id]
        if error is None:
            try:
                chat = await transport.http.request(
                    "GET", "/api/chats/" + quote(child.binding.chat_id, safe="")
                )
                if (chat.get("id") or chat.get("username")) != child.binding.chat_id or (
                    chat.get("name") != child.target.name
                    or chat.get("isGroup") is not child.target.is_group
                ):
                    error = "ConversationBindingChanged"
            except Exception:
                error = "StartupBindingCheckFailed"
        paused = transport.target_pauses.get(ref) or pause_reason or transport.paused
        # Fixed diagnostic codes only. Never print exception/request text.
        reason = error or paused
        if reason and re.fullmatch(r"[A-Za-z0-9:_-]{1,160}", reason) is None:
            reason = "VerificationFailed"
        rows.append(
            {
                "name": child.target.name,
                "kind": "group" if child.target.is_group else "private",
                "resolved": error
                not in {
                    "ConversationBindingChanged",
                    "StartupBindingCheckFailed",
                    "StartupReadCheckFailed",
                },
                "readable": error is None,
                "enabled": ready and not reason,
                "reason": reason,
            }
        )
    emit_startup_report(rows)
