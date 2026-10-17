"""Bound image reads from validated encrypted snapshots, without changing WeChat files.

This is Lemonbot's own reader for the pinned client's file format. Database and
image keys stay in the container; only the precisely selected image is returned.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.util
import hashlib
import os
import re
import stat
import struct
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_IMAGE = 10 * 1024 * 1024
_HEX = re.compile(r"[0-9a-f]{32}")


def _fields(data: bytes) -> list[tuple[int, bytes]]:
    """Bounded protobuf wire reader; never scan arbitrary bytes for a filename."""
    offset = 0
    result = []

    def integer() -> int:
        nonlocal offset
        value = 0
        for shift in range(0, 70, 7):
            if offset >= len(data):
                break
            byte = data[offset]
            offset += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError("ImageResourceMalformed")

    if len(data) > 65536:
        raise ValueError("ImageResourceMalformed")
    while offset < len(data):
        tag = integer()
        field, wire = tag >> 3, tag & 7
        if not field:
            raise ValueError("ImageResourceMalformed")
        if wire == 0:
            integer()
            continue
        if wire == 2:
            size = integer()
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
        else:
            raise ValueError("ImageResourceMalformed")
        if size > len(data) - offset:
            raise ValueError("ImageResourceMalformed")
        if wire == 2:
            result.append((field, data[offset : offset + size]))
        offset += size
    return result


def resource_hash(packed: bytes) -> str:
    candidates = [
        value.decode("ascii")
        for field, nested in _fields(packed)
        if field == 2
        for subfield, value in _fields(nested)
        if subfield == 1 and re.fullmatch(rb"[0-9a-f]{32}", value)
    ]
    if len(candidates) != 1:
        raise ValueError("ImageResourceAmbiguous")
    return candidates[0]


def read_file(root: Path, relative: Path) -> bytes:
    """Open every directory and the final file without following links."""
    if relative.is_absolute() or any(p in {"", ".", ".."} for p in relative.parts):
        raise ValueError("ImagePathInvalid")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_IMAGE + 64:
                raise ValueError("ImageFileInvalid")
            data = stream.read(MAX_IMAGE + 65)
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("ImageFileChanged")
            if len(data) != before.st_size:
                raise ValueError("ImageFileChanged")
            return data
    finally:
        os.close(fd)


def _decrypt_ecb(ciphertext: bytes, key: bytes) -> bytes:
    """OpenSSL EVP with PKCS#7 validation; the key is never put in argv."""
    if len(key) != 16 or not ciphertext or len(ciphertext) % 16:
        raise ValueError("ImageCipherInvalid")
    lib = ctypes.CDLL(ctypes.util.find_library("crypto") or "libcrypto.so.3")
    lib.EVP_CIPHER_CTX_new.argtypes = []
    lib.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
    lib.EVP_CIPHER_CTX_free.argtypes = [ctypes.c_void_p]
    lib.EVP_CIPHER_CTX_free.restype = None
    lib.EVP_aes_128_ecb.argtypes = []
    lib.EVP_aes_128_ecb.restype = ctypes.c_void_p
    lib.EVP_DecryptInit_ex.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    lib.EVP_DecryptInit_ex.restype = ctypes.c_int
    lib.EVP_DecryptUpdate.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    lib.EVP_DecryptUpdate.restype = ctypes.c_int
    lib.EVP_DecryptFinal_ex.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
    ]
    lib.EVP_DecryptFinal_ex.restype = ctypes.c_int
    context = lib.EVP_CIPHER_CTX_new()
    if not context:
        raise ValueError("ImageCipherUnavailable")
    output = ctypes.create_string_buffer(len(ciphertext) + 16)
    first, last = ctypes.c_int(), ctypes.c_int()
    try:
        if (
            lib.EVP_DecryptInit_ex(context, lib.EVP_aes_128_ecb(), None, key, None) != 1
            or lib.EVP_DecryptUpdate(
                context, output, ctypes.byref(first), ciphertext, len(ciphertext)
            )
            != 1
            or lib.EVP_DecryptFinal_ex(
                context, ctypes.byref(output, first.value), ctypes.byref(last)
            )
            != 1
        ):
            raise ValueError("ImageDecryptFailed")
        return output.raw[: first.value + last.value]
    finally:
        lib.EVP_CIPHER_CTX_free(context)


def _jpeg_boundary_xor(data: bytes, clear_prefix: bytes, tail_start: int) -> int | None:
    """Use a deterministically located JPEG marker, including padded JPEGs.

    Segment lengths in the decrypted prefix locate the next marker exactly.
    Do not scan entropy for candidates or borrow another image's cipher byte.
    """
    if not clear_prefix.startswith(b"\xff\xd8"):
        return None
    offset = 2
    while offset + 4 <= len(clear_prefix):
        if clear_prefix[offset] != 255:
            return None
        marker = clear_prefix[offset + 1]
        if marker in {0xDA, 0xD9} or not 0xC0 <= marker <= 0xFE:
            return None
        size = int.from_bytes(clear_prefix[offset + 2 : offset + 4], "big")
        if size < 2:
            return None
        offset += 2 + size
        if offset >= len(clear_prefix):
            position = tail_start + offset - len(clear_prefix)
            if position + 4 > len(data):
                return None
            candidate = data[position] ^ 255
            following = data[position + 1] ^ candidate
            length = int.from_bytes(
                bytes(b ^ candidate for b in data[position + 2 : position + 4]), "big"
            )
            if (
                following not in {0xD8, 0xD9}
                and 0xC0 <= following <= 0xFE
                and 2 <= length <= len(data) - position - 2
            ):
                return candidate
            return None
    return None


def decrypt_image(data: bytes, aes: str, xor: int | None) -> bytes:
    if not 15 <= len(data) <= MAX_IMAGE + 64 or data[:6] != b"\x07\x08V2\x08\x07":
        raise ValueError("ImageContainerUnsupported")
    if len(aes) < 16 or not re.fullmatch(r"[0-9a-fA-F]+", aes):
        raise ValueError("ImageKeyInvalid")
    head_size, tail_size = struct.unpack_from("<II", data, 6)
    boundary = 15 + (head_size // 16 + 1) * 16
    if head_size > MAX_IMAGE or boundary > len(data) or tail_size > len(data) - boundary:
        raise ValueError("ImageContainerMalformed")
    head = _decrypt_ecb(data[15:boundary], aes[:16].encode("ascii"))
    if len(head) != head_size:
        raise ValueError("ImageContainerMalformed")
    tail_start = len(data) - tail_size
    if tail_size and xor is None:
        xor = _jpeg_boundary_xor(data, head + data[boundary:tail_start], tail_start)
    if tail_size and xor is None:
        trailer = (
            b"\xff\xd9"
            if head.startswith(b"\xff\xd8")
            else b"IEND\xaeB`\x82"
            if head.startswith(b"\x89PNG\r\n\x1a\n")
            else b""
        )
        if not trailer or tail_size < len(trailer):
            raise ValueError("ImageXorUnavailable")
        values = {left ^ right for left, right in zip(data[-len(trailer) :], trailer, strict=True)}
        if len(values) != 1:
            raise ValueError("ImageXorUnavailable")
        xor = values.pop()
    if xor is not None and (type(xor) is not int or not 0 <= xor <= 255):
        raise ValueError("ImageKeyInvalid")
    result = head + data[boundary:tail_start] + bytes(b ^ (xor or 0) for b in data[tail_start:])
    if not 0 < len(result) <= MAX_IMAGE:
        raise ValueError("ImageSizeExceeded")
    return result


def static_image(data: bytes) -> tuple[bytes, str]:
    if data.startswith(b"wxgf"):
        # Existing pinned-image converter, bounded independently of the core.
        launcher = (
            "import os,resource; "
            "resource.setrlimit(resource.RLIMIT_AS,(1536*1024**2,1536*1024**2)); "
            "resource.setrlimit(resource.RLIMIT_CPU,(10,10)); "
            "resource.setrlimit(resource.RLIMIT_FSIZE,(10*1024**2,10*1024**2)); "
            "os.execv('/opt/tools/media-convert',['media-convert','wxgf2img'])"
        )
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(  # noqa: S603 -- fixed converter, no user arguments
                ["/usr/bin/python3", "-c", launcher],
                input=data,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
            if result.returncode:
                raise ValueError("ImageConversionFailed")
            output.seek(0)
            data = output.read(MAX_IMAGE + 1)
    if not 0 < len(data) <= MAX_IMAGE:
        raise ValueError("ImageSizeExceeded")
    if data.startswith(b"\xff\xd8\xff"):
        return data, "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return data, "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return data, "webp"
    raise ValueError("ImageFormatUnsupported")


def decrypt_resource(root: Path, relative: Path, aes: str, xor: int | None) -> bytes:
    thumbnail = relative.with_name(relative.stem + "_t.dat")
    try:
        encrypted = read_file(root, relative)
    except FileNotFoundError:
        return decrypt_image(read_file(root, thumbnail), aes, xor)
    try:
        return decrypt_image(encrypted, aes, xor)
    except ValueError as error:
        if str(error) != "ImageXorUnavailable" or xor is not None:
            raise
        # A WXGF header has no known image trailer. Derive its account cipher
        # byte from the SAME verified resource's JPEG/PNG thumbnail only. Never
        # search neighbouring images, chats or accounts for a usable key.
        try:
            thumb = read_file(root, thumbnail)
        except FileNotFoundError:
            raise ValueError("ImageXorUnavailable") from None
        clear = decrypt_image(thumb, aes, None)
        tail_size = struct.unpack_from("<I", thumb, 10)[0]
        if not tail_size or not clear:
            raise ValueError("ImageXorUnavailable") from None
        derived = thumb[-1] ^ clear[-1]
        return decrypt_image(encrypted, aes, derived)


def _one(rows: list[dict[str, Any]], code: str) -> dict[str, Any]:
    if len(rows) != 1:
        raise ValueError(code)
    return rows[0]


def read_bound_image(reader: Any, chat: str, local_id: int) -> dict[str, Any]:
    if chat not in reader.config["chat_ids"] or type(local_id) is not int or local_id <= 0:
        raise ValueError("TargetNotBound")
    account = reader.config["account"]

    def guard() -> None:
        auth = reader.api("/api/status/auth")
        if auth.get("status") != "logged_in" or auth.get("loggedInUser") != account:
            raise ValueError("AccountChanged")

    guard()
    path, key, table = reader.source_table(chat, account)
    expected = "Msg_" + hashlib.md5(chat.encode()).hexdigest()  # noqa: S324 -- vendor table convention
    if table != expected:
        raise ValueError("ImageMessageTableMismatch")
    row = _one(
        reader.query(
            path,
            key,
            'SELECT local_id,server_id,local_type,create_time FROM "' + table + '" '  # noqa: S608 -- verified MD5 table and integer
            "WHERE local_id=" + str(local_id) + " LIMIT 2",
        ),
        "ImageMessageMissingOrAmbiguous",
    )
    if row["local_type"] & 0xFFFFFFFF != 3:
        raise ValueError("ImageMessageTypeMismatch")
    directory = path.parent.parent.parent
    account_dir = directory.name
    keys = reader.query(
        Path("/data/agent.db"),
        reader.token,
        "SELECT db_name,hex_key FROM wechat_keys WHERE account_dir='"  # noqa: S608 -- escaped verified directory
        + account_dir.replace("'", "''")
        + "'",
        service=True,
        service_database_mode=reader.service_database_mode,
    )
    resource_key = _one(
        [r for r in keys if r["db_name"] == "message_resource.db"],
        "ImageResourceKeyMissingOrAmbiguous",
    )["hex_key"]
    aes = _one([r for r in keys if r["db_name"] == "_image_aes"], "ImageKeyMissingOrAmbiguous")[
        "hex_key"
    ]
    xor_rows = [r for r in keys if r["db_name"] == "_image_xor"]
    if len(xor_rows) > 1:
        raise ValueError("ImageKeyMissingOrAmbiguous")
    xor = int(xor_rows[0]["hex_key"], 16) if xor_rows else None
    resource_db = path.parent / "message_resource.db"
    with reader.snapshot(resource_db) as captured:
        chat_row = _one(
            reader._query(
                captured,
                resource_key,
                "SELECT rowid AS id FROM ChatName2Id WHERE user_name='"  # noqa: S608 -- escaped exact bound ID
                + chat.replace("'", "''")
                + "' LIMIT 2",
                immutable=True,
            ),
            "ImageResourceChatMissingOrAmbiguous",
        )
        chat_number = chat_row["id"]
        if type(chat_number) is not int or chat_number <= 0:
            raise ValueError("ImageResourceMalformed")
        resource_row = _one(
            reader._query(
                captured,
                resource_key,
                "SELECT hex(packed_info) AS packed FROM MessageResourceInfo WHERE chat_id="  # noqa: S608 -- validated integer IDs
                + str(chat_number)
                + " AND message_local_id="
                + str(local_id)
                + " LIMIT 2",
                immutable=True,
            ),
            "ImageResourceMissingOrAmbiguous",
        )
    digest = resource_hash(bytes.fromhex(resource_row["packed"]))
    if not _HEX.fullmatch(digest):
        raise ValueError("ImageResourceMalformed")
    chat_hash = hashlib.md5(chat.encode()).hexdigest()  # noqa: S324 -- pinned client's path convention
    month = datetime.fromtimestamp(
        row["create_time"],
        timezone.utc,  # noqa: UP017 -- pinned container Python 3.10
    ).strftime("%Y-%m")
    relative = Path("msg/attach") / chat_hash / month / "Img" / (digest + ".dat")
    data, fmt = static_image(decrypt_resource(directory, relative, aes, xor))
    guard()
    return {
        "accountVerified": True,
        "account": account,
        "chatId": chat,
        "localId": local_id,
        "type": "image",
        "data": base64.b64encode(data).decode("ascii"),
        "format": fmt,
        "source": "validated_wal_snapshot",
    }
