#!/usr/bin/env python3
"""Install/start only Lemonbot's metadata bridge on the pinned experiment.

No WeChat restart, messages, image pull, or data removal. Run after login and
binding. sudo may prompt in the terminal; credentials are never arguments.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import subprocess
from pathlib import Path

CONTAINER = "lemonbot-agent-wechat-repro"
DIGEST = "sha256:97d5a99f933592c15897a7579b4e2b2cbbb5f4dda856228f8271f91dd7cafb43"


def run(*args: str, capture: bool = False) -> str:
    result = subprocess.run(  # noqa: S603 -- fixed commands; validated private paths/IPs
        list(args), check=True, text=True, capture_output=capture, timeout=30
    )
    return result.stdout if capture else ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-dir", type=Path, default=Path.home() / ".local/state/lemonbot/agent-wechat-lab"
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path.home() / ".local/state/lemonbot/agent-wechat-reproduction/api-token",
    )
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    inspected = json.loads(run("sudo", "docker", "inspect", CONTAINER, capture=True))[0]
    if not inspected["State"]["Running"] or not inspected["Config"]["Image"].endswith("@" + DIGEST):
        raise SystemExit("PinnedRunningContainerRequired")
    network = inspected["NetworkSettings"]["Networks"]["bridge"]
    ip, gateway = network["IPAddress"], network["Gateway"]
    for value in (ip, gateway):
        address = ipaddress.ip_address(value)
        if address.version != 4 or not address.is_private or address.is_unspecified:
            raise SystemExit("PrivateContainerNetworkRequired")
    binding = args.state_dir / "metadata-binding.json"
    if not binding.is_file() or binding.is_symlink() or binding.stat().st_mode & 0o077:
        raise SystemExit("PrivateMetadataBindingRequired")
    run(
        "sudo",
        "docker",
        "cp",
        str(repo / "scripts/agent_wechat_wal_snapshot.py"),
        CONTAINER + ":/data/agent_wechat_wal_snapshot.py",
    )
    run(
        "sudo",
        "docker",
        "cp",
        str(repo / "scripts/agent_wechat_metadata.py"),
        CONTAINER + ":/data/lemonbot-metadata.py",
    )
    run("sudo", "docker", "cp", str(binding), CONTAINER + ":/data/lemonbot-conversation.json")
    # Match only our script's exact executable arguments, never WeChat processes.
    stop = (
        "import os,pathlib,signal; "
        "pids=[int(p.name) for p in pathlib.Path('/proc').iterdir() if p.name.isdigit() "
        "and (p/'cmdline').exists() and (p/'cmdline').read_bytes().split(bytes([0]))[:2] "
        "in ([b'python3',b'/tmp/lemonbot-metadata.py'],"
        "[b'python3',b'/data/lemonbot-metadata.py'])]; "
        "[os.kill(pid,signal.SIGTERM) for pid in pids]"
    )
    run("sudo", "docker", "exec", CONTAINER, "python3", "-c", stop)
    run(
        "sudo",
        "docker",
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
    run("systemctl", "--user", "stop", "lemonbot-wechat-metadata-20261009.service")
    run(
        "systemd-run",
        "--user",
        "--collect",
        "--unit=lemonbot-wechat-metadata-20261009.service",
        "--property=Restart=on-failure",
        "--property=UMask=0077",
        "/usr/bin/python3",
        str(repo / "scripts/agent_wechat_metadata.py"),
        "--forward-ip",
        ip,
        "--token-file",
        str(args.token_file),
    )
    print("Metadata bridge started on localhost; no messages were sent.")


if __name__ == "__main__":
    main()
