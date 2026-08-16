from __future__ import annotations

import asyncio
import io
import json
import struct

import pytest

from lemonbot.ipc.framing import (
    Envelope,
    IPCError,
    read_frame,
    read_frame_sync,
    write_frame,
    write_frame_sync,
)


class BufferWriter:
    def __init__(self) -> None:
        self.buffer = bytearray()

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        return None


async def test_round_trip_strict_json_frame() -> None:
    writer = BufferWriter()
    expected = Envelope(message_type="worker.health", payload={"healthy": True})
    await write_frame(writer, expected)  # type: ignore[arg-type]
    reader = asyncio.StreamReader()
    reader.feed_data(bytes(writer.buffer))
    reader.feed_eof()
    actual = await read_frame(reader)
    assert actual == expected


async def test_rejects_oversized_header_without_allocating_body() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data((2 * 1024 * 1024).to_bytes(4, "big"))
    reader.feed_eof()
    with pytest.raises(IPCError, match="length"):
        await read_frame(reader)


def test_blocking_worker_stdio_uses_the_same_bounded_protocol() -> None:
    buffer = io.BytesIO()
    expected = Envelope(message_type="worker.health", payload={"healthy": True})

    write_frame_sync(buffer, expected)
    buffer.seek(0)

    assert read_frame_sync(buffer) == expected


@pytest.mark.parametrize(
    "raw",
    [
        b'{"protocol_version":1,"protocol_version":1}',
        b'{"protocol_version":NaN}',
    ],
)
def test_worker_frames_reject_ambiguous_json(raw: bytes) -> None:
    framed = io.BytesIO(struct.pack("!I", len(raw)) + raw)

    with pytest.raises(IPCError, match="JSON"):
        read_frame_sync(framed)


def test_worker_envelope_schema_error_is_sanitized() -> None:
    raw = json.dumps(
        {
            "protocol_version": 1,
            "request_id": "not-a-uuid",
            "message_type": "worker.health",
            "payload": {},
        }
    ).encode()
    framed = io.BytesIO(struct.pack("!I", len(raw)) + raw)

    with pytest.raises(IPCError, match="schema validation"):
        read_frame_sync(framed)
