from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from lemonbot.tools.object_store import ContentAddressedStore, ObjectStoreError


def test_object_store_rejects_non_directory_hash_shard(tmp_path: Path) -> None:
    content = b"untrusted attachment"
    digest = hashlib.sha256(content).hexdigest()
    (tmp_path / digest[:2]).write_bytes(b"not a directory")
    store = ContentAddressedStore(tmp_path)

    with pytest.raises(ObjectStoreError, match="not a directory"):
        store.put_bytes(content)


def test_object_store_does_not_trust_corrupted_existing_object(tmp_path: Path) -> None:
    content = b"expected content"
    digest = hashlib.sha256(content).hexdigest()
    shard = tmp_path / digest[:2]
    shard.mkdir()
    (shard / digest).write_bytes(b"corrupted")
    store = ContentAddressedStore(tmp_path)

    with pytest.raises(ObjectStoreError, match="integrity"):
        store.put_bytes(content)


def test_object_store_rejects_linked_hash_shard(tmp_path: Path) -> None:
    content = b"untrusted attachment"
    digest = hashlib.sha256(content).hexdigest()
    outside = tmp_path / "outside"
    outside.mkdir()
    shard = tmp_path / digest[:2]
    try:
        shard.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable for this Windows user")
    store = ContentAddressedStore(tmp_path)

    with pytest.raises(ObjectStoreError, match="links"):
        store.put_bytes(content)
