from __future__ import annotations

import importlib.util
import io
import struct
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

_spec = importlib.util.spec_from_file_location(
    "wechat_image", Path(__file__).parents[2] / "scripts/agent_wechat_image.py"
)
assert _spec and _spec.loader
media = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(media)
KEY = "0" * 16  # Synthetic AES input, never an account or API credential.


def encrypted_image(data: bytes, tail: int = 2, xor: int = 93, head_size: int = 32) -> bytes:
    head = data[:head_size]
    encrypted = subprocess.run(  # noqa: S603 -- synthetic key and fixed test executable
        ["openssl", "enc", "-aes-128-ecb", "-K", KEY.encode().hex()],  # noqa: S607
        input=head,
        capture_output=True,
        check=True,
    ).stdout
    return (
        b"\x07\x08V2\x08\x07"
        + struct.pack("<II", len(head), tail)
        + b"\x00"
        + encrypted
        + data[head_size : len(data) - tail]
        + bytes(b ^ xor for b in data[-tail:])
    )


def picture() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (12, 9), "blue").save(stream, format="JPEG")
    return stream.getvalue()


@pytest.mark.parametrize("xor", [None, 93])
def test_native_bounded_decryption_and_static_image(xor: int | None) -> None:
    data = picture()
    decoded = media.decrypt_image(encrypted_image(data), KEY, xor)
    assert decoded == data
    assert media.static_image(decoded) == (data, "jpeg")


def test_padded_jpeg_derives_cipher_from_its_exact_segment_boundary() -> None:
    original = picture()
    data = original[:2] + b"\xff\xe1" + struct.pack(">H", 1402) + b"x" * 1400 + original[2:]
    data += bytes(24)
    encrypted = encrypted_image(data, tail=len(data) - 1024, head_size=1024)
    decoded = media.decrypt_image(encrypted, KEY, None)
    assert decoded == data
    with Image.open(io.BytesIO(decoded)) as image:
        image.load()
        assert image.size == (12, 9)
    # A corrupted length cannot make the reader guess an arbitrary boundary.
    corrupt = bytearray(data)
    corrupt[4:6] = b"\xff\xff"
    with pytest.raises(ValueError, match="ImageXorUnavailable"):
        media.decrypt_image(
            encrypted_image(bytes(corrupt), tail=len(data) - 1024, head_size=1024), KEY, None
        )


@pytest.mark.parametrize("change", ["header", "length", "cipher", "trailer", "key"])
def test_rejects_corrupt_container(change: str) -> None:
    data = bytearray(encrypted_image(picture()))
    key = KEY
    if change == "header":
        data[0] = 0
    elif change == "length":
        struct.pack_into("<I", data, 6, 2**31)
    elif change == "cipher":
        data[15:63] = b"\x00" * 48
    elif change == "trailer":
        data[-1] ^= 1
    else:
        key = "short"
    with pytest.raises(ValueError, match="Image"):
        media.decrypt_image(bytes(data), key, None)


@pytest.mark.parametrize(
    "packed",
    [
        b"",
        b"x" * 32,
        b"\x12\x22\x0a\x20" + b"g" * 32,
        b"\x12\x22\x0a\x20" + b"a" * 32 + b"\x12\x22\x0a\x20" + b"b" * 32,
    ],
)
def test_resource_hash_never_guesses(packed: bytes) -> None:
    with pytest.raises(ValueError, match="Image"):
        media.resource_hash(packed)


def test_resource_hash_accepts_only_exact_protobuf_field() -> None:
    assert media.resource_hash(b"\x12\x22\x0a\x20" + b"a" * 32) == "a" * 32


def test_wxgf_key_derivation_uses_only_same_resource_thumbnail(tmp_path: Path) -> None:
    data = b"wxgf" + b"synthetic content" * 10
    (tmp_path / "bound.dat").write_bytes(encrypted_image(data))
    (tmp_path / "other_t.dat").write_bytes(encrypted_image(picture()))
    with pytest.raises(ValueError, match="ImageXorUnavailable"):
        media.decrypt_resource(tmp_path, Path("bound.dat"), KEY, None)
    (tmp_path / "bound_t.dat").write_bytes(encrypted_image(picture()))
    assert media.decrypt_resource(tmp_path, Path("bound.dat"), KEY, None) == data


@pytest.mark.parametrize("link", ["directory", "file", "traversal"])
def test_file_links_and_traversal_rejected(tmp_path: Path, link: str) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "image").write_bytes(b"secret")
    if link == "directory":
        (root / "shard").symlink_to(outside, target_is_directory=True)
        relative = Path("shard/image")
    elif link == "file":
        (root / "image").symlink_to(outside / "image")
        relative = Path("image")
    else:
        relative = Path("../outside/image")
    with pytest.raises((ValueError, OSError)):
        media.read_file(root, relative)


class Reader:
    def __init__(self, root: Path) -> None:
        self.config = {"account": "self", "chat_ids": ["peer"]}
        self.token = "synthetic"  # noqa: S105 -- no live credentials
        self.service_database_mode = "plaintext"
        self.root = root
        self.calls: list[str] = []
        self.failure: str | None = None
        self.guards = 0

    def api(self, path: str) -> dict[str, str]:
        assert path == "/api/status/auth"  # Never use immutable upstream message lookup.
        self.guards += 1
        account = "changed" if self.failure == "account_after" and self.guards == 2 else "self"
        return {"status": "logged_in", "loggedInUser": account}

    def source_table(self, _chat: str, _account: str) -> tuple[Path, str, str]:
        return (
            self.root / "db_storage/message/message_0.db",
            "synthetic",
            "Msg_" + media.hashlib.md5(b"peer").hexdigest(),
        )

    def query(self, _path: Path, _key: str, sql: str, **_kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(sql)
        if "FROM wechat_keys" in sql:
            return [
                {"db_name": "message_resource.db", "hex_key": "synthetic"},
                {"db_name": "_image_aes", "hex_key": KEY},
            ]
        row = {"local_id": 17, "server_id": 25, "local_type": 3, "create_time": 1791968400}
        if self.failure == "message_type":
            row["local_type"] = 1
        if self.failure == "message_duplicate":
            return [row, row]
        return [row]

    @contextmanager
    def snapshot(self, path: Path):
        assert path == self.root / "db_storage/message/message_resource.db"
        self.calls.append("resource_wal_snapshot")
        yield path

    def _query(self, _path: Path, _key: str, sql: str, **kwargs: Any) -> list[dict[str, Any]]:
        assert kwargs == {"immutable": True}  # Only AFTER a validated snapshot.
        self.calls.append(sql)
        rows = (
            [{"id": 2}]
            if "ChatName2Id" in sql
            else [{"packed": (b"\x12\x22\x0a\x20" + b"a" * 32).hex()}]
        )
        if self.failure == "resource_duplicate" and "MessageResourceInfo" in sql:
            rows *= 2
        return rows


def test_bound_reader_uses_snapshots_and_never_upstream_message_lookup(tmp_path: Path) -> None:
    reader = Reader(tmp_path / "account")
    relative = Path("msg/attach") / media.hashlib.md5(b"peer").hexdigest() / "2026-10/Img"
    folder = reader.root / relative
    folder.mkdir(parents=True)
    data = picture()
    (folder / ("a" * 32 + ".dat")).write_bytes(encrypted_image(data))
    result = media.read_bound_image(reader, "peer", 17)
    assert result["source"] == "validated_wal_snapshot"
    assert result["chatId"] == "peer" and result["localId"] == 17
    assert media.base64.b64decode(result["data"]) == data
    assert "resource_wal_snapshot" in reader.calls
    assert any("local_id=17 LIMIT 2" in call for call in reader.calls)


@pytest.mark.parametrize(
    "failure",
    ["message_type", "message_duplicate", "resource_duplicate", "account_after", "unbound"],
)
def test_source_failures_are_closed(tmp_path: Path, failure: str) -> None:
    reader = Reader(tmp_path)
    reader.failure = failure
    if failure == "account_after":
        relative = Path("msg/attach") / media.hashlib.md5(b"peer").hexdigest() / "2026-10/Img"
        (tmp_path / relative).mkdir(parents=True)
        (tmp_path / relative / ("a" * 32 + ".dat")).write_bytes(encrypted_image(picture()))
    with pytest.raises(ValueError):
        media.read_bound_image(reader, "other" if failure == "unbound" else "peer", 17)
