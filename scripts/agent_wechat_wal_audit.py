#!/usr/bin/env python3
"""Audit an encrypted SQLite WAL without decrypting or changing it.

Run inside the experiment container with the bound message WAL path. Output
contains only format/integrity counts. It never includes paths or page data.
"""

from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path

MAX_WAL_BYTES = 64 * 1024 * 1024


def checksum(data: bytes, byte_order: str, state: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    first, second = state
    for left, right in struct.iter_unpack(byte_order + "II", data):
        first = (first + left + second) & 0xFFFFFFFF
        second = (second + right + first) & 0xFFFFFFFF
    return first, second


def audit_wal(data: bytes) -> dict[str, object]:
    if len(data) > MAX_WAL_BYTES:
        raise ValueError("WalSizeLimit")
    if not data:
        return {"status": "empty", "valid_frames": 0, "last_commit_frame": 0}
    if len(data) < 32:
        raise ValueError("WalHeaderIncomplete")
    magic, version, page_size = struct.unpack_from(">III", data)
    if magic not in (0x377F0682, 0x377F0683) or version != 3007000:
        raise ValueError("WalFormatUnsupported")
    if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
        raise ValueError("WalPageSizeInvalid")
    order = "<" if magic == 0x377F0682 else ">"
    state = checksum(data[:24], order)
    if state != struct.unpack_from(">II", data, 24):
        return {"status": "header_checksum_mismatch", "valid_frames": 0, "last_commit_frame": 0}
    valid, committed = 0, 0
    status = "valid"
    frame_size = 24 + page_size
    for offset in range(32, len(data) - frame_size + 1, frame_size):
        header = data[offset : offset + 24]
        if header[8:16] != data[16:24]:
            # Recycled tail frames belong to an earlier epoch, not corruption.
            status = "recycled_tail"
            break
        page, size = struct.unpack_from(">II", header)
        if not page:
            status = "frame_page_invalid"
            break
        state = checksum(header[:8] + data[offset + 24 : offset + frame_size], order, state)
        if state != struct.unpack_from(">II", header, 16):
            status = "frame_checksum_mismatch"
            break
        valid += 1
        if size:
            committed = valid
    return {
        "status": status,
        "valid_frames": valid,
        "last_commit_frame": committed,
        "uncommitted_valid_frames": valid - committed,
        "incomplete_tail_bytes": (len(data) - 32) % frame_size,
    }


def audit_path(path: Path) -> dict[str, object]:
    before = path.stat()
    if before.st_size > MAX_WAL_BYTES:
        raise ValueError("WalSizeLimit")
    with path.open("rb") as stream:
        data = stream.read(MAX_WAL_BYTES + 1)
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError("WalChangedDuringCapture")
    return audit_wal(data)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wal", type=Path)
    args = parser.parse_args()
    try:
        result = audit_path(args.wal)
    except (OSError, ValueError) as error:
        # OSError text may contain a private account path; never print it.
        code = str(error) if isinstance(error, ValueError) else "WalReadUnavailable"
        print(json.dumps({"status": "unavailable", "reason": code}))
        raise SystemExit(2) from None
    print(json.dumps(result))
    if result["status"] in {
        "header_checksum_mismatch",
        "frame_checksum_mismatch",
        "frame_page_invalid",
    }:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
