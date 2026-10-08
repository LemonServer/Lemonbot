from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture
def job(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'scripts'))
    module = importlib.import_module('agent_wechat_recovery_job')
    monkeypatch.setattr(module.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(module.fixed, 'pull_pids', lambda: [])
    monkeypatch.setattr(module.signal, 'signal', lambda *args: None)
    return module


class FinishedPull:
    def __init__(self, code: int) -> None:
        self.returncode = code

    def poll(self) -> int:
        return self.returncode

    def communicate(self, timeout: int | None = None) -> tuple[str, str]:
        return '', 'private signed url signature TLS handshake timeout'


def test_immediate_download_failure_cleans_proxy_without_leaking_error(
    job: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = []
    cleanup = []
    monkeypatch.setattr(job, 'progress', lambda phase, **fields: records.append((phase, fields)))
    monkeypatch.setattr(job.subprocess, 'Popen', lambda *args, **kwargs: FinishedPull(1))
    monkeypatch.setattr(job.fixed, 'remove_proxy', lambda: cleanup.append(True))
    with pytest.raises(SystemExit):
        job.main()
    assert len(cleanup) == 1
    assert records[-1][0] == 'download_failed'
    assert records[-1][1]['tls_timeout']
    assert 'signature' not in str(records)
    assert not records[-1][1]['messages_sent']


def test_immediate_success_restores_before_reporting_ready(
    job: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = []
    actions = []
    monkeypatch.setattr(job, 'progress', lambda phase, **fields: records.append((phase, fields)))
    monkeypatch.setattr(job.subprocess, 'Popen', lambda *args, **kwargs: FinishedPull(0))
    monkeypatch.setattr(job.fixed, 'remove_proxy', lambda: actions.append('cleanup'))
    monkeypatch.setattr(job.fixed, 'restore', lambda: actions.append('restore'))
    job.main()
    assert actions == ['cleanup', 'restore']
    assert [r[0] for r in records] == ['downloading', 'restoring', 'runtime_ready']
    assert records[-1][1]['requires_login_verification']
    assert not records[-1][1]['messages_sent']


def test_existing_pull_is_never_duplicated(
    job: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(job.fixed, 'pull_pids', lambda: [1234])

    def unexpected(*args: Any, **kwargs: Any) -> None:
        raise AssertionError('duplicate download started')

    monkeypatch.setattr(job.subprocess, 'Popen', unexpected)
    with pytest.raises(SystemExit, match='DownloadAlreadyRunning'):
        job.main()


def test_expired_lease_failure_keeps_only_diagnostic_classes(job: ModuleType) -> None:
    flags = job.download_error_flags(
        "snapshot extract-private-hash does not exist: not found; "
        "lease private-id: not found; https://private.example/blob?token=private-value"
    )
    assert flags["missing_snapshot"] and flags["missing_lease"]
    assert not flags["tls_timeout"] and not flags["disk_full"]
    assert "private" not in str(flags)
