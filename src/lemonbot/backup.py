from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from lemonbot.config.paths import RuntimePaths
from lemonbot.runtime_lock import AlreadyRunningError, RuntimeLock


class BackupError(RuntimeError):
    pass


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _object_inventory(root: Path) -> list[dict[str, Any]]:
    """Validate the exact content-addressed layout before archiving or switching."""

    inventory: list[dict[str, Any]] = []
    if not root.exists():
        return inventory
    if _is_link(root) or not root.is_dir():
        raise BackupError("object store root is not a regular directory")
    for shard in sorted(root.iterdir(), key=lambda item: item.name):
        if (
            _is_link(shard)
            or not shard.is_dir()
            or len(shard.name) != 2
            or any(ch not in "0123456789abcdef" for ch in shard.name)
        ):
            raise BackupError("object store contains an invalid shard")
        for item in sorted(shard.iterdir(), key=lambda child: child.name):
            digest = item.name
            if (
                _is_link(item)
                or not item.is_file()
                or len(digest) != 64
                or any(ch not in "0123456789abcdef" for ch in digest)
                or digest[:2] != shard.name
            ):
                raise BackupError("object store contains a non-addressed object")
            actual = _sha256(item)
            if actual != digest:
                raise BackupError("content-addressed object failed digest validation")
            inventory.append(
                {
                    "name": f"{shard.name}/{digest}",
                    "sha256": digest,
                    "size": item.stat().st_size,
                }
            )
    return inventory


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_snapshot(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise BackupError(f"database does not exist: {source}")
    with (
        closing(sqlite3.connect(source)) as live,
        closing(sqlite3.connect(destination)) as snapshot,
    ):
        live.backup(snapshot)
        result = snapshot.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            raise BackupError(f"database integrity check failed: {result}")


def create_backup(paths: RuntimePaths, output: Path | None = None) -> Path:
    paths.ensure()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = (output or paths.backups / f"lemonbot-{paths.profile}-{timestamp}.zip").resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise BackupError("refusing to overwrite an existing backup")

    with tempfile.TemporaryDirectory(prefix="lemonbot-backup-", dir=destination.parent) as tmp:
        temporary_root = Path(tmp)
        database_copy = temporary_root / f"{paths.profile}.db"
        _sqlite_snapshot(paths.database, database_copy)
        objects = _object_inventory(paths.objects)
        manifest = {
            "format": 1,
            "profile": paths.profile,
            "created_at": datetime.now(UTC).isoformat(),
            "database": {
                "name": database_copy.name,
                "sha256": _sha256(database_copy),
                "size": database_copy.stat().st_size,
            },
            "objects": objects,
        }
        partial = temporary_root / "backup.partial"
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
            archive.write(database_copy, f"database/{database_copy.name}")
            for entry in objects:
                relative = str(entry["name"])
                archive.write(paths.objects / relative, f"objects/{relative}")
        os.replace(partial, destination)
    return destination


def _validated_archive_members(
    archive: zipfile.ZipFile,
    *,
    profile: str,
    max_uncompressed_bytes: int,
) -> tuple[dict[str, zipfile.ZipInfo], dict[str, Any]]:
    members: dict[str, zipfile.ZipInfo] = {}
    folded_names: set[str] = set()
    total = 0
    for info in archive.infolist():
        name = info.filename
        member = PurePosixPath(name)
        mode = info.external_attr >> 16
        if (
            not name
            or "\\" in name
            or "\0" in name
            or member.is_absolute()
            or any(part in {"", ".", ".."} for part in member.parts)
            or info.is_dir()
        ):
            raise BackupError("backup contains an unsafe or unsupported path")
        if stat.S_IFMT(mode) == stat.S_IFLNK:
            raise BackupError("backup contains a symbolic link")
        if info.flag_bits & 0x1:
            raise BackupError("encrypted ZIP members are unsupported")
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise BackupError("backup uses an unsupported compression method")
        folded = name.casefold()
        if folded in folded_names:
            raise BackupError("backup contains duplicate member names")
        folded_names.add(folded)
        members[name] = info
        total += info.file_size
        if total > max_uncompressed_bytes:
            raise BackupError("backup exceeds the restore size limit")

    manifest_info = members.get("manifest.json")
    if manifest_info is None or manifest_info.file_size > 16 * 1024 * 1024:
        raise BackupError("backup manifest is missing or too large")
    try:
        manifest = json.loads(archive.read(manifest_info).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
        raise BackupError("backup manifest is missing or invalid") from exc
    if not isinstance(manifest, dict):
        raise BackupError("backup manifest must be an object")
    if manifest.get("format") != 1 or manifest.get("profile") != profile:
        raise BackupError("backup format or profile does not match this runtime")
    database = manifest.get("database")
    objects = manifest.get("objects")
    if not isinstance(database, dict) or not isinstance(objects, list):
        raise BackupError("backup manifest lacks a complete object inventory")
    database_name = f"{profile}.db"
    if database.get("name") != database_name:
        raise BackupError("backup database name does not match the profile")
    database_digest = database.get("sha256")
    database_size = database.get("size")
    if (
        not isinstance(database_digest, str)
        or len(database_digest) != 64
        or any(ch not in "0123456789abcdef" for ch in database_digest)
        or not isinstance(database_size, int)
        or isinstance(database_size, bool)
        or database_size < 1
    ):
        raise BackupError("backup database metadata is invalid")

    expected_names = {"manifest.json", f"database/{database_name}"}
    seen_objects: set[str] = set()
    normalized_objects: list[dict[str, Any]] = []
    for raw in objects:
        if not isinstance(raw, dict) or set(raw) != {"name", "sha256", "size"}:
            raise BackupError("backup object metadata is invalid")
        object_name = raw.get("name")
        digest = raw.get("sha256")
        size = raw.get("size")
        if (
            not isinstance(object_name, str)
            or not isinstance(digest, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or object_name != f"{digest[:2]}/{digest}"
            or len(digest) != 64
            or any(ch not in "0123456789abcdef" for ch in digest)
            or object_name.casefold() in seen_objects
        ):
            raise BackupError("backup object inventory is not content-addressed")
        seen_objects.add(object_name.casefold())
        normalized_objects.append({"name": object_name, "sha256": digest, "size": size})
        expected_names.add(f"objects/{object_name}")
    if set(members) != expected_names:
        raise BackupError("backup members do not exactly match the manifest")
    if members[f"database/{database_name}"].file_size != database_size:
        raise BackupError("backup database size does not match its manifest")
    for entry in normalized_objects:
        if members[f"objects/{entry['name']}"].file_size != entry["size"]:
            raise BackupError("backup object size does not match its manifest")
    manifest["objects"] = normalized_objects
    return members, manifest


def _extract_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    destination: Path,
    *,
    expected_sha256: str,
    expected_size: int,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    written = 0
    try:
        with archive.open(info) as source, destination.open("xb") as target:
            while chunk := source.read(1024 * 1024):
                written += len(chunk)
                if written > expected_size:
                    raise BackupError("backup member exceeds its declared size")
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    if written != expected_size or digest.hexdigest() != expected_sha256:
        destination.unlink(missing_ok=True)
        raise BackupError("backup member digest or size does not match its manifest")


def _atomic_install_restore(
    paths: RuntimePaths,
    staged_database: Path,
    staged_objects: Path,
    rollback: Path,
) -> None:
    rollback.mkdir()
    old_database = rollback / paths.database.name
    old_objects = rollback / "objects"
    old_sidecars: list[tuple[Path, Path]] = []
    installed_database = False
    installed_objects = False
    moved_database = False
    moved_objects = False
    try:
        if paths.database.exists():
            os.replace(paths.database, old_database)
            moved_database = True
        for suffix in ("-wal", "-shm"):
            active = Path(f"{paths.database}{suffix}")
            if active.exists():
                saved = rollback / active.name
                os.replace(active, saved)
                old_sidecars.append((active, saved))
        if paths.objects.exists():
            os.replace(paths.objects, old_objects)
            moved_objects = True
        os.replace(staged_database, paths.database)
        installed_database = True
        os.replace(staged_objects, paths.objects)
        installed_objects = True
    except BaseException as exc:
        try:
            if installed_objects and paths.objects.exists():
                os.replace(paths.objects, rollback / "failed-new-objects")
            if installed_database and paths.database.exists():
                os.replace(paths.database, rollback / "failed-new.db")
            if moved_objects and old_objects.exists():
                os.replace(old_objects, paths.objects)
            if moved_database and old_database.exists():
                os.replace(old_database, paths.database)
            for active, saved in old_sidecars:
                if saved.exists():
                    os.replace(saved, active)
        except BaseException as rollback_error:
            exc.add_note(f"restore rollback also failed: {rollback_error}")
        raise BackupError("atomic restore switch failed; active state was rolled back") from exc


def _restore_backup_locked(
    paths: RuntimePaths,
    archive_path: Path,
    *,
    preserve_current: bool,
    max_uncompressed_bytes: int,
) -> Path | None:
    preserved: Path | None = None
    if preserve_current and paths.database.exists():
        preserved = create_backup(paths)

    with tempfile.TemporaryDirectory(prefix="lemonbot-restore-", dir=paths.root) as tmp:
        staging = Path(tmp)
        staged_database = staging / "incoming" / f"{paths.profile}.db"
        staged_objects = staging / "incoming-objects"
        staged_objects.mkdir()
        with zipfile.ZipFile(archive_path) as archive:
            members, manifest = _validated_archive_members(
                archive,
                profile=paths.profile,
                max_uncompressed_bytes=max_uncompressed_bytes,
            )
            required = sum(info.file_size for info in members.values())
            if shutil.disk_usage(paths.root).free < required + 64 * 1024 * 1024:
                raise BackupError("insufficient free space to stage the restore safely")
            database = manifest["database"]
            _extract_member(
                archive,
                members[f"database/{paths.profile}.db"],
                staged_database,
                expected_sha256=str(database["sha256"]),
                expected_size=int(database["size"]),
            )
            for entry in manifest["objects"]:
                relative = str(entry["name"])
                _extract_member(
                    archive,
                    members[f"objects/{relative}"],
                    staged_objects / relative,
                    expected_sha256=str(entry["sha256"]),
                    expected_size=int(entry["size"]),
                )
        if _object_inventory(staged_objects) != manifest["objects"]:
            raise BackupError("staged object inventory failed validation")
        with closing(sqlite3.connect(staged_database)) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if result != ("ok",):
                raise BackupError("restored database failed integrity_check")
        _atomic_install_restore(
            paths,
            staged_database,
            staged_objects,
            staging / "rollback",
        )
    return preserved


def restore_backup(
    paths: RuntimePaths,
    archive_path: Path,
    *,
    preserve_current: bool = True,
    max_uncompressed_bytes: int = 20 * 1024 * 1024 * 1024,
) -> Path | None:
    paths.ensure()
    archive_path = archive_path.expanduser().resolve(strict=True)
    try:
        with RuntimeLock(paths.lock_file):
            return _restore_backup_locked(
                paths,
                archive_path,
                preserve_current=preserve_current,
                max_uncompressed_bytes=max_uncompressed_bytes,
            )
    except AlreadyRunningError as exc:
        raise BackupError("Lemonbot is running; stop it before restore") from exc
