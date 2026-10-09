from __future__ import annotations

import importlib.util
import sqlite3
import struct
from collections.abc import Iterator
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "wal_snapshot", Path(__file__).parents[2] / "scripts/agent_wechat_wal_snapshot.py"
)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "source.db"
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE messages(body TEXT)")
    writer.commit()
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    writer.execute("INSERT INTO messages VALUES ('committed')")
    writer.commit()
    try:
        yield path
    finally:
        writer.close()


def zero_seed(wal: bytes) -> bytes:
    data = bytearray(wal)
    page_size = struct.unpack_from(">I", data, 8)[0]
    order = "<" if struct.unpack_from(">I", data)[0] == 0x377F0682 else ">"
    state = (0, 0)
    for offset in range(32, len(data), page_size + 24):
        state = module.checksum(
            bytes(data[offset : offset + 8]) + bytes(data[offset + 24 : offset + 24 + page_size]),
            order,
            state,
        )
        struct.pack_into(">II", data, offset + 16, *state)
    return bytes(data)


@pytest.mark.parametrize("wechat_seed", [False, True])
def test_committed_snapshot_reads_wal_without_modifying_live_files(
    database: Path, wechat_seed: bool
) -> None:
    wal_path = Path(str(database) + "-wal")
    if wechat_seed:
        wal_path.write_bytes(zero_seed(wal_path.read_bytes()))
    originals = [p.read_bytes() for p in (database, wal_path)]
    page_size, _, pages, mode = module.committed_pages(originals[1])
    assert page_size > 0 and pages
    assert mode == ("wechat_zero_seed" if wechat_seed else "standard")
    with module.snapshot(database) as captured:
        assert captured.stat().st_mode & 0o077 == 0
        reader = sqlite3.connect(captured.as_uri() + "?immutable=1", uri=True)
        try:
            assert reader.execute("SELECT body FROM messages").fetchall() == [("committed",)]
        finally:
            reader.close()
    assert not captured.exists()
    assert originals == [p.read_bytes() for p in (database, wal_path)]


@pytest.mark.parametrize("wechat_seed", [False, True])
def test_damage_fails_closed_in_both_checksum_modes(database: Path, wechat_seed: bool) -> None:
    data = Path(str(database) + "-wal").read_bytes()
    if wechat_seed:
        data = zero_seed(data)
    broken = bytearray(data)
    broken[-100] ^= 1
    with pytest.raises(ValueError, match="FrameChecksumMismatch"):
        module.committed_pages(bytes(broken))


def test_valid_uncommitted_tail_never_enters_snapshot(database: Path) -> None:
    data = bytearray(Path(str(database) + "-wal").read_bytes())
    page_size = struct.unpack_from(">I", data, 8)[0]
    offset = len(data) - page_size - 24
    struct.pack_into(">I", data, offset + 4, 0)
    data = zero_seed(bytes(data))
    _, size, pages, mode = module.committed_pages(data)
    assert size == 0 and pages == [] and mode == "wechat_zero_seed"


def test_source_changes_during_capture_are_not_used(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = module.shutil.copyfile

    def changing_copy(source: Path, target: Path) -> None:
        original(source, target)
        with Path(str(source) + "-wal").open("ab") as writer:
            writer.write(b"changed")

    monkeypatch.setattr(module.shutil, "copyfile", changing_copy)
    with pytest.raises(ValueError, match="SourceChanged"):
        with module.snapshot(database):
            pytest.fail("inconsistent snapshot accepted")
