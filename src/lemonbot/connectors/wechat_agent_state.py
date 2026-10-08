"""Private binding storage shared by the lab and runtime."""

from __future__ import annotations

import json
import os
import secrets
import stat
from dataclasses import asdict
from pathlib import Path

from lemonbot.connectors.wechat_agent import Binding, LabPaused

DEFAULT_STATE = Path.home() / ".local/state/lemonbot/agent-wechat-lab"


def private_read(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > 65_536
        ):
            raise LabPaused("UnsafePrivateFile")
        return stream.read()


def private_write(path: Path, value: bytes) -> None:
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def prepare_state(path: Path) -> bytes:
    if path.absolute() != path.resolve():
        raise LabPaused("UnsafeStateDirectory")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_uid != os.getuid():
        raise LabPaused("UnsafeStateDirectory")
    os.chmod(path, 0o700)
    key_path = path / "comparison-key"
    if not key_path.exists():
        private_write(key_path, secrets.token_bytes(32))
    key = private_read(key_path)
    if len(key) != 32:
        raise LabPaused("InvalidComparisonKey")
    return key


def save_binding(path: Path, binding: Binding) -> None:
    if path.exists():
        previous = load_binding(path)
        if (
            previous.account_ref != binding.account_ref
            or previous.chat_id != binding.chat_id
            or previous.chat_ref != binding.chat_ref
        ):
            raise LabPaused("ExistingBindingChanged")
    private_write(path, json.dumps(asdict(binding)).encode())


def load_binding(path: Path) -> Binding:
    try:
        value = json.loads(private_read(path))
        binding = Binding(
            account_ref=value["account_ref"],
            chat_id=value["chat_id"],
            chat_ref=value["chat_ref"],
            header_path=tuple(value["header_path"]),
            header_roles=tuple(value["header_roles"]),
        )
        if (
            not isinstance(binding.chat_id, str)
            or not binding.chat_id.endswith("@chatroom")
            or not binding.account_ref.startswith("account_")
            or not binding.chat_ref.startswith("chat_")
            or any(type(i) is not int or i < 0 for i in binding.header_path)
            or any(not isinstance(role, str) for role in binding.header_roles)
        ):
            raise ValueError
        return binding
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise LabPaused("InvalidOrMissingBinding") from None
