#!/usr/bin/env python3
"""Install/inspect the separate localhost SearXNG container as the desktop user."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
from pathlib import Path

from lemonbot.config.settings import load_settings, resolve_config_path

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / "config/searxng/settings.local.yml"
NAME = "lemonbot-search"


def docker(args: argparse.Namespace, *parts: str) -> str:
    prefix = ["sudo", "docker"] if args.sudo else ["docker"]
    return subprocess.run(  # noqa: S603 -- fixed Docker argv, no shell
        prefix + list(parts),
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    ).stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["environment", "status", "stop"])
    parser.add_argument("--sudo", action="store_true")
    parser.add_argument("--image", help="Verified pinned image digest or local image ID")
    parser.add_argument("--config", type=Path, default=resolve_config_path())
    args = parser.parse_args()
    if os.geteuid() == 0:
        raise SystemExit("Run setup as the desktop user; only Docker may use sudo.")
    if args.command == "stop":
        docker(args, "stop", NAME)
    elif args.command == "status":
        data = json.loads(docker(args, "inspect", NAME))[0]
        print(
            json.dumps(
                {
                    "running": data["State"]["Running"],
                    "image_id": data["Image"],
                    "ports": data["HostConfig"]["PortBindings"],
                }
            )
        )
    else:
        args.image = args.image or load_settings(args.config).conversation.search_image
        if not ("@sha256:" in args.image or args.image.startswith("sha256:")):
            raise SystemExit("Use a verified immutable image digest or image ID.")
        info = json.loads(docker(args, "image", "inspect", args.image))[0]
        import platform

        expected = {"aarch64": "arm64", "x86_64": "amd64"}.get(platform.machine())
        if not expected or info["Architecture"] != expected:
            raise SystemExit("Search image architecture does not match this host.")
        if not SETTINGS.exists():
            SETTINGS.parent.mkdir(parents=True, exist_ok=True)
            content = (
                "use_default_settings:\n  engines:\n    keep_only: [bing, yahoo]\n"
                "engines:\n  - name: bing\n    disabled: false\n"
                "  - name: yahoo\n    disabled: false\n"
                "general:\n  debug: false\n"
                "search:\n  formats: [html, json]\n"
                "server:\n  secret_key: " + secrets.token_hex(32) + "\n"
                "  limiter: false\n  image_proxy: false\n"
                "outgoing:\n  request_timeout: 8.0\n  max_request_timeout: 12.0\n"
            )
            fd = os.open(SETTINGS, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(content)
        # Never replace a running container during repeat installation.
        try:
            current = json.loads(docker(args, "inspect", NAME))[0]
        except subprocess.CalledProcessError:
            current = None
        if current is not None:
            env = current["Config"]["Env"]
            mounts = [
                m for m in current["Mounts"] if m["Destination"] == "/etc/searxng/settings.yml"
            ]
            if (
                current["Image"] != info["Id"]
                or current["HostConfig"]["NetworkMode"] != "host"
                or "GRANIAN_HOST=127.0.0.1" not in env
                or "GRANIAN_PORT=6177" not in env
                or len(mounts) != 1
                or mounts[0]["Source"] != str(SETTINGS)
                or mounts[0]["RW"]
                or current["Config"]["User"] != f"{os.getuid()}:{os.getgid()}"
            ):
                raise SystemExit(
                    "Existing search container differs; inspect it before replacement."
                )
            docker(args, "start", NAME)
        else:
            docker(
                args,
                "run",
                "-d",
                "--name",
                NAME,
                "--restart",
                "unless-stopped",
                "--network",
                "host",
                "--memory",
                "1g",
                "--cpus",
                "2",
                "-e",
                "GRANIAN_HOST=127.0.0.1",
                "-e",
                "GRANIAN_PORT=6177",
                "--pids-limit",
                "128",
                "--security-opt",
                "no-new-privileges:true",
                "--user",
                str(os.getuid()) + ":" + str(os.getgid()),
                "--tmpfs",
                "/var/cache/searxng:rw,uid="
                + str(os.getuid())
                + ",gid="
                + str(os.getgid())
                + ",size=64m",
                "-e",
                "FORCE_OWNERSHIP=false",
                "--mount",
                "type=bind,src=" + str(SETTINGS) + ",dst=/etc/searxng/settings.yml,readonly",
                info["Id"],
            )
        print(
            json.dumps(
                {
                    "service": NAME,
                    "url": "http://127.0.0.1:6177",
                    "image_id": info["Id"],
                    "architecture": expected,
                    "settings": str(SETTINGS),
                }
            )
        )


if __name__ == "__main__":
    main()
