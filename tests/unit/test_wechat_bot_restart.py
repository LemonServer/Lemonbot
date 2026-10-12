from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "wechat_bot_restart", Path(__file__).parents[2] / "scripts/wechat_bot_restart.py"
)
assert _spec and _spec.loader
restart = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(restart)


def test_startup_report_is_scoped_to_current_uid_unit_and_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def command(*args: str, **kwargs: bool) -> str:
        calls.append((args, kwargs))
        return 'wechat_startup {"summary": true}\n'

    monkeypatch.setattr(restart.os, "getuid", lambda: 1234)
    monkeypatch.setattr(restart, "command", command)
    invocation = "a" * 32
    assert '"summary": true' in restart.startup_report(invocation)
    argv, kwargs = calls[0]
    assert "_UID=1234" in argv
    assert "_SYSTEMD_USER_UNIT=lemonbot-wechat-conversation.service" in argv
    assert "_SYSTEMD_INVOCATION_ID=" + invocation in argv
    assert kwargs["empty_allowed"]
