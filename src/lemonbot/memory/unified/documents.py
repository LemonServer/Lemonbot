"""Deterministic, source-bearing retrieval units; never fetch media or URLs."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from datetime import datetime

from pydantic import Field

from .archive import canonical
from .models import ArchiveModel, Identifier, Message, Observation, SourceRef

INDEX_VERSION = "hybrid-fts-bge-v1"
UNIT_CHARS = 1000


def literal_passages(text: str) -> tuple[str, ...]:
    """Literal retrieval hints, not message references or tool authorization."""
    quoted = re.findall(r'''[“「『"'‘]([^”」』"'’\n]{8,300})[”」』"'’]''', text)
    # Natural Chinese questions need not use quotation marks. A long unchanged
    # phrase can identify stored source text even among repeated failed replies.
    unquoted = re.findall(r"[\u3400-\u9fff]{12,300}", text)
    return tuple(dict.fromkeys((*quoted, *unquoted)))


class IndexUnit(ArchiveModel):
    unit_id: Identifier
    source: SourceRef
    author_id: Identifier
    occurred_at: datetime
    text: str = Field(min_length=1, max_length=UNIT_CHARS)
    start: int = Field(default=0, ge=0)
    observation_id: Identifier | None = None
    media: bool = False
    searchable: bool = True

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical(self.model_dump(mode="json")).encode()).hexdigest()


def terms(text: str) -> tuple[str, ...]:
    """Unicode words plus Chinese unigrams/bigrams; no third-party segmenter."""
    output: dict[str, None] = {}
    for word in re.findall(r"[\u3400-\u9fff]+|[^\W_\u3400-\u9fff]+", text.casefold()):
        if all("\u3400" <= char <= "\u9fff" for char in word):
            pieces = [word] if len(word) == 1 else [word[i : i + 2] for i in range(len(word) - 1)]
        else:
            pieces = [word]
        for piece in pieces:
            # Generated tokens are passed as quoted literals, never raw FTS syntax.
            output[piece] = None
    return tuple(output)


def message_units(
    message: Message,
    observations: tuple[Observation, ...],
    now: datetime,
) -> tuple[IndexUnit, ...]:
    if not message.eligible_as_evidence or message.occurred_at > now:
        return ()
    pieces: list[tuple[SourceRef, str, str | None, bool, bool]] = []
    for part in message.parts:
        source = SourceRef(scope=message.scope, message_id=message.message_id, part_id=part.part_id)
        media = part.kind in {"image", "audio", "video", "file", "unknown"}
        if media:
            body = f"[{part.kind}：仅有元数据，未读取原始附件]"
            searchable = False
        else:
            body = "\n".join(value for value in (part.text, part.title, part.url) if value)
            searchable = bool(body.strip())
        if body:
            pieces.append((source, body, None, media, searchable))
    for observation in observations:
        if (
            observation.source.scope != message.scope
            or observation.source.message_id != message.message_id
            or observation.status != "available"
            or observation.created_at > now
            or (observation.expires_at is not None and observation.expires_at <= now)
        ):
            continue
        source = observation.source
        observed_part = next((p for p in message.parts if p.part_id == source.part_id), None)
        if source.part_id is not None and observed_part is None:
            continue
        observed_text = observation.text
        if observed_text:
            pieces.append(
                (
                    source,
                    observed_text,
                    observation.observation_id,
                    observed_part is not None and observed_part.kind == "image",
                    True,
                )
            )
    result = []
    for source, body, observation_id, media, searchable in pieces:
        for start in range(0, len(body), UNIT_CHARS):
            identifier = hashlib.sha256(
                canonical(
                    [
                        source.model_dump(mode="json"),
                        observation_id,
                        start,
                        INDEX_VERSION,
                    ]
                ).encode()
            ).hexdigest()
            result.append(
                IndexUnit(
                    unit_id=identifier,
                    start=start,
                    source=source,
                    author_id=message.sender_id,
                    occurred_at=message.occurred_at,
                    text=body[start : start + UNIT_CHARS],
                    observation_id=observation_id,
                    media=media,
                    searchable=searchable,
                )
            )
    return tuple(result)


def query_values(message: Message) -> Iterator[str]:
    for part in message.parts:
        if part.kind in {"text", "link", "card"}:
            for value in (part.text, part.title, part.url):
                if value:
                    yield value


def query_over_limit(message: Message) -> bool:
    size = -1
    for value in query_values(message):
        size += len(value) + 1
        if size > 8000:
            return True
    return False


def current_query(message: Message) -> str:
    # A retrieval query is a bounded view; Message itself is never rewritten.
    result = ""
    for value in query_values(message):
        if result:
            result += "\n"
        result += value[: 8000 - len(result)]
        if len(result) >= 8000:
            break
    return result


def image_followup(query: str) -> bool:
    return any(
        word in query.casefold()
        for word in (
            "图片",
            "照片",
            "截图",
            "这张",
            "那张",
            "图里",
            "图中",
            "图形",
            "左边",
            "右边",
            "image",
            "photo",
            "picture",
        )
    )
