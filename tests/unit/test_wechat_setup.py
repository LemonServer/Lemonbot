from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from lemonbot.connectors.wechat_agent_state import private_read, private_write
from lemonbot.research.agent_wechat_lab import SimulatedWechat

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
_spec = importlib.util.spec_from_file_location(
    "wechat_setup", Path(__file__).parents[2] / "scripts/wechat_setup.py"
)
assert _spec and _spec.loader
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)


def args(tmp_path: Path) -> argparse.Namespace:
    state = tmp_path / "different user" / "state"
    state.mkdir(parents=True, mode=0o700)
    token = state / "token"
    private_write(token, b"a" * 64)
    return argparse.Namespace(state_dir=state, token_file=token, sudo=False, proxy=None)


def container(token: Path) -> dict:
    return {
        "Config": {"Image": setup.IMAGE},
        "State": {"Running": True},
        "HostConfig": {
            "CapAdd": ["CAP_SYS_PTRACE"],
            "SecurityOpt": ["seccomp=unconfined"],
            "RestartPolicy": {"Name": "no"},
            "PortBindings": {"6174/tcp": [{"HostIp": "127.0.0.1", "HostPort": "6174"}]},
        },
        "Mounts": [
            {"Type": "volume", "Name": setup.VOLUMES[0], "Destination": "/data", "RW": True},
            {"Type": "volume", "Name": setup.VOLUMES[1], "Destination": "/home/wechat", "RW": True},
            {"Type": "bind", "Source": str(token), "Destination": "/data/auth-token", "RW": False},
        ],
        "NetworkSettings": {
            "Networks": {"bridge": {"IPAddress": "172.17.0.2", "Gateway": "172.17.0.1"}}
        },
    }


@pytest.mark.parametrize("change", ["image", "mount", "public", "privileged", "cap", "restart"])
def test_existing_environment_rejects_unsafe_reuse(tmp_path: Path, change: str) -> None:
    token = args(tmp_path).token_file
    value = container(token)
    if change == "image":
        value["Config"]["Image"] = "upstream:latest"
    elif change == "mount":
        value["Mounts"][2]["Source"] = str(tmp_path / "wrong-token")
    elif change == "public":
        value["HostConfig"]["PortBindings"]["6174/tcp"][0]["HostIp"] = "0.0.0.0"  # noqa: S104 -- rejection fixture
    elif change == "privileged":
        value["HostConfig"]["Privileged"] = True
    elif change == "cap":
        value["HostConfig"]["CapAdd"] += ["NET_ADMIN"]
    else:
        value["HostConfig"]["RestartPolicy"]["Name"] = "always"
    with pytest.raises(ValueError):
        setup.validate_container(value, token)


async def test_first_bind_generates_private_disabled_config_and_preserves_repeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = args(tmp_path)
    mock = SimulatedWechat()
    mock.append("initialization text", is_self=True)
    monkeypatch.setattr(setup, "AgentWechatHTTP", lambda _token: mock.http())
    monkeypatch.setattr(setup, "backend", lambda _args: None)
    monkeypatch.setattr(setup, "inspect", lambda _args: container(options.token_file))
    synced = AsyncMock()
    monkeypatch.setattr(setup, "sync_whitelist", synced)
    await setup.bind_account(options)
    from lemonbot.config.settings import load_settings

    config = load_settings(options.state_dir / "conversation-runtime.toml")
    assert config.wechat_agent.state_dir == str(options.state_dir)
    assert config.wechat_agent.token_file == str(options.token_file)
    assert not config.wechat_agent.enabled
    assert config.runtime.connector == "fake"
    assert (
        json.loads(private_read(options.state_dir / "metadata-binding.json"))["self_identity"]
        == mock.account
    )
    assert synced.await_count == 1
    private_write(options.state_dir / "whitelist.toml", b'version=1\ngroups=["testing"]\n')
    before = private_read(options.state_dir / "conversation-runtime.toml")
    await setup.bind_account(options)
    assert private_read(options.state_dir / "conversation-runtime.toml") == before
    assert mock.posts == 0


async def test_bind_requires_self_evidence_and_creates_no_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = args(tmp_path)
    mock = SimulatedWechat()
    monkeypatch.setattr(setup, "AgentWechatHTTP", lambda _token: mock.http())
    with pytest.raises(ValueError, match="CanonicalSelfAmbiguous"):
        await setup.bind_account(options)
    assert not (options.state_dir / "binding.json").exists()
    assert mock.posts == 0


def test_user_service_paths_and_no_boot_enable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup, "REPO", tmp_path / "other user/repo%$name")
    text = setup.service(
        [str(tmp_path / "other user/python"), "-m", "lemonbot", "--config", str(tmp_path / "a%b$c")]
    )
    assert "Requires=lemonbot-wechat-metadata.service" in text
    assert "repo%%$name" in text
    assert "a%%b$$c" in text
    assert str(tmp_path / "other user/python") in text
    assert "UMask=0077" in text
    assert "WantedBy=default.target" in text
    with pytest.raises(ValueError, match="UnsafeServicePath"):
        setup.unit_arg("bad\npath")


def test_environment_reuses_container_without_pull_or_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = args(tmp_path)
    monkeypatch.setattr(setup, "inspect", lambda _args: container(options.token_file))
    calls = []

    def docker(_args: argparse.Namespace, *argv: str, **_kwargs: object) -> str:
        calls.append(argv)
        if argv[0] == "info":
            return str(tmp_path)
        if "viewonly" in argv[-1] and "-Q" in argv[-1]:
            return "ans=0 viewonly:0"
        return ""

    monkeypatch.setattr(setup, "docker", docker)
    setup.environment(options)
    assert not any(c[0] in {"pull", "run", "rm", "stop", "volume"} for c in calls)


def test_persistent_service_install_is_idempotent_and_does_not_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = args(tmp_path)
    text = (setup.REPO / "config/wechat-agent-conversation.example.toml").read_text()
    text = text.replace(
        'state_dir = "~/.local/state/lemonbot/agent-wechat-lab"',
        "state_dir = " + json.dumps(str(options.state_dir)),
    )
    text = text.replace(
        'token_file = "~/.local/state/lemonbot/agent-wechat-reproduction/api-token"',
        "token_file = " + json.dumps(str(options.token_file)),
    )
    private_write(options.state_dir / "conversation-runtime.toml", text.encode())
    monkeypatch.setattr(setup, "inspect", lambda _args: container(options.token_file))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(setup, "command", lambda *argv: calls.append(argv))
    setup.install_services(options)
    target = tmp_path / ".config/systemd/user" / setup.BOT_UNIT
    before = target.read_bytes()
    setup.install_services(options)
    assert target.read_bytes() == before
    assert all(c == ("systemctl", "--user", "daemon-reload") for c in calls)
    assert target.stat().st_mode & 0o077 == 0
    assert str(options.state_dir / "conversation-runtime.toml") in target.read_text()


async def test_doctor_missing_secret_service_reports_failure_without_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    options = args(tmp_path)
    from lemonbot.security import secrets as credential_module

    def unavailable() -> None:
        raise RuntimeError("synthetic-private-data-never-print")

    monkeypatch.setattr(credential_module, "platform_secret_store", unavailable)
    monkeypatch.setattr(setup, "inspect", lambda _args: {})
    assert not await setup.doctor(options)
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert any(r["check"] == "secret_store" and not r["ok"] for r in rows)
    assert "synthetic-private-data" not in json.dumps(rows)


def test_generated_units_pass_real_systemd_syntax_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil
    import subprocess

    if not shutil.which("systemd-analyze"):
        pytest.skip("systemd syntax checker not installed")
    repo = tmp_path / "another user %repo"
    repo.mkdir()
    monkeypatch.setattr(setup, "REPO", repo)
    bridge = tmp_path / setup.BRIDGE_UNIT
    bot = tmp_path / setup.BOT_UNIT
    bridge.write_text(setup.service([sys.executable, "-c", "pass"], bridge=True))
    bot.write_text(setup.service([sys.executable, "-c", "pass"]))
    result = subprocess.run(  # noqa: S603 -- local syntax check only
        [
            shutil.which("systemd-analyze") or "/usr/bin/systemd-analyze",
            "verify",
            str(bridge),
            str(bot),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "bad unit" not in result.stderr.lower()


@pytest.mark.parametrize("proxy_matches", [True, False])
def test_first_environment_pins_image_and_checks_explicit_daemon_proxy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    proxy_matches: bool,
) -> None:
    options = args(tmp_path)
    options.proxy = "http://127.0.0.1:12345"
    monkeypatch.setattr(setup, "inspect", lambda _args: {})
    monkeypatch.setattr(setup, "disk_free", lambda *_args: 9 * 1024**3)
    calls = []
    pulls = []
    monkeypatch.setattr(setup, "pull_image", lambda *_args: pulls.append(setup.IMAGE))

    def docker(_args: argparse.Namespace, *argv: str, **_kwargs: object) -> str:
        calls.append(argv)
        if argv[0] == "info":
            if argv[-1] == "{{.HTTPSProxy}}":
                return options.proxy if proxy_matches else ""
            return str(tmp_path)
        if argv[:2] == ("volume", "inspect") or argv[:2] == ("image", "inspect"):
            return ""
        if "x11vnc -Q" in argv[-1]:
            return "ans=viewonly:0"
        return ""

    monkeypatch.setattr(setup, "docker", docker)
    if not proxy_matches:
        with pytest.raises(ValueError, match="ConfigureDaemonProxyExplicitlyFirst"):
            setup.environment(options)
        assert not pulls
        assert not any(c[0] == "run" for c in calls)
    else:
        setup.environment(options)
        assert pulls == [setup.IMAGE]
        created = next(c for c in calls if c[0] == "run")
        assert created[-1] == setup.IMAGE
        assert "127.0.0.1:6174:6174" in created
        assert "--privileged" not in created
        assert any("dst=/data/auth-token,readonly" in c for c in created)
        assert not any("latest" in c for c in created)


def test_low_disk_aborts_before_environment_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = args(tmp_path)
    calls = []
    monkeypatch.setattr(
        setup, "docker", lambda *_args, **_kwargs: calls.append(_args) or str(tmp_path)
    )
    monkeypatch.setattr(setup, "disk_free", lambda *_args: 2 * 1024**3)
    with pytest.raises(ValueError, match="LessThan3GiBFree"):
        setup.environment(options)
    assert len(calls) == 1
    assert calls[0][1] == "info"
