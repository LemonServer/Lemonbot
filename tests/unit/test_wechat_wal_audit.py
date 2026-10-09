from __future__ import annotations

import importlib.util
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "wal_audit", Path(__file__).parents[2] / "scripts/agent_wechat_wal_audit.py"
)
assert spec and spec.loader
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


@pytest.fixture
def real_wal(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "synthetic.db"
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE messages(body TEXT)")
    writer.commit()
    writer.execute("INSERT INTO messages VALUES ('synthetic')")
    writer.commit()
    try:
        yield Path(str(path) + "-wal")
    finally:
        writer.close()


def test_actual_sqlite_wal_validates_without_changing_file(real_wal: Path) -> None:
    before = real_wal.read_bytes()
    result = audit.audit_path(real_wal)
    assert result["status"] == "valid"
    assert result["last_commit_frame"] > 0
    assert real_wal.read_bytes() == before


def test_first_frame_damage_cannot_be_accepted_as_committed(real_wal: Path) -> None:
    data = bytearray(real_wal.read_bytes())
    data[32 + 24 + 100] ^= 1
    result = audit.audit_wal(bytes(data))
    assert result["status"] == "frame_checksum_mismatch"
    assert result["valid_frames"] == result["last_commit_frame"] == 0


def test_header_damage_is_reported_separately(real_wal: Path) -> None:
    data = bytearray(real_wal.read_bytes())
    data[20] ^= 1
    assert audit.audit_wal(bytes(data))["status"] == "header_checksum_mismatch"


def test_recycled_tail_is_not_mistaken_for_a_corrupt_current_frame(real_wal: Path) -> None:
    data = bytearray(real_wal.read_bytes())
    data[32 + 8] ^= 1
    result = audit.audit_wal(bytes(data))
    assert result["status"] == "recycled_tail"
    assert result["last_commit_frame"] == 0


def test_unbounded_or_partial_input_is_rejected() -> None:
    with pytest.raises(ValueError, match="HeaderIncomplete"):
        audit.audit_wal(b"short")
    assert audit.audit_wal(b"")["status"] == "empty"
