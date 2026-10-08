#!/usr/bin/env python3
"""One background recovery attempt with sanitized, durable progress."""
from __future__ import annotations

import json
import os
import pwd
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import agent_wechat_fixed_runtime as fixed

child: subprocess.Popen[str] | None = None


def progress(phase: str, **fields: Any) -> None:
    user = pwd.getpwnam('lemon')
    state = Path(user.pw_dir) / '.local/state/lemonbot/agent-wechat-reproduction'
    path = state / 'recovery-progress.json'
    temp = state / 'recovery-progress.json.tmp'
    data = {'phase': phase, 'pid': os.getpid(), 'updated_unix': int(time.time()), **fields}
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.chown(temp, user.pw_uid, user.pw_gid)
    os.replace(temp, path)
    print(json.dumps(data), flush=True)


def stop(_signum: int, _frame: Any) -> None:
    if child is not None and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
    try:
        fixed.remove_proxy()
    finally:
        progress('interrupted', messages_sent=False)
    raise SystemExit(2)


def download_error_flags(error: str) -> dict[str, bool]:
    # Persist diagnostic classes only; stderr can contain signed registry URLs.
    value = error.lower()
    return {
        'tls_timeout': 'tls handshake timeout' in value,
        'missing_snapshot': 'snapshot' in value
        and ('not found' in value or 'does not exist' in value),
        'missing_lease': 'lease' in value and ('not found' in value or 'does not exist' in value),
        'disk_full': 'no space left' in value,
        'connection_reset': 'connection reset' in value,
        'unexpected_eof': 'unexpected eof' in value,
    }


def main() -> None:
    global child
    if os.geteuid() != 0:
        raise SystemExit('SudoRequired')
    if fixed.pull_pids():
        raise SystemExit('DownloadAlreadyRunning')
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        progress('downloading', fixed_image=True)
        child = subprocess.Popen(  # noqa: S603
            ['/usr/bin/docker', 'pull', '--quiet', fixed.IMAGE],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        while child.poll() is None:
            try:
                _, error = child.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                if fixed.shutil.disk_usage('/').free < 3 * 1024**3:
                    child.terminate()
                    child.communicate(timeout=15)
                    raise RuntimeError('DiskSpaceProtection') from None
                ingest = Path('/var/lib/containerd/io.containerd.content.v1.content/ingest')
                size = sum(p.stat().st_size for p in ingest.rglob('data') if p.is_file())
                progress('downloading', partial_layer_bytes=size,
                         free_gib=round(fixed.shutil.disk_usage('/').free / 1024**3, 2))
                continue
            break
        _, error = child.communicate()
        if child.returncode != 0:
            fixed.remove_proxy()
            progress('download_failed', exit_code=child.returncode,
                     **download_error_flags(error),
                     error_details_suppressed=True, messages_sent=False)
            raise SystemExit(2)
        progress('restoring')
        fixed.remove_proxy()
        fixed.restore()
        progress('runtime_ready', messages_sent=False, requires_login_verification=True)
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        if child is not None and child.poll() is None:
            child.terminate()
            child.communicate(timeout=15)
        fixed.remove_proxy()
        progress('recovery_failed', error=type(error).__name__, messages_sent=False)
        raise SystemExit(2) from None


if __name__ == '__main__':
    main()
