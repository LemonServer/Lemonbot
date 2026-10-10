#!/usr/bin/env python3
"""Bounded, local-only agent-wechat reproduction helper for Lemonbot research."""

from __future__ import annotations

# Historical research commands may pin a floating image. For 1.0 installation use
# wechat_setup.py (fixed digest); only open-vnc is referenced by the current handbook.
import argparse
import base64
import fcntl
import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import shutil
import socket
import stat
import struct
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

REPO = "ghcr.io/thisnick/agent-wechat"
SOURCE = "https://github.com/thisnick/agent-wechat.git"
IMAGE_TAG = f"{REPO}:latest"
API = "http://127.0.0.1:6174"
CONTAINER = "lemonbot-agent-wechat-repro"
DATA_VOLUME = "lemonbot-agent-wechat-repro-data"
HOME_VOLUME = "lemonbot-agent-wechat-repro-home"
MARKER_RE = re.compile(r"^LB26_[A-F0-9]{24}$")
STATE = Path.home() / ".local" / "state" / "lemonbot" / "agent-wechat-reproduction"


def fail(message: str, code: int = 2, **details: Any) -> None:
    print(json.dumps({"ok": False, "error": message, **details}, ensure_ascii=True))
    raise SystemExit(code)


def run(
    args: list[str], *, capture: bool = True, timeout: int = 900
) -> subprocess.CompletedProcess[str]:
    try:
        # All callers use fixed executable names and locally constructed arguments.
        result = subprocess.run(  # noqa: S603
            args, check=False, text=True, capture_output=capture, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        fail("ExternalCommandFailed")
    if result.returncode != 0:
        fail("ExternalCommandFailed")
    return result


def ensure_state() -> None:
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    home = Path.home().resolve()
    if STATE.is_symlink() or not STATE.resolve().is_relative_to(home):
        fail("UnsafeResearchStatePath")
    os.chmod(STATE, 0o700)


def private_write(path: Path, data: bytes) -> None:
    temp = path.with_name(path.name + ".tmp")
    if path.is_symlink() or temp.is_symlink():
        fail("UnsafeResearchStateFile")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | nofollow, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    os.chmod(path, 0o600)


def free_bytes() -> int:
    usage = shutil.disk_usage(Path.home())
    return usage.free


def docker_daemon() -> tuple[str, str]:
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
        fail("LinuxAmd64Required")
    binary = shutil.which("docker")
    if not binary:
        fail("DockerUnavailable")
    result = run([binary, "info", "--format", "{{.ServerVersion}}"])
    version = result.stdout.strip()
    if not version:
        fail("DockerDaemonUnavailable")
    return binary, version


def docker_ready() -> str:
    _, version = docker_daemon()
    if free_bytes() < 3 * 1024**3:
        fail("LessThan3GiBFree")
    return version


def lock_path() -> Path:
    return STATE / "artifact-lock.json"


def read_lock() -> dict[str, Any]:
    ensure_state()
    if lock_path().is_symlink():
        fail("UnsafeArtifactLockPath")
    try:
        value = json.loads(lock_path().read_text())
        image = value["image"]
        commit = value["source_commit"]
        if value.get("schema") != 1 or not re.fullmatch(
            r"ghcr\.io/thisnick/agent-wechat@sha256:[0-9a-f]{64}", image
        ):
            raise ValueError
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError
        return value
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        fail("ArtifactLockMissingOrInvalid")


def docker_inspect(image: str) -> dict[str, Any]:
    result = run([shutil.which("docker") or "docker", "image", "inspect", image])
    try:
        value = json.loads(result.stdout)
        return value[0]
    except (ValueError, IndexError, TypeError):
        fail("ImageInspectFailed")


def cmd_preflight(_: argparse.Namespace) -> None:
    version = docker_ready()
    print(
        json.dumps(
            {
                "ok": True,
                "arch": "amd64",
                "docker_server": version,
                "free_bytes": free_bytes(),
                "state_path": str(STATE),
            },
            ensure_ascii=True,
        )
    )


def cmd_pin(_: argparse.Namespace) -> None:
    ensure_state()
    version = docker_ready()
    if free_bytes() < 8 * 1024**3:
        fail("PinRequiresAtLeast8GiBFreeForDownloadHeadroom")
    remote_before = run(["git", "ls-remote", SOURCE, "refs/heads/main"])
    before = re.match(r"^([0-9a-f]{40})\s", remote_before.stdout)
    if not before:
        fail("UpstreamCommitUnavailable")
    run(
        [shutil.which("docker") or "docker", "pull", IMAGE_TAG],
        capture=False,
        timeout=2 * 60 * 60,
    )
    image_data = docker_inspect(IMAGE_TAG)
    refs = image_data.get("RepoDigests") or []
    image_ref = next((r for r in refs if r.startswith(REPO + "@sha256:")), None)
    if not image_ref or not re.fullmatch(re.escape(REPO) + r"@sha256:[0-9a-f]{64}", image_ref):
        fail("OfficialImageDigestUnavailable")
    if image_data.get("Architecture") != "amd64":
        fail("ImageArchitectureMismatch")
    remote_after = run(["git", "ls-remote", SOURCE, "refs/heads/main"])
    match = re.match(r"^([0-9a-f]{40})\s", remote_after.stdout)
    if not match or match.group(1) != before.group(1):
        fail("UpstreamChangedDuringPin")
    commit = match.group(1)
    labels = image_data.get("Config", {}).get("Labels") or {}
    revision = labels.get("org.opencontainers.image.revision")
    source = labels.get("org.opencontainers.image.source")
    if revision and revision != commit:
        fail("ImageSourceRevisionMismatch")
    if source and source.rstrip("/") != "https://github.com/thisnick/agent-wechat":
        fail("ImageSourceMismatch")
    lock = {
        "schema": 1,
        "resolved_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "image_tag_resolved": IMAGE_TAG,
        "image": image_ref,
        "image_id": image_data.get("Id"),
        "arch": image_data.get("Architecture"),
        "source_commit": commit,
        "image_source_label": source,
        "image_revision_label": revision,
        "source_revision_label_matches": bool(revision and revision == commit),
        "docker_server": version,
    }
    ensure_state()
    private_write(lock_path(), (json.dumps(lock, indent=2) + "\n").encode())
    if free_bytes() < 3 * 1024**3:
        fail("LessThan3GiBFreeAfterPull")
    print(
        json.dumps(
            {
                "ok": True,
                "image": image_ref,
                "source_commit": commit,
                "source_revision_verified": lock["source_revision_label_matches"],
                "lock_file": str(lock_path()),
            },
            ensure_ascii=True,
        )
    )


def token_file() -> Path:
    return STATE / "api-token"


def ensure_token() -> None:
    ensure_state()
    path = token_file()
    if path.is_symlink():
        fail("UnsafeApiTokenPath")
    if not path.exists():
        private_write(path, secrets.token_hex(32).encode())
    mode = stat.S_IMODE(path.stat().st_mode)
    token = path.read_text().strip()
    if mode & 0o077 or not re.fullmatch(r"[0-9a-f]{64}", token):
        fail("UnsafeApiTokenFile")


def container_info() -> dict[str, Any] | None:
    docker = shutil.which("docker")
    if not docker:
        fail("DockerUnavailable")
    result = subprocess.run(  # noqa: S603
        [docker, "container", "inspect", CONTAINER], capture_output=True, text=True
    )
    if result.returncode:
        return None
    try:
        return json.loads(result.stdout)[0]
    except (ValueError, IndexError, TypeError):
        fail("ContainerInspectFailed")


def ensure_volume(docker: str, name: str) -> None:
    inspected = subprocess.run(  # noqa: S603
        [docker, "volume", "inspect", name], capture_output=True, text=True
    )
    if inspected.returncode:
        run([docker, "volume", "create", "--label", "io.lemonbot.agent-wechat-repro=1", name])
        inspected = subprocess.run(  # noqa: S603
            [docker, "volume", "inspect", name], capture_output=True, text=True
        )
    try:
        volume = json.loads(inspected.stdout)[0]
    except (ValueError, IndexError, TypeError):
        fail("ResearchVolumeInspectFailed")
    if (volume.get("Labels") or {}).get("io.lemonbot.agent-wechat-repro") != "1":
        fail("RefusingToReuseUnownedVolume")


def validate_container(info: dict[str, Any], lock: dict[str, Any]) -> None:
    config = info.get("Config", {})
    host = info.get("HostConfig", {})
    if config.get("Image") != lock["image"]:
        fail("ExistingContainerUsesDifferentImage")
    if (config.get("Labels") or {}).get("io.lemonbot.agent-wechat-repro") != "1":
        fail("RefusingToReuseUnownedContainer")
    if (host.get("CapDrop") or []) or (host.get("CapAdd") or []) != ["CAP_SYS_PTRACE"]:
        fail("ExistingContainerCapabilitiesMismatch")
    if "seccomp=unconfined" not in (host.get("SecurityOpt") or []):
        fail("ExistingContainerSecurityProfileMismatch")
    if (
        host.get("Privileged")
        or host.get("PidMode") == "host"
        or host.get("IpcMode") == "host"
        or host.get("Devices")
        or host.get("RestartPolicy", {}).get("Name") != "no"
    ):
        fail("ExistingContainerIsolationMismatch")
    bindings = host.get("PortBindings") or {}
    ports = bindings.get("6174/tcp") or []
    if (
        len(ports) != 1
        or ports[0].get("HostIp") != "127.0.0.1"
        or ports[0].get("HostPort") != "6174"
    ):
        fail("ExistingContainerPortBindingMismatch")
    mounts = info.get("Mounts") or []
    expected = {
        "/data": ("volume", DATA_VOLUME, True),
        "/home/wechat": ("volume", HOME_VOLUME, True),
        "/data/auth-token": ("bind", str(token_file()), False),
    }
    if len(mounts) != len(expected):
        fail("ExistingContainerMountMismatch")
    for mount in mounts:
        destination = mount.get("Destination")
        wanted = expected.get(destination)
        if not wanted or mount.get("Type") != wanted[0] or mount.get("RW") != wanted[2]:
            fail("ExistingContainerMountMismatch")
        if destination == "/data/auth-token":
            if mount.get("Source") != wanted[1]:
                fail("ExistingContainerMountMismatch")
        elif mount.get("Name") != wanted[1]:
            fail("ExistingContainerMountMismatch")


def cmd_start(_: argparse.Namespace) -> None:
    docker_ready()
    lock = read_lock()
    ensure_token()
    docker = shutil.which("docker") or "docker"
    existing = container_info()
    if existing:
        validate_container(existing, lock)
        if not existing.get("State", {}).get("Running"):
            run([docker, "start", CONTAINER])
        print(
            json.dumps(
                {"ok": True, "container": CONTAINER, "state": "running", "image": lock["image"]},
                ensure_ascii=True,
            )
        )
        return
    for volume in (DATA_VOLUME, HOME_VOLUME):
        ensure_volume(docker, volume)
    run(
        [
            docker,
            "run",
            "--detach",
            "--name",
            CONTAINER,
            "--pull=never",
            "--restart=no",
            "--stop-timeout=15",
            "--label",
            "io.lemonbot.agent-wechat-repro=1",
            "--cap-add=SYS_PTRACE",
            "--security-opt=seccomp=unconfined",
            "--publish",
            "127.0.0.1:6174:6174",
            "--mount",
            f"type=volume,src={DATA_VOLUME},dst=/data",
            "--mount",
            f"type=volume,src={HOME_VOLUME},dst=/home/wechat",
            "--mount",
            f"type=bind,src={token_file()},dst=/data/auth-token,readonly",
            lock["image"],
        ]
    )
    print(
        json.dumps(
            {
                "ok": True,
                "container": CONTAINER,
                "state": "started",
                "api": API,
                "image": lock["image"],
            },
            ensure_ascii=True,
        )
    )


def cmd_stop(_: argparse.Namespace) -> None:
    docker, _ = docker_daemon()
    info = container_info()
    if not info:
        print(json.dumps({"ok": True, "state": "already_stopped"}))
        return
    if (info.get("Config", {}).get("Labels") or {}).get("io.lemonbot.agent-wechat-repro") != "1":
        fail("RefusingToStopUnownedContainer")
    run([docker, "stop", "--time=15", CONTAINER])
    print(json.dumps({"ok": True, "state": "stopped", "volumes_preserved": True}))


def auth_token() -> str:
    ensure_token()
    return token_file().read_text().strip()


def api(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    payload = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(  # noqa: S310 - fixed loopback HTTP endpoint
        API + path,
        data=payload,
        method=method,
        headers={
            "Authorization": "Bearer " + auth_token(),
            "Content-Type": "application/json",
        },
    )
    try:
        # API is a fixed loopback HTTP endpoint; the path is built internally.
        with urllib.request.urlopen(request, timeout=25) as response:  # noqa: S310
            return json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        fail("LocalApiUnavailableOrInvalidResponse")


LOGIN_EVENT_NAMES = {"status", "qr", "phone_confirm", "login_success", "login_timeout", "error"}
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def login_event_kinds(payload: bytes) -> set[str]:
    """Extract only known event names; never retain event payloads such as QR data."""
    try:
        event = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return set()
    found: set[str] = set()

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                if key in LOGIN_EVENT_NAMES:
                    found.add(key)
                if (
                    key in {"type", "event"}
                    and isinstance(child, str)
                    and child in LOGIN_EVENT_NAMES
                ):
                    found.add(child)
                visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, depth + 1)

    visit(event)
    return found


def websocket_read_exact(stream: socket.socket, length: int, deadline: float) -> bytes:
    data = bytearray()
    while len(data) < length:
        if time.monotonic() >= deadline:
            raise TimeoutError
        try:
            part = stream.recv(length - len(data))
        except TimeoutError:
            continue
        if not part:
            raise EOFError
        data.extend(part)
    return bytes(data)


def websocket_send_frame(stream: socket.socket, opcode: int, payload: bytes = b"") -> None:
    mask = secrets.token_bytes(4)
    header = bytearray([0x80 | opcode])
    size = len(payload)
    if size < 126:
        header.append(0x80 | size)
    elif size < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", size))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", size))
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    stream.sendall(bytes(header) + mask + masked)


def run_login_websocket(timeout_seconds: int) -> dict[str, int]:
    token = urllib.parse.quote(auth_token(), safe="")
    path = f"/api/ws/login?timeoutMs={timeout_seconds * 1000}&newAccount=false&token={token}"
    key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    events: dict[str, int] = {}
    connected = False
    timed_out = False
    stream: socket.socket | None = None
    try:
        stream = socket.create_connection(("127.0.0.1", 6174), timeout=8)
        stream.settimeout(0.5)
        request = (
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:6174\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        stream.sendall(request.encode("ascii"))
        header = bytearray()
        handshake_deadline = time.monotonic() + 8
        while b"\r\n\r\n" not in header and len(header) < 16384:
            header.extend(websocket_read_exact(stream, 1, handshake_deadline))
        lines = bytes(header).split(b"\r\n\r\n", 1)[0].decode("latin1").split("\r\n")
        if not lines or not lines[0].startswith("HTTP/1.1 101 "):
            fail("LoginWebSocketHandshakeRejected")
        response_headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                response_headers[name.strip().lower()] = value.strip()
        accept_digest = hashlib.sha1((key + WEBSOCKET_GUID).encode()).digest()  # noqa: S324 - required by RFC 6455
        expected = base64.b64encode(accept_digest).decode()
        if response_headers.get("sec-websocket-accept") != expected:
            fail("LoginWebSocketHandshakeInvalid")
        connected = True
        deadline = time.monotonic() + timeout_seconds
        fragments = bytearray()
        fragment_opcode: int | None = None
        while time.monotonic() < deadline:
            try:
                first, second = websocket_read_exact(stream, 2, deadline)
            except EOFError:
                break
            final = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            size = second & 0x7F
            if size == 126:
                size = struct.unpack("!H", websocket_read_exact(stream, 2, deadline))[0]
            elif size == 127:
                size = struct.unpack("!Q", websocket_read_exact(stream, 8, deadline))[0]
            if size > 4 * 1024 * 1024:
                fail("LoginWebSocketFrameTooLarge")
            mask = websocket_read_exact(stream, 4, deadline) if masked else b""
            payload = websocket_read_exact(stream, size, deadline) if size else b""
            if masked:
                payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            if opcode == 0x8:
                break
            if opcode == 0x9:
                websocket_send_frame(stream, 0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in {0x1, 0x2}:
                fragment_opcode = opcode
                fragments = bytearray(payload)
            elif opcode == 0x0 and fragment_opcode is not None:
                fragments.extend(payload)
            else:
                continue
            if len(fragments) > 4 * 1024 * 1024:
                fail("LoginWebSocketFrameTooLarge")
            if not final:
                continue
            if fragment_opcode == 0x1:
                for kind in login_event_kinds(bytes(fragments)):
                    events[kind] = events.get(kind, 0) + 1
                if {"qr", "login_success", "login_timeout", "error"}.intersection(events):
                    break
            fragment_opcode = None
            fragments.clear()
    except TimeoutError:
        timed_out = True
    except (OSError, EOFError, ValueError, struct.error):
        fail("LoginWebSocketTransportFailed")
    finally:
        if stream is not None:
            if connected:
                try:
                    websocket_send_frame(stream, 0x8)
                except OSError:
                    pass
            stream.close()
    if not connected:
        fail("LoginWebSocketNotConnected")
    if timed_out:
        events["client_timeout"] = 1
    return events


def cmd_login_flow(args: argparse.Namespace) -> None:
    if not 5 <= args.timeout_seconds <= 120:
        fail("LoginFlowTimeoutOutOfRange")
    docker_daemon()
    info = container_info()
    if not info or not info.get("State", {}).get("Running"):
        fail("ContainerNotRunning")
    auth = api("GET", "/api/status/auth")
    chats = api("GET", "/api/chats?limit=1&offset=0")
    already_ready = (
        isinstance(auth, dict)
        and auth.get("status") == "logged_in"
        and bool(auth.get("loggedInUser"))
        and isinstance(chats, list)
        and bool(chats)
    )
    if already_ready:
        print(json.dumps({"ok": True, "result": "already_initialized", "chat_list_nonempty": True}))
        return
    events = run_login_websocket(args.timeout_seconds)
    auth = api("GET", "/api/status/auth")
    chats = api("GET", "/api/chats?limit=1&offset=0")
    auth_status = auth.get("status") if isinstance(auth, dict) else None
    logged_in_user_present = isinstance(auth, dict) and bool(auth.get("loggedInUser"))
    chat_list_nonempty = isinstance(chats, list) and bool(chats)
    if auth_status == "logged_in" and logged_in_user_present and chat_list_nonempty:
        print(
            json.dumps(
                {
                    "ok": True,
                    "result": "postconditions_met",
                    "auth_status": auth_status,
                    "login_success_event": bool(events.get("login_success")),
                    "chat_list_nonempty": True,
                    "event_counts": events,
                }
            )
        )
        return
    fail(
        "LoginFlowIncomplete",
        auth_status=auth_status,
        logged_in_user_present=logged_in_user_present,
        chat_list_nonempty=chat_list_nonempty,
        qr_required=bool(events.get("qr")),
        event_counts=events,
    )


def testing_chat_ids() -> list[str]:
    matches: list[str] = []
    limit = 100
    for offset in range(0, 1000, limit):
        value = api("GET", f"/api/chats?limit={limit}&offset={offset}")
        if not isinstance(value, list):
            fail("UnexpectedChatList")
        for chat in value:
            if isinstance(chat, dict) and chat.get("name") == "testing":
                chat_id = chat.get("id") or chat.get("username")
                if isinstance(chat_id, str) and chat_id.endswith("@chatroom"):
                    matches.append(chat_id)
        if len(value) < limit:
            break
    else:
        fail("ChatListTooLargeToVerifyUniqueness")
    return sorted(set(matches))


def testing_chat() -> str:
    ensure_state()
    path = STATE / "testing-chat.json"
    if path.is_symlink():
        fail("UnsafeTestingSelectionPath")
    try:
        data = json.loads(path.read_text())
        chat_id = data["id"]
        if not isinstance(chat_id, str) or not chat_id.endswith("@chatroom"):
            raise ValueError
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        fail("TestingGroupNotSelected")
    if testing_chat_ids() != [chat_id]:
        fail("TestingGroupNotUniqueOrChanged")
    return chat_id


def cmd_status(_: argparse.Namespace) -> None:
    docker_daemon()
    info = container_info()
    if not info or not info.get("State", {}).get("Running"):
        fail("ContainerNotRunning")
    result = api("GET", "/api/status/auth")
    status = result.get("status") if isinstance(result, dict) else None
    print(
        json.dumps(
            {
                "ok": True,
                "container": "running",
                "auth_status": status,
                "account_id_suppressed": True,
            },
            ensure_ascii=True,
        )
    )


def cmd_open_vnc(_: argparse.Namespace) -> None:
    docker_daemon()
    info = container_info()
    if not info or not info.get("State", {}).get("Running"):
        fail("ContainerNotRunning")
    url = f"{API}/vnc/?token={urllib.parse.quote(auth_token())}&autoconnect=true"
    # Open only the authenticated local VNC page required by this experiment.
    if not webbrowser.open(url):
        fail(
            "BrowserLaunchFailed; open the local authenticated VNC URL using the private token file"
        )
    print(json.dumps({"ok": True, "browser_open_requested": True, "token_printed": False}))


def cmd_select_testing(_: argparse.Namespace) -> None:
    matches = testing_chat_ids()
    if not matches:
        fail("TestingGroupNotFound")
    if len(matches) > 1:
        fail("TestingGroupAmbiguous", match_count=len(matches))
    ensure_state()
    private_write(STATE / "testing-chat.json", json.dumps({"id": matches[0]}).encode())
    print(json.dumps({"ok": True, "selected": "testing", "group_id_suppressed": True}))


def key_bytes() -> bytes:
    ensure_state()
    path = STATE / "comparison-key"
    if path.is_symlink():
        fail("UnsafeComparisonKeyPath")
    if not path.exists():
        private_write(path, secrets.token_bytes(32))
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        fail("UnsafeComparisonKey")
    return path.read_bytes()


def fingerprint(value: object) -> str | None:
    if value is None or value == "":
        return None
    digest = hmac.new(key_bytes(), str(value).encode(), hashlib.sha256).hexdigest()
    return "ref_" + digest[:20]


def cmd_new_marker(_: argparse.Namespace) -> None:
    print(json.dumps({"marker": "LB26_" + secrets.token_hex(12).upper()}, ensure_ascii=True))


def cmd_observe(args: argparse.Namespace) -> None:
    if not MARKER_RE.fullmatch(args.marker):
        fail("InvalidCanaryMarker")
    group = testing_chat()
    rows = api("GET", f"/api/messages/{urllib.parse.quote(group, safe='')}?limit=100&offset=0")
    if not isinstance(rows, list):
        fail("UnexpectedMessageList")
    matches = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("content"), str):
            continue
        if args.marker not in row["content"]:
            continue
        matches.append(
            {
                "message_ref": fingerprint(row.get("localId")),
                "sender_ref": fingerprint(row.get("sender")),
                "is_self": row.get("isSelf") if type(row.get("isSelf")) is bool else None,
            }
        )
    print(
        json.dumps(
            {"ok": True, "marker": args.marker, "match_count": len(matches), "matches": matches},
            ensure_ascii=True,
        )
    )


def cmd_send(args: argparse.Namespace) -> None:
    if not args.confirm_testing or not MARKER_RE.fullmatch(args.marker):
        fail("ExplicitTestingConfirmationAndValidMarkerRequired")
    group = testing_chat()
    ensure_state()
    ledger_path = STATE / "sent-markers.json"
    if ledger_path.is_symlink():
        fail("UnsafeSendLedgerPath")
    lock_path = STATE / "send.lock"
    if lock_path.is_symlink():
        fail("UnsafeSendLockPath")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "r+") as lock_stream:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
        try:
            ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
        except (OSError, json.JSONDecodeError):
            fail("SendLedgerUnreadable")
        if args.marker in ledger:
            fail("MarkerAlreadyAttemptedNoRetry")
        ledger[args.marker] = {
            "state": "attempted",
            "at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        private_write(ledger_path, json.dumps(ledger).encode())
        try:
            result = api("POST", "/api/messages/send", {"chatId": group, "text": args.marker})
        except SystemExit:
            ledger[args.marker]["state"] = "unknown_no_retry"
            private_write(ledger_path, json.dumps(ledger).encode())
            raise
        accepted = isinstance(result, dict) and result.get("success") is True
        ledger[args.marker]["state"] = "request_accepted" if accepted else "request_failed_no_retry"
        private_write(ledger_path, json.dumps(ledger).encode())
    print(
        json.dumps(
            {
                "ok": accepted,
                "marker": args.marker,
                "result": ledger[args.marker]["state"],
                "delivery_requires_readback": True,
            },
            ensure_ascii=True,
        )
    )
    if not accepted:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (
        ("preflight", cmd_preflight),
        ("pin", cmd_pin),
        ("start", cmd_start),
        ("stop", cmd_stop),
        ("status", cmd_status),
        ("login-flow", cmd_login_flow),
        ("open-vnc", cmd_open_vnc),
        ("select-testing", cmd_select_testing),
        ("new-marker", cmd_new_marker),
    ):
        child = sub.add_parser(name)
        child.set_defaults(func=func)
    login_flow = sub.choices["login-flow"]
    login_flow.add_argument("--timeout-seconds", type=int, default=45)
    observe = sub.add_parser("observe")
    observe.add_argument("--marker", required=True)
    observe.set_defaults(func=cmd_observe)
    send = sub.add_parser("send")
    send.add_argument("--marker", required=True)
    send.add_argument("--confirm-testing", action="store_true")
    send.set_defaults(func=cmd_send)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
