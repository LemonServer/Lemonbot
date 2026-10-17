from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from lemonbot.config import RuntimePaths
from lemonbot.config.settings import AppSettings
from lemonbot.connectors.wechat_agent_state import private_write

_spec = importlib.util.spec_from_file_location(
    "wechat_conversation_control",
    Path(__file__).parents[2] / "scripts/wechat_conversation_control.py",
)
assert _spec and _spec.loader
control = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(control)


async def test_admin_session_stays_in_private_state_when_config_is_in_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "checkout/config/lemonbot.toml"
    config.parent.mkdir(parents=True)
    state = tmp_path / "private-state"
    state.mkdir(mode=0o700)
    settings = AppSettings.model_validate(
        {
            "profile": "lab",
            "runtime": {"data_root": str(tmp_path / "data")},
            "wechat_agent": {"state_dir": str(state)},
        }
    )
    paths = RuntimePaths.from_settings(settings)
    paths.ensure()
    private_write(
        paths.lock_file.parent / "lab.admin-url",
        b"http://127.0.0.1:8765/login#synthetic-bootstrap",
    )
    monkeypatch.setattr(control, "load_settings", lambda _path: settings)
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/auth/exchange":
            return httpx.Response(
                200,
                json={"csrf": "synthetic-csrf"},
                headers={"set-cookie": "lemonbot_session=synthetic-session; Path=/"},
            )
        return httpx.Response(
            200,
            json={
                "global_paused": False,
                "channel_pauses": {control.CHANNEL: False},
                "queue_depth": 0,
                "unknown_outbox": 0,
            },
        )

    client = httpx.AsyncClient
    monkeypatch.setattr(
        control.httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs),
    )
    await control.control("status", config)
    cache = state / "conversation-admin-session.json"
    assert json.loads(cache.read_text())["session"] == "synthetic-session"
    assert cache.stat().st_mode & 0o077 == 0
    assert not (config.parent / cache.name).exists()
    await control.control("status", config)
    assert requests.count("/auth/exchange") == 1
