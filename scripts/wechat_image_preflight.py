#!/usr/bin/env python3
"""Read-only native dependency checks inside an unbound, pinned image.

This checks executable compatibility, not login, database access or delivery.
It uses no account data, network, keys or host runtime directories.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import hashlib
import importlib
import json
import platform
import struct
import subprocess
from pathlib import Path


def elf_machine(path: Path) -> int:
    with path.open("rb") as stream:
        header = stream.read(20)
    if len(header) != 20 or header[:5] != b"\x7fELF\x02" or header[5] != 1:
        raise ValueError("NativeELFRequired")
    return struct.unpack_from("<H", header, 18)[0]


def report(architecture: str) -> dict:
    machine, elf = {"amd64": ("x86_64", 62), "arm64": ("aarch64", 183)}[architecture]
    if platform.machine() != machine:
        raise ValueError("RuntimeArchitectureMismatch")
    for path in (
        "/opt/wechat/wechat",
        "/opt/agent-server/agent-server",
        "/usr/local/bin/sqlcipher",
    ):
        if elf_machine(Path(path)) != elf:
            raise ValueError("BinaryArchitectureMismatch")
    frida = importlib.import_module("frida")
    native = importlib.import_module("frida._frida")
    if elf_machine(Path(native.__file__)) != elf:
        raise ValueError("FridaArchitectureMismatch")
    # Initialize the native module; do not attach to WeChat or enumerate accounts.
    frida.get_local_device()
    zstd = ctypes.CDLL(ctypes.util.find_library("zstd") or "libzstd.so.1")
    zstd.ZSTD_versionString.restype = ctypes.c_char_p
    cipher = subprocess.run(
        ["/usr/local/bin/sqlcipher", ":memory:"],
        input="PRAGMA cipher_version;\n",
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    ).stdout.strip()
    package = subprocess.run(
        ["/usr/bin/dpkg-query", "-W", "-f=${Version}", "wechat"],
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    ).stdout.strip()
    helper = Path("/opt/tools/extract-keys.py")
    return {
        "ok": True,
        "evidence": "native_dependencies_not_live_channel_acceptance",
        "architecture": architecture,
        "wechat_package_version": package,
        "sqlcipher_version": cipher,
        "frida_version": frida.__version__,
        "zstd_version": zstd.ZSTD_versionString().decode("ascii"),
        "key_extractor_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--architecture", choices=("amd64", "arm64"), required=True)
    args = parser.parse_args()
    try:
        result = report(args.architecture)
    except Exception as error:
        # Only codes/types: never print process output or a future account identifier.
        reason = str(error) if isinstance(error, ValueError) else type(error).__name__
        result = {"ok": False, "reason": reason}
    print(json.dumps(result))
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
