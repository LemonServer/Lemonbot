"""Convert one account/chat-verified snapshot row without loading any media."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from lemonbot.connectors.wechat_agent import CHANNEL, Binding, reference
from lemonbot.domain.models import OutboxState

from .archive import ArchiveError
from .models import ConversationScope, Message, MessagePart, SourceRef

_URL = re.compile(r"https://[^\s<>\"'，。！？；、]{1,4096}", re.IGNORECASE)
_TRAILING = ".,;:!?，。！？；、)]}》」』"


def archive_scope(binding: Binding, *, is_group: bool) -> ConversationScope:
    return ConversationScope(
        channel=CHANNEL,
        account_id=binding.account_ref,
        conversation_id=binding.chat_ref,
        kind="group" if is_group else "private",
    )


def snapshot_message(
    row: dict[str, Any], *, binding: Binding, key: bytes, is_group: bool
) -> tuple[Message, str | None]:
    """Call only after account, target, snapshot and anchor verification.

    Native IDs are broker locators; canonical HMAC event IDs stay unchanged.
    Mention text and quoted identity do not grant trigger or source authority.
    """
    local_id, sender, kind = row.get("localId"), row.get("sender"), row.get("type")
    if type(kind) is not int:
        raise ArchiveError("ArchiveMessageTypeMissing")
    if row.get("chatId") != binding.chat_id or type(local_id) is not int or local_id <= 0:
        raise ArchiveError("ArchiveMessageIdentityMissing")
    if type(row.get("isSelf")) is not bool:
        raise ArchiveError("ArchiveDirectionMissing")
    if not isinstance(sender, str) or not sender or len(sender) > 512:
        raise ArchiveError("ArchiveSenderMissing")
    if not is_group and not row["isSelf"] and sender != binding.chat_id:
        raise ArchiveError("ArchivePrivateSenderMismatch")
    try:
        occurred_at = datetime.fromisoformat(row["timestamp"])
        if occurred_at.tzinfo is None:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ArchiveError("ArchiveTimestampMissing") from None
    scope = archive_scope(binding, is_group=is_group)
    identifier = reference(key, "event", binding.account_ref, binding.chat_id, str(local_id))
    parts = []
    text = row.get("content")
    if text is not None and not isinstance(text, str):
        raise ArchiveError("ArchiveInvalidText")
    if kind == 1 or row.get("quoteReply") is True:
        parts.append(MessagePart(part_id="text", kind="text", text=text or ""))
        # URL parts retain exact links; the original full text remains intact.
        urls = tuple(dict.fromkeys(m.group(0).rstrip(_TRAILING) for m in _URL.finditer(text or "")))
        parts.extend(
            MessagePart(part_id=f"url-{i}", kind="link", url=url) for i, url in enumerate(urls)
        )
    elif kind == 3:
        parts.append(MessagePart(part_id="image", kind="image", unavailable_reason="not_loaded"))
    elif kind == 49:
        url = row.get("linkUrl")
        title = row.get("linkTitle")
        parts.append(
            MessagePart(
                part_id="card",
                kind="card",
                url=url if isinstance(url, str) and url else None,
                title=title if isinstance(title, str) else None,
                unavailable_reason=None if isinstance(url, str) and url else "card_url_unavailable",
            )
        )
    else:
        media = {34: "audio", 43: "video", 47: "unknown"}.get(kind, "unknown")
        parts.append(
            MessagePart.model_validate(
                {
                    "part_id": "unsupported",
                    "kind": media,
                    "unavailable_reason": "unsupported_kind",
                }
            )
        )
    references: tuple[SourceRef, ...] = ()
    if row.get("quoteReply") is True:
        quoted_id = row.get("quotedMessageLocalId", row.get("quotedImageLocalId"))
        if type(quoted_id) is int and quoted_id > 0 and quoted_id != local_id:
            references = (
                SourceRef(
                    scope=scope,
                    message_id=reference(
                        key, "event", binding.account_ref, binding.chat_id, str(quoted_id)
                    ),
                    part_id="image" if row.get("quotedImage") is True else None,
                ),
            )
        else:
            parts.append(
                MessagePart(
                    part_id="unresolved-quote",
                    kind="unknown",
                    unavailable_reason="quoted_source_unavailable_or_ambiguous",
                )
            )
        # quotedText is never merged with current text or used to guess identity.
    is_self = row["isSelf"]
    message = Message(
        scope=scope,
        message_id=identifier,
        event_id=None if is_self else identifier,
        transport_message_id=str(local_id),
        sender_id=reference(key, "sender", binding.account_ref, sender),
        direction="outbound" if is_self else "inbound",
        occurred_at=occurred_at,
        parts=tuple(parts),
        references=references,
        delivery_state=OutboxState.ACKNOWLEDGED if is_self else None,
    )
    receipt = (
        reference(key, "receipt", binding.account_ref, binding.chat_id, str(local_id))
        if is_self
        else None
    )
    return message, receipt
