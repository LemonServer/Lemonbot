"""Fail-closed, observe-only broker for official Linux WeChat AT-SPI snapshots."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lemonbot.domain import (
    ConnectorHealth,
    DeliveryReceipt,
    DeliveryStatus,
    InboundEvent,
    OutboundMessage,
)

from .atspi_protocol import AtspiHealth, AtspiSnapshot, AtspiTranscriptItem
from .base import Connector

CHANNEL = "wechat_personal_lab"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AtspiEnrollmentTarget(_StrictModel):
    target_ref: str = Field(pattern=r"^[a-z0-9_-]{1,128}$")
    chat_kind: Literal["private", "group"]
    header_selector: tuple[int, ...] = Field(min_length=1, max_length=32)
    header_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    transcript_selector: tuple[int, ...] = Field(min_length=1, max_length=32)
    self_item_signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    inbound_item_signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    self_body_relative_path: tuple[int, ...] = Field(min_length=1, max_length=16)
    inbound_body_relative_path: tuple[int, ...] = Field(min_length=1, max_length=16)
    sender_relative_path: tuple[int, ...] | None = Field(default=None, max_length=16)
    sender_attribute_key: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_semantic_proof(self) -> AtspiEnrollmentTarget:
        if self.self_item_signature == self.inbound_item_signature:
            raise ValueError("self and inbound signatures must differ")
        if self.chat_kind == "group" and (
            self.sender_relative_path is None or not self.sender_attribute_key
        ):
            raise ValueError("group targets require a stable sender attribute")
        if self.sender_attribute_key in {"name", "description", "display-name"}:
            raise ValueError("display text cannot be used as sender identity")
        return self


class AtspiEnrollment(_StrictModel):
    schema_version: Literal[1] = 1
    account_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    ui_signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    targets: tuple[AtspiEnrollmentTarget, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def require_unique_targets(self) -> AtspiEnrollment:
        refs = [target.target_ref for target in self.targets]
        if len(refs) != len(set(refs)):
            raise ValueError("enrollment target refs must be unique")
        return self

    @classmethod
    def load(cls, path: Path, expected_sha256: str) -> AtspiEnrollment:
        absolute = path.expanduser()
        if absolute.is_symlink() or not absolute.is_absolute() or not absolute.is_file():
            raise ValueError("enrollment bundle must be an absolute regular non-symlink file")
        if absolute.stat().st_mode & 0o077:
            raise ValueError("enrollment bundle permissions must be 0600")
        payload = absolute.read_bytes()
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ValueError("enrollment bundle hash mismatch")
        result = cls.model_validate_json(payload)
        return result


class AtspiSnapshotSource(Protocol):
    def snapshots(self) -> AsyncIterator[AtspiSnapshot]: ...

    async def health(self) -> AtspiHealth: ...

    async def close(self) -> None: ...


class AtspiCursor(_StrictModel):
    tail_fingerprints: tuple[str, ...] = Field(default=(), max_length=100)
    chain_hash: str = Field(default="0" * 64, pattern=r"^[0-9a-f]{64}$")
    generation: int = Field(default=0, ge=0)


LoadCursor = Callable[[str], Awaitable[AtspiCursor | None]]
SaveCursor = Callable[[str, AtspiCursor], Awaitable[None]]


def _item_fingerprint(item: AtspiTranscriptItem) -> str:
    canonical = json.dumps(
        {
            "direction": item.direction,
            "sender_ref": item.sender_ref,
            "text": item.text,
            "structure": item.structure_fingerprint,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _align_tail(previous: tuple[str, ...], current: tuple[str, ...]) -> int | None:
    """Return the first new item index, or None when the overlap is ambiguous."""

    if not previous:
        return len(current)
    maximum = min(len(previous), len(current))
    for size in range(maximum, 0, -1):
        needle = previous[-size:]
        matches = [
            index
            for index in range(len(current) - size + 1)
            if current[index : index + size] == needle
        ]
        if len(matches) == 1:
            return matches[0] + size
        if len(matches) > 1:
            return None
    return None


class AtspiObserveConnector(Connector):
    """Turns proven, allowlisted transcript deltas into durable inbound events."""

    def __init__(
        self,
        source: AtspiSnapshotSource,
        enrollment: AtspiEnrollment,
        *,
        allow_target_refs: frozenset[str],
        load_cursor: LoadCursor | None = None,
        save_cursor: SaveCursor | None = None,
    ) -> None:
        enrolled = {target.target_ref: target for target in enrollment.targets}
        if not allow_target_refs or not allow_target_refs <= enrolled.keys():
            raise ValueError("allowlist must be a non-empty subset of enrollment targets")
        self._source = source
        self._enrollment = enrolled
        self._allowed = allow_target_refs
        self._load_cursor = load_cursor
        self._save_cursor = save_cursor
        self._cursors: dict[str, AtspiCursor] = {}
        self._paused_reason: str | None = None
        self._closed = False
        self._failed_receipts: dict[UUID, DeliveryReceipt] = {}

    async def _cursor(self, target_ref: str) -> AtspiCursor:
        cursor = self._cursors.get(target_ref)
        if cursor is None and self._load_cursor is not None:
            cursor = await self._load_cursor(target_ref)
        cursor = cursor or AtspiCursor()
        self._cursors[target_ref] = cursor
        return cursor

    async def _save(self, target_ref: str, cursor: AtspiCursor) -> None:
        self._cursors[target_ref] = cursor
        if self._save_cursor is not None:
            await self._save_cursor(target_ref, cursor)

    async def events(self) -> AsyncIterator[InboundEvent]:
        async for snapshot in self._source.snapshots():
            if self._closed:
                return
            target = self._enrollment.get(snapshot.target_ref)
            if target is None or snapshot.target_ref not in self._allowed:
                self._paused_reason = "unenrolled_target"
                continue
            if (
                snapshot.chat_kind != target.chat_kind
                or snapshot.header_fingerprint != target.header_fingerprint
            ):
                self._paused_reason = "target_identity_mismatch"
                continue
            cursor = await self._cursor(snapshot.target_ref)
            fingerprints = tuple(_item_fingerprint(item) for item in snapshot.items)
            start = _align_tail(cursor.tail_fingerprints, fingerprints)
            if start is None:
                self._paused_reason = "transcript_alignment_ambiguous"
                continue
            chain_hash = cursor.chain_hash
            for item, fingerprint in zip(snapshot.items[start:], fingerprints[start:], strict=True):
                chain_hash = hashlib.sha256(f"{chain_hash}:{fingerprint}".encode()).hexdigest()
                if item.direction == "self":
                    continue
                if snapshot.chat_kind == "group" and not item.sender_ref:
                    self._paused_reason = "group_sender_unproven"
                    break
                occurred_at = item.occurred_at or datetime.now(UTC)
                yield InboundEvent(
                    channel=CHANNEL,
                    event_id=f"atspi-v1:{snapshot.target_ref}:{chain_hash}",
                    chat_id=snapshot.target_ref,
                    sender_id=item.sender_ref or snapshot.target_ref,
                    text=item.text,
                    occurred_at=occurred_at,
                    metadata={
                        "source": "linux_atspi",
                        "chat_kind": snapshot.chat_kind,
                        "structure_fingerprint": item.structure_fingerprint,
                    },
                )
            else:
                updated = AtspiCursor(
                    tail_fingerprints=fingerprints[-100:],
                    chain_hash=chain_hash,
                    generation=snapshot.generation,
                )
                await self._save(snapshot.target_ref, updated)
                self._paused_reason = None

    async def deliver(self, message: OutboundMessage) -> DeliveryReceipt:
        existing = self._failed_receipts.get(message.message_id)
        if existing is not None:
            return existing
        receipt = DeliveryReceipt(
            message_id=message.message_id,
            status=DeliveryStatus.FAILED,
            detail="observe_only: AT-SPI connector has no outbound action API",
        )
        self._failed_receipts[message.message_id] = receipt
        return receipt

    async def health(self) -> ConnectorHealth:
        worker = await self._source.health()
        healthy = worker.healthy and self._paused_reason is None and not self._closed
        return ConnectorHealth(
            healthy=healthy,
            detail=self._paused_reason or worker.detail_code,
            account_id=self._enrollment[next(iter(self._allowed))].target_ref,
        )

    async def close(self) -> None:
        self._closed = True
        await self._source.close()


__all__ = [
    "AtspiCursor",
    "AtspiEnrollment",
    "AtspiEnrollmentTarget",
    "AtspiObserveConnector",
    "AtspiSnapshot",
    "AtspiTranscriptItem",
]
