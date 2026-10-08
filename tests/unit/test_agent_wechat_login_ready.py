from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def viewer(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'scripts'))
    return importlib.import_module('agent_wechat_login_ready')


def test_failed_recovery_does_not_open_viewer_or_touch_api(
    viewer: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(viewer.os, 'getuid', lambda: 1000)
    monkeypatch.setattr(viewer, 'read_progress', lambda path: {'phase': 'download_failed'})
    calls = []
    monkeypatch.setattr(viewer, 'initialize_login', lambda: calls.append(True))
    with pytest.raises(RuntimeError, match='RecoveryNotReady'):
        viewer.main()
    assert not calls


def test_waits_for_success_before_initializing_once(
    viewer: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(viewer.os, 'getuid', lambda: 1000)
    phases = iter([{'phase': 'downloading'}, {'phase': 'runtime_ready'}])
    monkeypatch.setattr(viewer, 'read_progress', lambda path: next(phases))
    sleeps = []
    calls = []
    monkeypatch.setattr(viewer.time, 'sleep', sleeps.append)
    monkeypatch.setattr(viewer, 'initialize_login', lambda: calls.append(True))
    viewer.main()
    assert sleeps == [10]
    assert calls == [True]


@pytest.mark.parametrize('already_logged_in', [True, False])
def test_login_uses_only_status_and_login_endpoints_without_logging_token(
    viewer: ModuleType, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], already_logged_in: bool,
) -> None:
    calls = []
    flows = []
    urls = []

    def api(method: str, path: str) -> object:
        calls.append((method, path))
        if path.startswith('/api/chats'):
            return [{'id': 'private-chat'}]
        return {'status': 'logged_in' if already_logged_in else 'logged_out',
                'loggedInUser': 'private-account' if already_logged_in else None}

    monkeypatch.setattr(viewer.repro, 'api', api)
    monkeypatch.setattr(viewer.repro, 'auth_token', lambda: 'private-token')
    monkeypatch.setattr(viewer.repro, 'run_login_websocket',
                        lambda seconds: flows.append(seconds) or {'qr': 1})
    monkeypatch.setattr(viewer.webbrowser, 'open', lambda url: urls.append(url) or True)
    viewer.initialize_login()
    assert flows == ([] if already_logged_in else [120])
    assert len(urls) == 1 and 'token=private-token' in urls[0]
    assert all(method == 'GET' for method, _ in calls)
    assert all(path in {'/api/status/auth', '/api/chats?limit=1&offset=0'} for _, path in calls)
    output = capsys.readouterr().out
    assert 'private-' not in output
    assert all(not json.loads(line)['messages_sent'] for line in output.splitlines())


def test_public_progress_file_is_rejected(viewer: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / 'progress.json'
    path.write_text('{"phase":"runtime_ready"}')
    path.chmod(0o644)
    with pytest.raises(RuntimeError, match='UnsafeRecoveryProgress'):
        viewer.read_progress(path)
