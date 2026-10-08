#!/usr/bin/env python3
"""Restore the pinned historical experiment, preserving all existing volumes.

Run with sudo. This helper neither logs account data nor sends WeChat messages.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

import agent_wechat_repro as repro

IMAGE = (
    "ghcr.io/thisnick/agent-wechat@sha256:"
    "97d5a99f933592c15897a7579b4e2b2cbbb5f4dda856228f8271f91dd7cafb43"
)
PROXY_FILE = Path('/run/systemd/system/docker.service.d/http-proxy.conf')
PROXY_CONFIG = (
    '[Service]\nEnvironment="HTTP_PROXY=http://127.0.0.1:7897" '
    '"HTTPS_PROXY=http://127.0.0.1:7897" "NO_PROXY=localhost,127.0.0.1,::1"\n'
)


def report(**data: Any) -> None:
    print(json.dumps(data), flush=True)


def command(args: list[str], *, required: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, capture_output=True, text=True, timeout=60)  # noqa: S603
    if required and result.returncode:
        raise RuntimeError('RuntimeCommandFailed')
    return result


def pull_pids() -> list[int]:
    result = command(
        ['pgrep', '-f', '(^|/)docker pull --quiet ' + re.escape(IMAGE)], required=False,
    )
    return [int(s) for s in result.stdout.split() if s.isdigit()]


def remove_proxy() -> None:
    if not PROXY_FILE.exists():
        return
    if PROXY_FILE.read_text() != PROXY_CONFIG:
        raise RuntimeError('TemporaryProxyChanged')
    if command(['docker', 'ps', '-q']).stdout.strip():
        raise RuntimeError('RefusingDaemonRestartWithRunningContainers')
    PROXY_FILE.unlink()
    command(['systemctl', 'daemon-reload'])
    command(['systemctl', 'restart', 'docker'])
    report(temporary_proxy_removed=True)


def wait_for_image() -> None:
    while True:
        if shutil.disk_usage('/').free < 3 * 1024**3:
            for pid in pull_pids():
                os.kill(pid, signal.SIGINT)
            raise RuntimeError('DiskSpaceProtection')
        image = command(['docker', 'image', 'inspect', IMAGE], required=False)
        running = pull_pids()
        if not running:
            if image.returncode:
                raise RuntimeError('FixedImageDownloadIncomplete')
            return
        ingest = Path('/var/lib/containerd/io.containerd.content.v1.content/ingest')
        size = sum(p.stat().st_size for p in ingest.rglob('data') if p.is_file())
        report(download_running=True, partial_layer_bytes=size,
               free_gib=round(shutil.disk_usage('/').free / 1024**3, 2))
        time.sleep(30)


def restore() -> None:
    user = pwd.getpwnam('lemon')
    repro.STATE = Path(user.pw_dir) / '.local/state/lemonbot/agent-wechat-reproduction'
    lock = json.loads((repro.STATE / 'artifact-lock.json').read_text())
    if lock.get('image') != IMAGE:
        raise RuntimeError('ArtifactLockChanged')
    token = repro.token_file()
    info = token.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != user.pw_uid
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise RuntimeError('UnsafePersistentToken')
    images = json.loads(command(['docker', 'image', 'inspect', IMAGE]).stdout)
    if images[0].get('Architecture') != 'amd64' or images[0].get('Os') != 'linux':
        raise RuntimeError('ImagePlatformMismatch')
    for name in (repro.DATA_VOLUME, repro.HOME_VOLUME):
        volume = json.loads(command(['docker', 'volume', 'inspect', name]).stdout)[0]
        if (volume.get('Labels') or {}).get('io.lemonbot.agent-wechat-repro') != '1':
            raise RuntimeError('OriginalVolumeIdentityMismatch')
    existing = repro.container_info()
    if existing:
        repro.validate_container(existing, lock)
        if not existing.get('State', {}).get('Running'):
            command(['docker', 'start', repro.CONTAINER])
    else:
        command([
            'docker', 'run', '--detach', '--name', repro.CONTAINER, '--pull=never',
            '--restart=no', '--stop-timeout=15', '--label', 'io.lemonbot.agent-wechat-repro=1',
            '--cap-add=SYS_PTRACE', '--security-opt=seccomp=unconfined',
            '--publish', '127.0.0.1:6174:6174',
            '--mount', f'type=volume,src={repro.DATA_VOLUME},dst=/data',
            '--mount', f'type=volume,src={repro.HOME_VOLUME},dst=/home/wechat',
            '--mount', f'type=bind,src={token},dst=/data/auth-token,readonly', IMAGE,
        ])
    current = repro.container_info()
    if not current:
        raise RuntimeError('ContainerMissingAfterStart')
    repro.validate_container(current, lock)
    report(container_started=True, fixed_image=True, original_volumes=True,
           started_at=current.get('State', {}).get('StartedAt'))
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        result = command([
            'docker', 'exec', '-e', 'DISPLAY=:99', repro.CONTAINER,
            'x11vnc', '-sync', '-R', 'noviewonly',
        ], required=False)
        if result.returncode == 0:
            query = command([
                'docker', 'exec', '-e', 'DISPLAY=:99', repro.CONTAINER,
                'x11vnc', '-Q', 'viewonly',
            ], required=False)
            if 'ans=viewonly:0' in query.stdout + query.stderr:
                report(vnc_input_enabled=True)
                break
        time.sleep(2)
    else:
        raise RuntimeError('VNCInputNotVerified')
    request = urllib.request.Request(
        'http://127.0.0.1:6174/api/status/auth',
        headers={'Authorization': 'Bearer ' + token.read_text().strip()},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=10) as response:
        auth = json.load(response)
    status = auth.get('status')
    report(api_ready=True,
           auth_status=status if status in {'logged_in', 'logged_out'} else 'unknown',
           login_user_present=bool(auth.get('loggedInUser')))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wait', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('SudoRequired')
    try:
        if args.wait:
            wait_for_image()
        remove_proxy()
        restore()
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        if not pull_pids():
            remove_proxy()
        report(ok=False,
               error=str(error) if isinstance(error, RuntimeError) else type(error).__name__)
        raise SystemExit(2) from None


if __name__ == '__main__':
    main()
