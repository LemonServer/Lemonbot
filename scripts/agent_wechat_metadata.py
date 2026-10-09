#!/usr/bin/env python3
"""Narrow read-only mention bridge for the pinned local WeChat container.

Keys never leave the container. Only bound message IDs and mention booleans
are returned. Host forwarding exposes this bridge exclusively on loopback.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import hashlib
import hmac
import json
import os
import select
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

MAX_SOURCE = 65536


def canonical_self(rows: list[dict[str, Any]], account: str) -> str:
    """Resolve the canonical ID from self metadata, not nickname/prefix guessing."""
    identities = {r.get("sender") for r in rows if r.get("isSelf") is True}
    if len(identities) != 1:
        raise ValueError("CanonicalSelfAmbiguous")
    sender = identities.pop()
    if not isinstance(sender, str) or not sender or len(sender) > 512:
        raise ValueError("CanonicalSelfMissing")
    if account != sender and not account.startswith(sender + "_"):
        raise ValueError("CanonicalSelfMismatch")
    return sender


def mentioned_self(source: str, account: str) -> bool:
    if not source:
        return False
    if len(source) > MAX_SOURCE or "<!DOCTYPE" in source.upper() or "<!ENTITY" in source.upper():
        raise ValueError("InvalidSource")
    root = ET.fromstring(source)  # noqa: S314 -- bounded XML; DTD and entities rejected above
    if root.tag != "msgsource":
        raise ValueError("InvalidSource")
    tags = root.findall("./atuserlist")
    if len(tags) > 1:
        raise ValueError("InvalidSource")
    members = {x.strip() for x in (tags[0].text or "").split(",")} if tags else set()
    return account in members and not members.intersection({"notify@all", "all", "@all"})


def decode_source(value: str, compressed: int) -> str:
    data = bytes.fromhex(value)
    if len(data) > MAX_SOURCE:
        raise ValueError("InvalidSource")
    if compressed and data.startswith(bytes.fromhex("28b52ffd")):
        lib = ctypes.CDLL(ctypes.util.find_library("zstd") or "libzstd.so.1")
        lib.ZSTD_decompress.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        lib.ZSTD_decompress.restype = ctypes.c_size_t
        lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
        lib.ZSTD_isError.restype = ctypes.c_uint
        output = ctypes.create_string_buffer(MAX_SOURCE)
        size = lib.ZSTD_decompress(output, MAX_SOURCE, data, len(data))
        if lib.ZSTD_isError(size):
            raise ValueError("SourceDecompressionFailed")
        data = output.raw[:size]
    return data.decode("utf-8", errors="strict")


class SourceReader:
    def __init__(self, token: str, config: dict[str, Any]) -> None:
        self.token = token
        self.config = config
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.canonical_identity: str | None = None
        self.tables: dict[str, tuple[Path, str, str]] = {}

    def api(self, path: str) -> Any:
        request = urllib.request.Request(
            "http://127.0.0.1:6174" + path, headers={"Authorization": "Bearer " + self.token}
        )
        with self.opener.open(request, timeout=10) as response:
            return json.load(response)

    @staticmethod
    def query(path: Path, key: str, sql: str, *, service: bool = False) -> list[dict[str, Any]]:
        if service:
            prefix = "PRAGMA key='" + key.replace("'", "''") + "';\n"
        else:
            if len(key) != 64 or any(c not in "0123456789abcdefABCDEF" for c in key):
                raise ValueError("InvalidKey")
            prefix = "PRAGMA key=\"x'" + key + "'\"; PRAGMA cipher_compatibility=4;\n"
        result = subprocess.run(  # noqa: S603 -- fixed executable and private internal inputs
            ["/usr/local/bin/sqlcipher", "-readonly", str(path)],
            input=prefix + "PRAGMA query_only=ON;\n.mode json\n" + sql + ";\n",
            text=True,
            capture_output=True,
            timeout=10,
        )
        if result.returncode:
            raise ValueError("SourceQueryFailed")
        start = result.stdout.find("[")
        return json.loads(result.stdout[start:]) if start >= 0 else []

    def source_table(self, chat_id: str, account: str) -> tuple[Path, str, str]:
        if chat_id in self.tables:
            return self.tables[chat_id]
        keys = self.query(
            Path("/data/agent.db"),
            self.token,
            "SELECT account_dir,db_name,hex_key FROM wechat_keys",
            service=True,
        )
        dirs = {
            r["account_dir"]
            for r in keys
            if r["account_dir"] == account or r["account_dir"].startswith(account + "_")
        }
        if len(dirs) != 1:
            raise ValueError("AccountDirectoryAmbiguous")
        directory = dirs.pop()
        roots = [
            Path("/home/wechat/xwechat_files") / directory / "db_storage",
            Path("/home/wechat/Documents/xwechat_files") / directory / "db_storage",
        ]
        base = next((p for p in roots if p.is_dir()), None)
        if base is None:
            raise ValueError("AccountDirectoryMissing")
        table = "Msg_" + hashlib.md5(chat_id.encode()).hexdigest()  # noqa: S324 -- vendor table convention
        matches = []
        for entry in keys:
            name = entry["db_name"]
            if (
                entry["account_dir"] != directory
                or not name.startswith("message_")
                or not name.endswith(".db")
                or any(x in name for x in ("fts", "resource"))
            ):
                continue
            path = base / "message" / name
            found = self.query(
                path,
                entry["hex_key"],
                "SELECT name FROM sqlite_master WHERE name='" + table + "'",  # noqa: S608 -- table name is MD5 hex
            )
            if found:
                matches.append((path, entry["hex_key"]))
        if len(matches) != 1:
            raise ValueError("MessageTableAmbiguous")
        path, key = matches[0]
        self.tables[chat_id] = (path, key, table)
        return path, key, table

    def mentions(self, chat_id: str, offset: int, ids: list[int] | None = None) -> dict[str, Any]:
        if chat_id not in self.config["chat_ids"] or not 0 <= offset <= 900:
            raise ValueError("TargetNotBound")
        auth = self.api("/api/status/auth")
        account = auth.get("loggedInUser")
        if auth.get("status") != "logged_in" or account != self.config["account"]:
            raise ValueError("AccountChanged")
        if self.canonical_identity is None:
            groups = [c for c in self.config["chat_ids"] if c.endswith("@chatroom")]
            if len(groups) != 1:
                raise ValueError("InvalidMentionBinding")
            rows = self.api(
                "/api/messages/" + urllib.parse.quote(groups[0], safe="") + "?limit=100&offset=0"
            )
            if not isinstance(rows, list):
                raise ValueError("CanonicalSelfMissing")
            self.canonical_identity = canonical_self(rows, account)
        if ids is not None:
            if len(ids) > 100 or any(type(i) is not int or i <= 0 for i in ids):
                raise ValueError("InvalidMessageIds")
            if not ids:
                return {"rows": [], "accountVerified": True}
        path, key, table = self.source_table(chat_id, account)
        selection = (
            " WHERE local_id IN (" + ",".join(str(i) for i in ids) + ")"
            if ids is not None
            else " ORDER BY create_time DESC LIMIT 100 OFFSET " + str(offset)
        )
        rows = self.query(
            path,
            key,
            'SELECT local_id,hex(source) AS source,WCDB_CT_source AS compressed FROM "'  # noqa: S608 -- MD5 table name and bounded integer offset only
            + table
            + '"'
            + selection,
        )
        result = [
            {
                "localId": r["local_id"],
                "mentionedSelf": mentioned_self(
                    decode_source(r["source"] or "", r["compressed"] or 0), self.canonical_identity
                ),
            }
            for r in rows
        ]
        if self.api("/api/status/auth").get("loggedInUser") != account:
            raise ValueError("AccountChanged")
        return {"rows": result, "accountVerified": True}


class ChangeNotifications:
    """Linux inotify wakes scoped reads; no message contents enter notifications.

    A DB directory is shared by chats. Notifications therefore mean 'possibly
    changed', not a particular sender/chat. Readers retain their own anchors.
    """

    def __init__(self, directories: list[Path]) -> None:
        self.epoch = uuid4().hex
        self.sequence = 0
        self.ready = False
        self.condition = threading.Condition()
        libc = ctypes.CDLL(None, use_errno=True)
        libc.inotify_init1.argtypes = [ctypes.c_int]
        libc.inotify_init1.restype = ctypes.c_int
        libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        libc.inotify_add_watch.restype = ctypes.c_int
        self.fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise ValueError("NotificationSetupFailed")
        try:
            # MODIFY, CLOSE_WRITE, MOVED_TO, CREATE, DELETE_SELF, MOVE_SELF.
            for directory in directories:
                watch = libc.inotify_add_watch(self.fd, os.fsencode(directory), 0x00000D8A)
                if watch < 0:
                    raise ValueError("NotificationSetupFailed")
            if not directories:
                raise ValueError("NotificationDirectoryMissing")
        except Exception:
            os.close(self.fd)
            raise
        self.ready = True
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()

    def _watch(self) -> None:
        try:
            while self.ready:
                readable, _, _ = select.select([self.fd], [], [], 1)
                if not readable:
                    continue
                data = os.read(self.fd, 65536)
                offset, changed, invalidated = 0, False, False
                while offset + 16 <= len(data):
                    _, mask, _, size = struct.unpack_from("iIII", data, offset)
                    name = data[offset + 16 : offset + 16 + size].split(b"\0", 1)[0]
                    offset += 16 + size
                    # Overflow is a dirty signal, so bounded anchor reconciliation
                    # handles it. Losing a watch fails closed until explicit restart.
                    if mask & (0x400 | 0x800 | 0x8000):
                        invalidated = True
                    if mask & 0x4000 or (
                        name.startswith(b"message_")
                        and (name.endswith(b".db") or name.endswith(b".db-wal"))
                    ):
                        changed = True
                with self.condition:
                    if invalidated:
                        self.ready = False
                    if changed or invalidated:
                        self.sequence += 1
                        self.condition.notify_all()
        except OSError:
            with self.condition:
                self.ready = False
                self.condition.notify_all()

    def wait(self, epoch: str | None, after: int, timeout: float) -> dict[str, Any]:
        if after < 0 or not 0 <= timeout <= 20:
            raise ValueError("InvalidNotificationCursor")
        deadline = time.monotonic() + timeout
        with self.condition:
            while self.ready and epoch == self.epoch and after == self.sequence:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(remaining)
            if not self.ready:
                raise ValueError("NotificationsUnavailable")
            return {
                "ready": True,
                "epoch": self.epoch,
                "sequence": self.sequence,
                "changed": epoch != self.epoch or after != self.sequence,
            }

    def close(self) -> None:
        with self.condition:
            self.ready = False
            self.condition.notify_all()
        self.thread.join(timeout=2)
        os.close(self.fd)


def notification_directories(reader: SourceReader) -> list[Path]:
    # Resolve only this bound account's directory, without touching other accounts.
    account = reader.config["account"]
    roots = (Path("/home/wechat/xwechat_files"), Path("/home/wechat/Documents/xwechat_files"))
    matches = [
        p / "db_storage" / "message"
        for root in roots
        if root.is_dir()
        for p in root.iterdir()
        if (p.name == account or p.name.startswith(account + "_"))
        and (p / "db_storage" / "message").is_dir()
    ]
    if len(matches) != 1:
        raise ValueError("NotificationDirectoryAmbiguous")
    return matches


def serve(
    token: str,
    reader: SourceReader | None,
    upstream: str | None,
    *,
    bind_address: str = "127.0.0.1",
    allowed_peer: str | None = None,
) -> None:
    notifications = ChangeNotifications(notification_directories(reader)) if reader else None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            if allowed_peer and self.client_address[0] != allowed_peer:
                self.send_error(403)
                return
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.send_error(401)
                return
            parsed = urllib.parse.urlsplit(self.path)
            try:
                if parsed.path == "/health":
                    body = {"ready": True}
                elif parsed.path.startswith("/mentions/") or parsed.path == "/changes":
                    args = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                    allowed = (
                        {"after", "epoch", "timeout"}
                        if parsed.path == "/changes"
                        else {"offset", "ids"}
                    )
                    if set(args) - allowed or any(len(v) != 1 for v in args.values()):
                        raise ValueError("InvalidRequest")
                    if upstream:
                        request = urllib.request.Request(
                            "http://" + upstream + ":6175" + self.path,
                            headers={"Authorization": "Bearer " + token},
                        )
                        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                        with opener.open(request, timeout=25) as response:
                            body = json.load(response)
                    elif parsed.path == "/changes":
                        assert notifications is not None
                        body = notifications.wait(
                            args.get("epoch", [None])[0],
                            int(args.get("after", ["0"])[0]),
                            float(args.get("timeout", ["20"])[0]),
                        )
                    else:
                        assert reader is not None
                        body = reader.mentions(
                            urllib.parse.unquote(parsed.path[len("/mentions/") :]),
                            int(args.get("offset", ["0"])[0]),
                            (
                                [int(i) for i in args["ids"][0].split(",") if i]
                                if "ids" in args
                                else None
                            ),
                        )
                else:
                    self.send_error(404)
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
            except Exception:
                data = b'{"error":"MetadataBridgeUnavailable"}'
                self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    try:
        ThreadingHTTPServer((bind_address, 6175), Handler).serve_forever()
    finally:
        if notifications:
            notifications.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", type=Path, default=Path("/data/auth-token"))
    parser.add_argument("--binding", type=Path, default=Path("/data/lemonbot-conversation.json"))
    parser.add_argument("--forward-ip")
    parser.add_argument("--bind-address", default="127.0.0.1")
    parser.add_argument("--allowed-peer")
    args = parser.parse_args()
    if args.forward_ip:
        import ipaddress

        address = ipaddress.ip_address(args.forward_ip)
        if not address.is_private or address.version != 4:
            parser.error("ExpectedPrivateContainerAddress")
    import ipaddress

    bind = ipaddress.ip_address(args.bind_address)
    if not bind.is_loopback:
        if bind.is_unspecified or not bind.is_private or not args.allowed_peer:
            parser.error("RestrictedBindRequiresHostPeer")
        if not ipaddress.ip_address(args.allowed_peer).is_private:
            parser.error("ExpectedPrivateHostPeer")
    if args.forward_ip and not bind.is_loopback:
        parser.error("HostForwardMustBeLoopback")
    token = args.token_file.read_text().strip()
    reader = None if args.forward_ip else SourceReader(token, json.loads(args.binding.read_text()))
    serve(
        token,
        reader,
        args.forward_ip,
        bind_address=args.bind_address,
        allowed_peer=args.allowed_peer,
    )


if __name__ == "__main__":
    main()
