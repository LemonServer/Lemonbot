#!/usr/bin/env python3
"""Pinned, non-destructive setup for Ubuntu desktop deployments (no message sending)."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from lemonbot.config.settings import load_settings
from lemonbot.connectors.wechat_agent import (
    AgentWechatHTTP,
    LabPaused,
    account_id,
    enroll,
    reference,
)
from lemonbot.connectors.wechat_agent_state import (
    load_binding,
    prepare_state,
    private_read,
    private_write,
    save_binding,
)
from lemonbot.connectors.wechat_agent_whitelist import read_whitelist, sync_whitelist
from lemonbot.runtime_lock import RuntimeLock

IMAGE = (
    "ghcr.io/thisnick/agent-wechat@"
    "sha256:97d5a99f933592c15897a7579b4e2b2cbbb5f4dda856228f8271f91dd7cafb43"
)
CONTAINER = "lemonbot-agent-wechat-repro"
VOLUMES = (CONTAINER + "-data", CONTAINER + "-home")
REPO = Path(__file__).resolve().parents[1]
STATE = Path.home() / ".local/state/lemonbot/agent-wechat-lab"
TOKEN = Path.home() / ".local/state/lemonbot/agent-wechat-reproduction/api-token"
BOT_UNIT = "lemonbot-wechat-conversation.service"
BRIDGE_UNIT = "lemonbot-wechat-metadata.service"


def command(*args: str, optional: bool = False, timeout: int = 60) -> str:
    result = subprocess.run(  # noqa: S603 -- argv only, no shell; no secrets in argv
        args, text=True, capture_output=True, timeout=timeout, check=False
    )
    if result.returncode and not optional:
        raise RuntimeError("CommandFailed:" + Path(args[0]).name)
    return result.stdout if result.returncode == 0 else ""


def docker(args: argparse.Namespace, *argv: str, optional: bool = False) -> str:
    prefix = ("sudo", "docker") if args.sudo else ("docker",)
    return command(*prefix, *argv, optional=optional, timeout=7200 if argv[0] == "pull" else 60)


def inspect(args: argparse.Namespace) -> dict:
    value = docker(args, "container", "inspect", CONTAINER, optional=True)
    return json.loads(value)[0] if value else {}


def validate_container(info: dict, token: Path) -> None:
    host = info["HostConfig"]
    if info["Config"]["Image"] != IMAGE:
        raise ValueError("PinnedImageRequired")
    caps = host.get("CapAdd") or []
    if caps not in (["SYS_PTRACE"], ["CAP_SYS_PTRACE"]):
        raise ValueError("UnexpectedCapabilities")
    if (
        host.get("Privileged")
        or host.get("PidMode") == "host"
        or host.get("IpcMode") == "host"
        or host.get("Devices")
        or host.get("CapDrop")
        or host.get("RestartPolicy", {}).get("Name") != "no"
        or host.get("SecurityOpt") != ["seccomp=unconfined"]
    ):
        raise ValueError("UnexpectedIsolationSettings")
    if host.get("PortBindings") != {"6174/tcp": [{"HostIp": "127.0.0.1", "HostPort": "6174"}]}:
        raise ValueError("LocalhostPortsRequired")
    wanted = {
        "/data": ("volume", VOLUMES[0], True),
        "/home/wechat": ("volume", VOLUMES[1], True),
        "/data/auth-token": ("bind", str(token), False),
    }
    mounts = info.get("Mounts", [])
    if len(mounts) != len(wanted):
        raise ValueError("UnexpectedMounts")
    for mount in mounts:
        source = mount.get("Name") if mount["Type"] == "volume" else mount.get("Source")
        if wanted.get(mount["Destination"]) != (mount["Type"], source, mount["RW"]):
            raise ValueError("UnexpectedMounts")


def network(info: dict) -> tuple[str, str]:
    net = info["NetworkSettings"]["Networks"]["bridge"]
    addresses = (net["IPAddress"], net["Gateway"])
    for value in addresses:
        ip = ipaddress.ip_address(value)
        if ip.version != 4 or not ip.is_private or ip.is_unspecified:
            raise ValueError("PrivateNetworkRequired")
    return addresses


def backend(args: argparse.Namespace) -> None:
    info = inspect(args)
    validate_container(info, args.token_file)
    if not info["State"]["Running"]:
        raise ValueError("RunningContainerRequired")
    ip, gateway = network(info)
    binding = args.state_dir / "metadata-binding.json"
    private_read(binding)
    for source, dest in (
        (REPO / "scripts/agent_wechat_wal_snapshot.py", "agent_wechat_wal_snapshot.py"),
        (REPO / "scripts/agent_wechat_metadata.py", "lemonbot-metadata.py"),
        (binding, "lemonbot-conversation.json"),
    ):
        docker(args, "cp", str(source), CONTAINER + ":/data/" + dest)
    stop = (
        "import os,pathlib,signal; "
        "pids=[int(p.name) for p in pathlib.Path('/proc').iterdir() if p.name.isdigit() "
        "and (p/'cmdline').exists() and (p/'cmdline').read_bytes().split(bytes([0]))[:2] "
        "in ([b'python3',b'/tmp/lemonbot-metadata.py'],"
        "[b'python3',b'/data/lemonbot-metadata.py'])]; "
        "[os.kill(pid,signal.SIGTERM) for pid in pids]"
    )
    docker(args, "exec", CONTAINER, "python3", "-c", stop)
    docker(
        args,
        "exec",
        "-d",
        CONTAINER,
        "python3",
        "/data/lemonbot-metadata.py",
        "--bind-address",
        ip,
        "--allowed-peer",
        gateway,
    )


def disk_free(args: argparse.Namespace, root: str) -> int:
    try:
        return shutil.disk_usage(root).free
    except PermissionError:
        if not args.sudo:
            raise ValueError("DockerFilesystemNotReadableUseSudo") from None
        values = command("sudo", "stat", "-f", "--format=%a:%S", root).strip().split(":")
        return int(values[0]) * int(values[1])


def pull_image(args: argparse.Namespace, root: str) -> None:
    prefix = ["sudo", "docker"] if args.sudo else ["docker"]
    # Output remains temporary/private and is discarded; poll actual Docker filesystem.
    with tempfile.TemporaryFile() as output:
        with subprocess.Popen(  # noqa: S603 -- fixed pinned image, argv only
            [*prefix, "pull", IMAGE], stdout=output, stderr=output
        ) as process:
            deadline = time.monotonic() + 7200
            while process.poll() is None:
                if disk_free(args, root) < 3 * 1024**3 or time.monotonic() >= deadline:
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise ValueError("PullStoppedDiskOrTimeout")
                time.sleep(1)
            if process.returncode:
                raise RuntimeError("PinnedImagePullFailed")


def environment(args: argparse.Namespace) -> None:
    if platform.machine() != "x86_64":
        raise ValueError("AMD64Required")
    if args.token_file != args.token_file.resolve():
        raise ValueError("UnsafeTokenPath")
    args.token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not args.token_file.exists():
        private_write(args.token_file, secrets.token_hex(32).encode())
    token = private_read(args.token_file).decode().strip()
    if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        raise ValueError("InvalidPrivateToken")
    # Check the filesystem Docker actually stores images on, not merely the repository.
    root = docker(args, "info", "--format", "{{.DockerRootDir}}").strip()
    free = disk_free(args, root)
    if free < 3 * 1024**3:
        raise ValueError("LessThan3GiBFree")
    info = inspect(args)
    if info:
        validate_container(info, args.token_file)
        if not info["State"]["Running"]:
            docker(args, "start", CONTAINER)
    else:
        if not docker(args, "image", "inspect", IMAGE, optional=True):
            if free < 8 * 1024**3:
                raise ValueError("Need8GiBFreeBeforePull")
            if args.proxy:
                # Docker pull uses daemon networking: refuse to pretend CLI HTTP_PROXY controls it.
                raise ValueError("ConfigureDaemonProxyExplicitlyThenRerunWithoutProxy")
            pull_image(args, root)
        if disk_free(args, root) < 3 * 1024**3:
            raise ValueError("LessThan3GiBFreeAfterPull")
        for volume in VOLUMES:
            if not docker(args, "volume", "inspect", volume, optional=True):
                docker(
                    args, "volume", "create", "--label", "io.lemonbot.agent-wechat-repro=1", volume
                )
            else:
                value = json.loads(docker(args, "volume", "inspect", volume))[0]
                if (value.get("Labels") or {}).get("io.lemonbot.agent-wechat-repro") != "1":
                    raise ValueError("UnownedVolume")
        docker(
            args,
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
            "--env",
            "TZ=Asia/Shanghai",
            "--publish",
            "127.0.0.1:6174:6174",
            "--mount",
            f"type=volume,src={VOLUMES[0]},dst=/data",
            "--mount",
            f"type=volume,src={VOLUMES[1]},dst=/home/wechat",
            "--mount",
            f"type=bind,src={args.token_file},dst=/data/auth-token,readonly",
            IMAGE,
        )
    docker(
        args,
        "exec",
        CONTAINER,
        "sh",
        "-c",
        "ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && "
        "echo Asia/Shanghai > /etc/timezone",
    )
    deadline = time.monotonic() + 90
    while True:
        docker(
            args,
            "exec",
            CONTAINER,
            "sh",
            "-c",
            "DISPLAY=:99 x11vnc -sync -R noviewonly 2>&1",
            optional=True,
        )
        result = docker(
            args,
            "exec",
            CONTAINER,
            "sh",
            "-c",
            "DISPLAY=:99 x11vnc -Q viewonly 2>&1",
            optional=True,
        )
        if "viewonly:0" in result or "viewonly=0" in result:
            break
        if time.monotonic() >= deadline:
            raise ValueError("VNCInputNotVerified")
        time.sleep(2)
    if (args.state_dir / "metadata-binding.json").exists():
        backend(args)
    print("Fixed environment ready. Open authenticated noVNC; login manually. No messages sent.")


async def bind_account(args: argparse.Namespace) -> None:
    key = prepare_state(args.state_dir)
    http = AgentWechatHTTP(private_read(args.token_file).decode().strip())
    try:
        candidate = await enroll(http, key)
        anchor_path = args.state_dir / "binding.json"
        if anchor_path.exists():
            anchor = load_binding(anchor_path)
            if anchor.account_ref != candidate.account_ref or anchor.chat_id != candidate.chat_id:
                raise LabPaused("ExistingBindingChanged")
        account = account_id(await http.request("GET", "/api/status/auth"))
        if reference(key, "account", account) != candidate.account_ref:
            raise LabPaused("AccountChanged")
        rows = await http.request(
            "GET", "/api/messages/" + quote(candidate.chat_id, safe="") + "?limit=100&offset=0"
        )
        from agent_wechat_metadata import canonical_self

        identity = canonical_self(rows, account)
        if account_id(await http.request("GET", "/api/status/auth")) != account:
            raise LabPaused("AccountChanged")
        save_binding(anchor_path, candidate)
        meta_path = args.state_dir / "metadata-binding.json"
        if not meta_path.exists():
            private_write(
                meta_path,
                json.dumps(
                    {"account": account, "chat_ids": [candidate.chat_id], "self_identity": identity}
                ).encode(),
            )
        else:
            saved = json.loads(private_read(meta_path))
            if saved.get("account") != account or saved.get("self_identity") != identity:
                raise LabPaused("ExistingBindingChanged")
        config = args.state_dir / "conversation-runtime.toml"
        whitelist = args.state_dir / "whitelist.toml"
        if not whitelist.exists():
            private_write(whitelist, b'version = 1\ngroups = ["testing"]\ncontacts = []\n')
        if not config.exists():
            text = (REPO / "config/wechat-agent-conversation.example.toml").read_text()
            text = text.replace(
                'token_file = "~/.local/state/lemonbot/agent-wechat-reproduction/api-token"',
                "token_file = " + json.dumps(str(args.token_file)),
            )
            text = text.replace(
                'state_dir = "~/.local/state/lemonbot/agent-wechat-lab"',
                "state_dir = " + json.dumps(str(args.state_dir)),
            )
            text = text.replace(
                'whitelist_file = ""', "whitelist_file = " + json.dumps(str(whitelist))
            )
            load_settings_text_check = args.state_dir / "setup-config-check.toml"
            private_write(load_settings_text_check, text.encode())
            load_settings(load_settings_text_check)
            load_settings_text_check.unlink()
            private_write(config, text.encode())
        # Start only our bridge; neither WeChat nor bot are restarted by binding.
        backend(args)
        ip, _ = network(inspect(args))
        token = private_read(args.token_file).decode().strip()
        async with httpx.AsyncClient(
            base_url=f"http://{ip}:6175",
            trust_env=False,
            headers={"Authorization": "Bearer " + token},
            timeout=90,
        ) as meta:
            await sync_whitelist(
                http, candidate, key, args.state_dir, read_whitelist(whitelist), meta
            )
        private_write(args.state_dir / "paused.json", b'{"reason":"ExplicitResumeRequired"}')
        print("Account anchored; whitelist synchronized; config stays disabled. No messages sent.")
    finally:
        await http.close()


def unit_arg(value: str) -> str:
    if any(c in value for c in "\n\r\x00"):
        raise ValueError("UnsafeServicePath")
    return (
        '"'
        + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
        + '"'
    )


def service(exec_args: list[str], *, bridge: bool = False) -> str:
    unit_arg(str(REPO))  # Validate controls; WorkingDirectory accepts a raw path, not argv quotes.
    dependency = "Requires=" + BRIDGE_UNIT + "\nAfter=" + BRIDGE_UNIT + "\n" if not bridge else ""
    return (
        "[Unit]\nDescription=Lemonbot "
        + ("metadata bridge" if bridge else "text bot")
        + "\n"
        + dependency
        + "\n[Service]\nType=simple\nUMask=0077\nWorkingDirectory="
        + str(REPO).replace("%", "%%")
        + "\nExecStart="
        + " ".join(map(unit_arg, exec_args))
        + "\nRestart=on-failure\nRestartSec=10\nTimeoutStopSec=100\n"
        + "\n[Install]\nWantedBy=default.target\n"
    )


def install_services(args: argparse.Namespace) -> None:
    config = args.state_dir / "conversation-runtime.toml"
    settings = load_settings(config)
    if (
        Path(settings.wechat_agent.state_dir).expanduser() != args.state_dir
        or Path(settings.wechat_agent.token_file).expanduser() != args.token_file
    ):
        raise ValueError("ConfigStateDirectoryMismatch")
    info = inspect(args)
    validate_container(info, args.token_file)
    ip, _ = network(info)
    directory = Path.home() / ".config/systemd/user"
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in (
        (BOT_UNIT, service([sys.executable, "-m", "lemonbot", "run", "--config", str(config)])),
        (
            BRIDGE_UNIT,
            service(
                [
                    sys.executable,
                    str(REPO / "scripts/agent_wechat_metadata.py"),
                    "--forward-ip",
                    ip,
                    "--token-file",
                    str(args.token_file),
                ],
                bridge=True,
            ),
        ),
    ):
        private_write(directory / name, content.encode())
    command("systemctl", "--user", "daemon-reload")
    print(
        "Persistent user units installed, not enabled or started. Bridge IP recorded for this run."
    )


async def doctor(args: argparse.Namespace) -> bool:
    checks: list[dict] = []

    def check(name: str, ok: bool, remedy: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "remedy": "" if ok else remedy})

    check("python", sys.version_info[:2] == (3, 12), "Use Python 3.12 and uv sync --locked.")
    check(
        "platform",
        platform.machine() == "x86_64"
        and 'VERSION_ID="24.04"' in Path("/etc/os-release").read_text(),
        "Use Ubuntu 24.04 AMD64 desktop.",
    )
    check(
        "desktop",
        bool(os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY")),
        "Run in desktop session.",
    )
    try:
        from lemonbot.security.secrets import platform_secret_store

        platform_secret_store()
        from lemonbot.security.secrets import NamespacedSecretStore

        store = NamespacedSecretStore(platform_secret_store(), "lab")
        check(
            "model_key",
            bool(store.get("deepseek_api_key")),
            "Run lemonbot secret set deepseek_api_key --profile lab (hidden input).",
        )
        import secretstorage

        bus = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(bus)
        check("secret_store", not collection.is_locked(), "Unlock the desktop keyring.")
    except Exception:
        check("secret_store", False, "Install/unlock GNOME Keyring and session D-Bus.")
    try:
        info = inspect(args)
        validate_container(info, args.token_file)
        check("container", info["State"]["Running"], "Run environment, then manually log in.")
        root = docker(args, "info", "--format", "{{.DockerRootDir}}").strip()
        check("disk", disk_free(args, root) >= 3 * 1024**3, "Keep at least 3 GiB free.")
        check(
            "docker",
            bool(docker(args, "version", "--format", "{{.Server.Version}}")),
            "Install Docker.",
        )
    except Exception:
        check("container", False, "Run environment; inspect Docker access and localhost mounts.")
    try:
        config = load_settings(args.state_dir / "conversation-runtime.toml")
        check(
            "configured",
            config.wechat_agent.enabled and config.runtime.connector == "wechat_agent",
            "Explicitly enable wechat_agent and deepseek in private runtime TOML.",
        )
        requested = read_whitelist(Path(config.wechat_agent.whitelist_file).expanduser())
        check("whitelist", True, "Check exact names in private whitelist TOML.")
        check("admin_port", config.admin.host == "127.0.0.1", "Admin must bind only localhost.")
        token = private_read(args.token_file).decode().strip()
        http = AgentWechatHTTP(token)
        try:
            anchor = load_binding(args.state_dir / "binding.json")
            auth = account_id(await http.request("GET", "/api/status/auth"))
            key = private_read(args.state_dir / "comparison-key")
            check(
                "account",
                reference(key, "account", auth) == anchor.account_ref,
                "Login the bound account; never replace identity files to bypass a mismatch.",
            )
        finally:
            await http.close()
        from lemonbot.connectors.wechat_agent_conversation import load_targets

        targets = load_targets(
            args.state_dir / "conversation-binding.json", anchor.account_ref, key
        )
        check(
            "whitelist_binding",
            {(t.name, t.is_group) for t in targets}
            == {(name, True) for name in requested.groups}
            | {(name, False) for name in requested.contacts},
            "Stop bot and bind for initial setup, or safe restart to synchronize changed names.",
        )
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:6175",
            trust_env=False,
            headers={"Authorization": "Bearer " + token},
            timeout=90,
        ) as meta:
            response = await meta.post(
                "/read-cycle", json={"anchors": {t.binding.chat_id: None for t in targets}}
            )
            response.raise_for_status()
            body = response.json()
            check(
                "bridge",
                body.get("accountVerified") is True and body.get("account") == auth,
                "Restore metadata bridge with environment and install-services.",
            )
            entries = body.get("targets", [])
            check(
                "messages",
                len(entries) == len(targets) and all(not c.get("error") for c in entries),
                "Open unavailable targets in WeChat; restart to resync exact whitelist names.",
            )
    except Exception:
        check(
            "binding_bridge",
            False,
            "Login, open testing, send one self seed, then bind and install-services.",
        )
    for row in checks:
        print(json.dumps(row))
    return all(row["ok"] for row in checks)


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("environment", "bind", "install-services", "doctor"))
    parser.add_argument("--state-dir", type=Path, default=STATE)
    parser.add_argument("--token-file", type=Path, default=TOKEN)
    parser.add_argument(
        "--sudo",
        action="store_true",
        help="Run Docker commands via sudo; never run this script as root.",
    )
    parser.add_argument(
        "--proxy", help="Explicit daemon proxy reminder; no global Docker configuration is changed."
    )
    args = parser.parse_args()
    args.state_dir = args.state_dir.expanduser().absolute()
    args.token_file = args.token_file.expanduser().absolute()
    if os.getuid() == 0:
        parser.error("Run as the desktop user; use --sudo only for Docker access.")
    try:
        if args.command == "doctor":
            if not asyncio.run(doctor(args)):
                raise SystemExit(1)
        else:
            prepare_state(args.state_dir)
            with RuntimeLock(args.state_dir / "run.lock", persistent=True):
                if args.command == "environment":
                    environment(args)
                elif args.command == "bind":
                    asyncio.run(bind_account(args))
                else:
                    install_services(args)
    except Exception as error:
        # Error type/code only: HTTP bodies, process output and account IDs stay private.
        code = (
            str(error)
            if isinstance(error, (LabPaused, ValueError, RuntimeError))
            else type(error).__name__
        )
        if not code or any(
            c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789:_-"
            for c in code
        ):
            code = type(error).__name__
        print(
            json.dumps(
                {
                    "ok": False,
                    "reason": code,
                    "help": "See docs/deployment.md; no data was deleted.",
                }
            )
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
