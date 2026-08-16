from __future__ import annotations

import asyncio
import json
import struct
from typing import Any, BinaryIO
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

MAX_FRAME_BYTES = 1024 * 1024
_HEADER = struct.Struct("!I")


class IPCError(RuntimeError):
    pass


def _decode_envelope(payload: bytes) -> Envelope:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    def reject_non_finite(value: str) -> None:
        raise ValueError(f"non-finite JSON number is forbidden: {value}")

    try:
        value = json.loads(
            payload,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_non_finite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise IPCError("worker sent invalid UTF-8 JSON") from exc
    try:
        return Envelope.model_validate(value)
    except (RecursionError, ValueError) as exc:
        raise IPCError("worker envelope failed schema validation") from exc


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: int = Field(default=1, ge=1, le=1)
    request_id: UUID = Field(default_factory=uuid4)
    message_type: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]*$")
    payload: dict[str, Any]


async def read_frame(reader: asyncio.StreamReader, *, limit: int = MAX_FRAME_BYTES) -> Envelope:
    try:
        header = await reader.readexactly(_HEADER.size)
    except asyncio.IncompleteReadError as exc:
        raise IPCError("worker pipe closed while reading frame header") from exc
    (length,) = _HEADER.unpack(header)
    if length == 0 or length > limit:
        raise IPCError(f"invalid worker frame length: {length}")
    try:
        payload = await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise IPCError("worker pipe closed while reading frame body") from exc
    return _decode_envelope(payload)


async def write_frame(
    writer: asyncio.StreamWriter,
    envelope: Envelope,
    *,
    limit: int = MAX_FRAME_BYTES,
) -> None:
    payload = envelope.model_dump_json().encode("utf-8")
    if len(payload) > limit:
        raise IPCError(f"worker frame exceeds {limit} bytes")
    writer.write(_HEADER.pack(len(payload)))
    writer.write(payload)
    await writer.drain()


def read_frame_sync(reader: BinaryIO, *, limit: int = MAX_FRAME_BYTES) -> Envelope:
    """Read the same bounded protocol from a blocking worker stdio pipe."""

    def read_exactly(length: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < length:
            chunk = reader.read(length - len(chunks))
            if not chunk:
                break
            chunks.extend(chunk)
        return bytes(chunks)

    header = read_exactly(_HEADER.size)
    if len(header) != _HEADER.size:
        raise IPCError("worker pipe closed while reading frame header")
    (length,) = _HEADER.unpack(header)
    if length == 0 or length > limit:
        raise IPCError(f"invalid worker frame length: {length}")
    payload = read_exactly(length)
    if len(payload) != length:
        raise IPCError("worker pipe closed while reading frame body")
    return _decode_envelope(payload)


def write_frame_sync(
    writer: BinaryIO,
    envelope: Envelope,
    *,
    limit: int = MAX_FRAME_BYTES,
) -> None:
    """Write one bounded protocol frame to a blocking worker stdio pipe."""

    payload = envelope.model_dump_json().encode("utf-8")
    if len(payload) > limit:
        raise IPCError(f"worker frame exceeds {limit} bytes")
    writer.write(_HEADER.pack(len(payload)))
    writer.write(payload)
    writer.flush()
