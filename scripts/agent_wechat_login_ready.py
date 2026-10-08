#!/usr/bin/env python3
"""Open the fixed runtime login viewer once recovery succeeds; never send messages."""
from __future__ import annotations

import json
import os
import stat
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

import agent_wechat_repro as repro


def read_progress(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 65536):
            raise RuntimeError('UnsafeRecoveryProgress')
        value = json.load(stream)
    if not isinstance(value, dict):
        raise RuntimeError('InvalidRecoveryProgress')
    return value


def initialize_login() -> None:
    # The recovery worker verifies the image, original mounts and localhost binding
    # before it writes runtime_ready. Use the historical token, never the candidate's.
    urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
    auth = repro.api('GET', '/api/status/auth')
    chats = repro.api('GET', '/api/chats?limit=1&offset=0')
    token = urllib.parse.quote(repro.auth_token(), safe='')
    opened = webbrowser.open('http://127.0.0.1:6174/vnc/?token=' + token + '&autoconnect=true')
    print(json.dumps({'viewer_opened': opened, 'messages_sent': False}), flush=True)
    if (auth.get('status') == 'logged_in' and auth.get('loggedInUser')
            and isinstance(chats, list) and chats):
        events = {}
    else:
        events = repro.run_login_websocket(120)
    auth = repro.api('GET', '/api/status/auth')
    chats = repro.api('GET', '/api/chats?limit=1&offset=0')
    print(json.dumps({
        'login_initialized_at_unix': int(time.time()), 'event_counts': events,
        'logged_in': auth.get('status') == 'logged_in',
        'login_user_present': bool(auth.get('loggedInUser')),
        'chat_list_nonempty': isinstance(chats, list) and bool(chats),
        'messages_sent': False,
    }), flush=True)


def main() -> None:
    if os.getuid() == 0:
        raise SystemExit('RunAsDesktopUser')
    deadline = time.monotonic() + 8 * 3600
    path = repro.STATE / 'recovery-progress.json'
    while time.monotonic() < deadline:
        data = read_progress(path)
        if data.get('phase') == 'runtime_ready':
            initialize_login()
            return
        if data.get('phase') in {'download_failed', 'recovery_failed', 'interrupted'}:
            raise RuntimeError('RecoveryNotReady')
        time.sleep(10)
    raise RuntimeError('RecoveryWaitExpired')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, AttributeError):
        print(json.dumps({'ok': False, 'phase': 'viewer_not_ready', 'messages_sent': False}))
        raise SystemExit(2) from None
