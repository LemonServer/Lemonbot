from __future__ import annotations

from pathlib import Path

from lemonbot.tools.object_store import ContentAddressedStore


def test_content_addressed_store_deduplicates(tmp_path: Path) -> None:
    store = ContentAddressedStore(tmp_path)
    first = store.put_bytes(b"same content")
    second = store.put_bytes(b"same content")
    assert first.sha256 == second.sha256
    assert first.path == second.path
    assert first.path.read_bytes() == b"same content"
