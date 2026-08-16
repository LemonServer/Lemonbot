from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


class ObjectStoreError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StoredObject:
    sha256: str
    path: Path
    size: int


class ContentAddressedStore:
    def __init__(self, root: Path, *, max_object_bytes: int = 50 * 1024 * 1024) -> None:
        self._root = root.resolve()
        self._max = max_object_bytes
        self._root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _is_link(path: Path) -> bool:
        return path.is_symlink() or path.is_junction()

    def _directory_for(self, digest: str, *, create: bool) -> Path:
        directory = self._root / digest[:2]
        if create:
            try:
                directory.mkdir(parents=False, exist_ok=True)
            except FileExistsError as exc:
                raise ObjectStoreError("object shard is not a directory") from exc
        if directory.exists():
            if self._is_link(directory) or not directory.is_dir():
                raise ObjectStoreError("links and non-directories are forbidden in object shards")
            try:
                directory.resolve(strict=True).relative_to(self._root)
            except (OSError, ValueError) as exc:
                raise ObjectStoreError("object shard escapes the configured root") from exc
        return directory

    @staticmethod
    def _file_digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def put_bytes(self, content: bytes) -> StoredObject:
        if len(content) > self._max:
            raise ValueError("object exceeds configured size limit")
        digest = hashlib.sha256(content).hexdigest()
        directory = self._directory_for(digest, create=True)
        destination = directory / digest
        if self._is_link(destination) or (destination.exists() and not destination.is_file()):
            raise ObjectStoreError("object path is not a regular file")
        if destination.exists() and (
            destination.stat().st_size != len(content)
            or self._file_digest(destination) != digest
        ):
            raise ObjectStoreError("existing content-addressed object failed integrity check")
        if not destination.exists():
            descriptor, temporary_name = tempfile.mkstemp(prefix="incoming-", dir=directory)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    temporary.replace(destination)
                except FileExistsError:
                    temporary.unlink(missing_ok=True)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        return StoredObject(sha256=digest, path=destination, size=len(content))

    def path_for(self, sha256: str) -> Path:
        if len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256):
            raise ValueError("invalid SHA-256 object identifier")
        destination = self._directory_for(sha256, create=False) / sha256
        if self._is_link(destination) or (destination.exists() and not destination.is_file()):
            raise ObjectStoreError("object path is not a regular file")
        return destination
