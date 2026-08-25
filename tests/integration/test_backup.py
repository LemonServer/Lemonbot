from __future__ import annotations

import hashlib
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path

import pytest

from lemonbot.backup import BackupError, create_backup, restore_backup
from lemonbot.config.paths import RuntimePaths
from lemonbot.runtime_lock import RuntimeLock


def test_backup_and_non_destructive_restore(tmp_path: Path) -> None:
    paths = RuntimePaths(root=tmp_path / "runtime", profile="lab")
    paths.ensure()
    with closing(sqlite3.connect(paths.database)) as connection:
        connection.execute("CREATE TABLE facts(value TEXT NOT NULL)")
        connection.execute("INSERT INTO facts VALUES ('before')")
        connection.commit()
    content = b"attachment"
    digest = hashlib.sha256(content).hexdigest()
    (paths.objects / digest[:2]).mkdir()
    (paths.objects / digest[:2] / digest).write_bytes(content)

    archive = create_backup(paths, tmp_path / "snapshot.zip")
    with closing(sqlite3.connect(paths.database)) as connection:
        connection.execute("UPDATE facts SET value='after'")
        connection.commit()

    preserved = restore_backup(paths, archive)
    assert preserved is not None and preserved.exists()
    with closing(sqlite3.connect(paths.database)) as connection:
        assert connection.execute("SELECT value FROM facts").fetchone() == ("before",)
    assert (paths.objects / digest[:2] / digest).read_bytes() == content


def _fixture_backup(tmp_path: Path) -> tuple[RuntimePaths, Path, str]:
    paths = RuntimePaths(root=tmp_path / "runtime", profile="lab")
    paths.ensure()
    with closing(sqlite3.connect(paths.database)) as connection:
        connection.execute("CREATE TABLE facts(value TEXT NOT NULL)")
        connection.execute("INSERT INTO facts VALUES ('backup')")
        connection.commit()
    content = b"immutable-object"
    digest = hashlib.sha256(content).hexdigest()
    (paths.objects / digest[:2]).mkdir()
    (paths.objects / digest[:2] / digest).write_bytes(content)
    return paths, create_backup(paths, tmp_path / "source.zip"), digest


def test_restore_rejects_tampered_object_before_touching_active_state(
    tmp_path: Path,
) -> None:
    paths, source, digest = _fixture_backup(tmp_path)
    tampered = tmp_path / "tampered.zip"
    with (
        zipfile.ZipFile(source) as original,
        zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_DEFLATED) as changed,
    ):
        for info in original.infolist():
            body = original.read(info)
            if info.filename.startswith("objects/"):
                body += b"tampered"
            changed.writestr(info.filename, body)
    with closing(sqlite3.connect(paths.database)) as connection:
        connection.execute("UPDATE facts SET value='active'")
        connection.commit()

    with pytest.raises(BackupError, match=r"digest|size"):
        restore_backup(paths, tampered, preserve_current=False)

    with closing(sqlite3.connect(paths.database)) as connection:
        assert connection.execute("SELECT value FROM facts").fetchone() == ("active",)
    assert (paths.objects / digest[:2] / digest).read_bytes() == b"immutable-object"


def test_restore_uses_real_lock_and_ignores_an_unlocked_stale_lock_file(
    tmp_path: Path,
) -> None:
    paths, archive, _ = _fixture_backup(tmp_path)
    paths.lock_file.write_text("stale-pid", encoding="ascii")
    assert restore_backup(paths, archive, preserve_current=False) is None

    with RuntimeLock(paths.lock_file):
        with pytest.raises(BackupError, match="running"):
            restore_backup(paths, archive, preserve_current=False)
