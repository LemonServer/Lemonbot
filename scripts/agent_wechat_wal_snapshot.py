"""Validated, encrypted snapshots for the pinned WeChat WAL reader.

The observed writer seeds frame checksums with zero. SQLite seeds them with
the WAL-header checksum. Both modes are validated in full; invalid frames are
never overlaid. Only committed pages enter a private temporary copy.
"""

from __future__ import annotations

import os
import shutil
import struct
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MAX_BYTES = 128 * 1024 * 1024


def checksum(data: bytes, order: str, state: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    first, second = state
    for left, right in struct.iter_unpack(order + "II", data):
        first = (first + left + second) & 0xFFFFFFFF
        second = (second + right + first) & 0xFFFFFFFF
    return first, second


def committed_pages(wal: bytes) -> tuple[int, int, list[tuple[int, bytes]], str]:
    if not wal:
        return 0, 0, [], "empty"
    if len(wal) < 32 or len(wal) > MAX_BYTES:
        raise ValueError("WalSizeInvalid")
    magic, version, page_size = struct.unpack_from(">III", wal)
    if magic not in (0x377F0682, 0x377F0683) or version != 3007000:
        raise ValueError("WalFormatUnsupported")
    if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
        raise ValueError("WalPageSizeInvalid")
    order = "<" if magic == 0x377F0682 else ">"
    state = checksum(wal[:24], order)
    if state != struct.unpack_from(">II", wal, 24):
        raise ValueError("WalHeaderChecksumMismatch")
    mode = "standard"
    frames: list[tuple[int, bytes]] = []
    last_commit, committed_size = 0, 0
    frame_size = page_size + 24
    for offset in range(32, len(wal) - frame_size + 1, frame_size):
        header = wal[offset : offset + 24]
        if header[8:16] != wal[16:24]:
            break  # Recycled frames are outside the current WAL epoch.
        page, size = struct.unpack_from(">II", header)
        if not page or page * page_size > MAX_BYTES or size * page_size > MAX_BYTES:
            raise ValueError("WalPageBoundExceeded")
        payload = wal[offset + 24 : offset + frame_size]
        block = header[:8] + payload
        actual = struct.unpack_from(">II", header, 16)
        expected = checksum(block, order, state)
        if not frames and expected != actual and checksum(block, order) == actual:
            mode = "wechat_zero_seed"
            expected = actual
        if expected != actual:
            raise ValueError("WalFrameChecksumMismatch")
        state = actual
        frames.append((page, payload))
        if size:
            last_commit, committed_size = len(frames), size
    return page_size, committed_size, frames[:last_commit], mode


def _stamp(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    if stat.st_size > MAX_BYTES:
        raise ValueError("SnapshotSizeLimit")
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


@contextmanager
def snapshot(path: Path) -> Iterator[Path]:
    wal_path = Path(str(path) + "-wal")
    before = (_stamp(path), _stamp(wal_path))
    if before[0] is None:
        raise ValueError("SnapshotSourceMissing")
    wal = wal_path.read_bytes() if before[1] is not None else b""
    page_size, committed_size, pages, _mode = committed_pages(wal)
    with tempfile.TemporaryDirectory(prefix="lemonbot-encrypted-snapshot-") as directory:
        os.chmod(directory, 0o700)
        target = Path(directory) / "snapshot.db"
        shutil.copyfile(path, target)
        target.chmod(0o600)
        if before != (_stamp(path), _stamp(wal_path)):
            raise ValueError("SnapshotSourceChanged")
        if pages:
            with target.open("r+b") as stream:
                for page, payload in pages:
                    stream.seek((page - 1) * page_size)
                    stream.write(payload)
                stream.truncate(committed_size * page_size)
        yield target
