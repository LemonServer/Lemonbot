from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def helper(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'scripts'))
    return importlib.import_module('agent_wechat_fixed_runtime')


def test_proxy_cleanup_refuses_foreign_config(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / 'http-proxy.conf'
    path.write_text('existing user configuration')
    monkeypatch.setattr(helper, 'PROXY_FILE', path)
    with pytest.raises(RuntimeError, match='TemporaryProxyChanged'):
        helper.remove_proxy()
    assert path.read_text() == 'existing user configuration'


def test_proxy_cleanup_refuses_running_containers(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / 'http-proxy.conf'
    path.write_text(helper.PROXY_CONFIG)
    monkeypatch.setattr(helper, 'PROXY_FILE', path)
    monkeypatch.setattr(
        helper, 'command', lambda args: subprocess.CompletedProcess(args, 0, 'running-container'),
    )
    with pytest.raises(RuntimeError, match='RefusingDaemonRestartWithRunningContainers'):
        helper.remove_proxy()
    assert path.exists()


def test_proxy_cleanup_removes_only_own_dropin(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / 'http-proxy.conf'
    path.write_text(helper.PROXY_CONFIG)
    kept = tmp_path / 'api-token'
    kept.write_text('synthetic-token')
    calls = []

    def command(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, '')

    monkeypatch.setattr(helper, 'PROXY_FILE', path)
    monkeypatch.setattr(helper, 'command', command)
    helper.remove_proxy()
    assert not path.exists()
    assert kept.exists()
    assert calls == [
        ['docker', 'ps', '-q'], ['systemctl', 'daemon-reload'], ['systemctl', 'restart', 'docker'],
    ]


def prepare_state(helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / '.local/state/lemonbot/agent-wechat-reproduction'
    state.mkdir(parents=True)
    (state / 'artifact-lock.json').write_text(json.dumps({'image': helper.IMAGE}))
    token = state / 'api-token'
    token.write_text('synthetic-token')
    token.chmod(0o600)
    monkeypatch.setattr(
        helper.pwd, 'getpwnam',
        lambda name: SimpleNamespace(pw_dir=str(tmp_path), pw_uid=os.getuid()),
    )
    return state


def test_restore_refuses_changed_fixed_image(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = prepare_state(helper, tmp_path, monkeypatch)
    (state / 'artifact-lock.json').write_text(json.dumps({'image': 'unexpected:latest'}))
    calls = []
    monkeypatch.setattr(helper, 'command', lambda args: calls.append(args))
    with pytest.raises(RuntimeError, match='ArtifactLockChanged'):
        helper.restore()
    assert not calls


def test_restore_refuses_public_token(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = prepare_state(helper, tmp_path, monkeypatch)
    (state / 'api-token').chmod(0o644)
    with pytest.raises(RuntimeError, match='UnsafePersistentToken'):
        helper.restore()


def test_restore_never_creates_replacement_for_unowned_volume(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_state(helper, tmp_path, monkeypatch)
    calls = []

    def command(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        data = [{'Architecture': 'amd64', 'Os': 'linux'}] if args[1] == 'image' else [{}]
        return subprocess.CompletedProcess(args, 0, json.dumps(data))

    monkeypatch.setattr(helper, 'command', command)
    with pytest.raises(RuntimeError, match='OriginalVolumeIdentityMismatch'):
        helper.restore()
    assert all('create' not in args and 'run' not in args for args in calls)


def test_download_failure_is_not_treated_as_ready(
    helper: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(helper, 'pull_pids', lambda: [])
    monkeypatch.setattr(
        helper, 'command', lambda args, required=False: subprocess.CompletedProcess(args, 1, ''),
    )
    with pytest.raises(RuntimeError, match='FixedImageDownloadIncomplete'):
        helper.wait_for_image()
