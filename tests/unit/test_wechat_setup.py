from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from contextlib import asynccontextmanager
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
sys.modules[_spec.name] = setup
_spec.loader.exec_module(setup)


def image(env: setup.FixedEnvironment = setup.AMD64) -> dict:
    return {
        "Id": "sha256:synthetic-" + env.architecture,
        "Architecture": env.architecture,
        "Os": "linux",
        "RepoDigests": [env.image],
    }


@pytest.fixture(autouse=True)
def supported_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(setup.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(setup.platform, "system", lambda: "Linux")

    def docker(_args: argparse.Namespace, *argv: str, **_kwargs: object) -> str:
        assert argv == ("image", "inspect", setup.AMD64.image)
        return json.dumps([image()])

    monkeypatch.setattr(setup, "docker", docker)
    assets = tmp_path / "synthetic novnc assets"
    assets.mkdir()
    monkeypatch.setattr(setup, "novnc_assets", lambda: assets)


def args(tmp_path: Path) -> argparse.Namespace:
    state = tmp_path / "different user" / "state"
    state.mkdir(parents=True, mode=0o700)
    (tmp_path / "checkout/config").mkdir(parents=True)
    token = state / "token"
    private_write(token, b"a" * 64)
    return argparse.Namespace(
        state_dir=state,
        token_file=token,
        config=tmp_path / "checkout/config/lemonbot.toml",
        sudo=False,
        proxy=None,
        experimental_arm64=False,
    )


def container(token: Path, env: setup.FixedEnvironment = setup.AMD64) -> dict:
    return {
        "Config": {"Image": env.image},
        "Image": image(env)["Id"],
        "State": {"Running": True},
        "HostConfig": {
            "CapAdd": ["CAP_SYS_PTRACE"],
            "SecurityOpt": ["seccomp=unconfined"],
            "RestartPolicy": {"Name": "no"},
            "PortBindings": {"6174/tcp": [{"HostIp": "127.0.0.1", "HostPort": "6174"}]},
        },
        "Mounts": [
            {"Type": "volume", "Name": env.volumes[0], "Destination": "/data", "RW": True},
            {"Type": "volume", "Name": env.volumes[1], "Destination": "/home/wechat", "RW": True},
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


@pytest.mark.parametrize("env", [setup.AMD64, setup.ARM64])
@pytest.mark.parametrize("existing_whitelist", [False, True])
async def test_first_bind_generates_private_disabled_config_and_preserves_repeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env: setup.FixedEnvironment,
    existing_whitelist: bool,
) -> None:
    options = args(tmp_path)
    options.experimental_arm64 = env.experimental
    monkeypatch.setattr(setup.platform, "machine", lambda: env.machine)
    monkeypatch.setattr(setup, "docker", lambda *_args, **_kwargs: json.dumps([image(env)]))
    mock = SimulatedWechat()
    mock.append("initialization text", is_self=True)
    monkeypatch.setattr(setup, "AgentWechatHTTP", lambda _token: mock.http())
    monkeypatch.setattr(setup, "backend", lambda _args: None)
    monkeypatch.setattr(setup, "inspect", lambda _args: container(options.token_file, env))
    synced = AsyncMock()
    monkeypatch.setattr(setup, "sync_whitelist", synced)
    whitelist = options.config.parent / "wechat-whitelist.local.toml"
    if existing_whitelist:
        private_write(
            whitelist, b'version=1\ngroups=["testing", "Second group"]\ncontacts=["Peer"]\n'
        )
    private_write(
        options.state_dir / "whitelist.toml", b'version=1\ngroups=["Old ignored group"]\n'
    )
    await setup.bind_account(options)
    from lemonbot.config.settings import load_settings

    config = load_settings(options.config)
    assert config.wechat_agent.state_dir == str(options.state_dir)
    assert config.wechat_agent.token_file == str(options.token_file)
    assert not config.wechat_agent.enabled
    assert config.runtime.connector == "fake"
    assert config.models.provider == "disabled"
    assert not config.wechat_agent.memory_enabled
    assert config.wechat_agent.whitelist_file == str(whitelist)
    assert synced.call_args.args[4].groups == (
        ("testing", "Second group") if existing_whitelist else ("testing",)
    )
    if env.experimental:
        assert config.runtime.data_root == str(Path.home() / ".local/share/Lemonbot-wechat-orin")
    assert (
        json.loads(private_read(options.state_dir / "metadata-binding.json"))["self_identity"]
        == mock.account
    )
    assert synced.await_count == 1
    private_write(
        options.config.parent / "wechat-whitelist.local.toml", b'version=1\ngroups=["testing"]\n'
    )
    before = private_read(options.config)
    await setup.bind_account(options)
    assert private_read(options.config) == before
    assert mock.posts == 0


@pytest.mark.parametrize("env", [setup.AMD64, setup.ARM64])
def test_setup_resolves_config_and_preserves_private_state_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: setup.FixedEnvironment
) -> None:
    options = args(tmp_path)
    options.command = "install-services"
    options.experimental_arm64 = env.experimental
    monkeypatch.setattr(setup.platform, "machine", lambda: env.machine)
    original_config = options.config
    text = (setup.REPO / "config/wechat-agent-conversation.example.toml").read_text()
    text = text.replace(
        'state_dir = "~/.local/state/lemonbot/agent-wechat-lab"',
        "state_dir = " + json.dumps(str(options.state_dir)),
    ).replace(
        'token_file = "~/.local/state/lemonbot/agent-wechat-reproduction/api-token"',
        "token_file = " + json.dumps(str(options.token_file)),
    )
    private_write(original_config, text.encode())
    original_state, original_token = options.state_dir, options.token_file
    monkeypatch.setenv("LEMONBOT_CONFIG", str(original_config))
    monkeypatch.chdir(tmp_path)
    options.config = options.state_dir = options.token_file = None
    setup.configure_paths(options)
    assert options.config == original_config
    assert options.state_dir == original_state
    assert options.token_file == original_token
    monkeypatch.setenv("LEMONBOT_CONFIG", str(tmp_path / "ignored.toml"))
    setup.configure_paths(options)
    assert options.config == original_config
    options.state_dir = tmp_path / "conflicting-state"
    with pytest.raises(ValueError, match="ConfigStateDirectoryMismatch"):
        setup.configure_paths(options)


@pytest.mark.parametrize("operation", ["doctor", "install-services"])
def test_missing_checkout_config_rejects_legacy_implicit_fallback(
    tmp_path: Path, operation: str
) -> None:
    options = args(tmp_path)
    options.command = operation
    private_write(options.state_dir / "conversation-runtime.toml", b'profile="lab"\n')
    with pytest.raises(ValueError, match="RuntimeConfigMissing"):
        setup.configure_paths(options)


async def test_bind_requires_self_evidence_and_creates_no_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = args(tmp_path)
    mock = SimulatedWechat()
    monkeypatch.setattr(setup, "inspect", lambda _args: container(options.token_file))
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
        if argv[:2] == ("image", "inspect"):
            return json.dumps([image()])
        if argv[0] == "info":
            return str(tmp_path)
        if "viewonly" in argv[-1] and "-Q" in argv[-1]:
            return "ans=0 viewonly:0"
        return ""

    monkeypatch.setattr(setup, "docker", docker)
    setup.environment(options)
    assert not any(c[0] in {"pull", "run", "rm", "stop", "volume"} for c in calls)


@pytest.mark.parametrize("env", [setup.AMD64, setup.ARM64])
def test_persistent_service_install_is_idempotent_and_does_not_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env: setup.FixedEnvironment,
) -> None:
    options = args(tmp_path)
    options.experimental_arm64 = env.experimental
    monkeypatch.setattr(setup.platform, "machine", lambda: env.machine)
    monkeypatch.setattr(setup, "docker", lambda *_args, **_kwargs: json.dumps([image(env)]))
    text = (setup.REPO / "config/wechat-agent-conversation.example.toml").read_text()
    text = text.replace(
        'state_dir = "~/.local/state/lemonbot/agent-wechat-lab"',
        "state_dir = " + json.dumps(str(options.state_dir)),
    )
    text = text.replace(
        'token_file = "~/.local/state/lemonbot/agent-wechat-reproduction/api-token"',
        "token_file = " + json.dumps(str(options.token_file)),
    )
    private_write(options.config, text.encode())
    monkeypatch.setattr(setup, "inspect", lambda _args: container(options.token_file, env))
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
    assert str(options.config) in target.read_text()


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
        if argv[:2] == ("image", "inspect"):
            return "" if _kwargs.get("optional") else json.dumps([image()])
        if argv[:2] == ("volume", "inspect"):
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


@pytest.mark.parametrize(
    ("machine", "reason"),
    [("aarch64", "ExperimentalARM64OptInRequired"), ("armv7l", "UnsupportedHostArchitecture")],
)
def test_unsupported_architecture_rejected_before_token_or_docker_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    machine: str,
    reason: str,
) -> None:
    options = args(tmp_path)
    options.token_file.unlink()
    monkeypatch.setattr(setup.platform, "machine", lambda: machine)
    calls = []
    monkeypatch.setattr(setup, "docker", lambda *argv, **_kwargs: calls.append(argv) or "")
    with pytest.raises(ValueError, match=reason):
        setup.environment(options)
    assert not options.token_file.exists()
    assert not calls


@pytest.mark.parametrize("env", [setup.AMD64, setup.ARM64])
def test_native_profiles_use_distinct_state_and_volumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: setup.FixedEnvironment
) -> None:
    options = args(tmp_path)
    options.experimental_arm64 = True
    monkeypatch.setattr(setup.platform, "machine", lambda: env.machine)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "actual desktop user")
    assert setup.selected_environment(options) == env
    state, token = setup.default_paths(env)
    assert state.is_absolute() and token.is_absolute()
    assert str(state).startswith(str(Path.home()))
    assert setup.default_paths(setup.AMD64) != setup.default_paths(setup.ARM64)
    assert set(setup.AMD64.volumes).isdisjoint(setup.ARM64.volumes)
    from lemonbot.config import paths
    from lemonbot.config.settings import resolve_config_path

    assert paths.default_wechat_runtime_config() == resolve_config_path()


@pytest.mark.parametrize("env", [setup.AMD64, setup.ARM64])
@pytest.mark.parametrize("fault", ["architecture", "os", "digest", "container_image"])
async def test_every_binding_and_service_entry_rejects_mismatched_image_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env: setup.FixedEnvironment,
    fault: str,
) -> None:
    options = args(tmp_path)
    options.experimental_arm64 = env.experimental
    monkeypatch.setattr(setup.platform, "machine", lambda: env.machine)
    value, instance = image(env), container(options.token_file, env)
    reason = "ImageArchitectureMismatch"
    if fault == "architecture":
        value["Architecture"] = "arm64" if env == setup.AMD64 else "amd64"
    elif fault == "os":
        value["Os"] = "windows"
    elif fault == "digest":
        value["RepoDigests"] = ["upstream:latest"]
        reason = "PinnedImageRequired"
    else:
        instance["Image"] = "sha256:another-image"
        reason = "ContainerImageMismatch"
    monkeypatch.setattr(setup, "docker", lambda *_args, **_kwargs: json.dumps([value]))
    monkeypatch.setattr(setup, "inspect", lambda _args: instance)
    http = AsyncMock()
    monkeypatch.setattr(setup, "AgentWechatHTTP", http)
    with pytest.raises(ValueError, match=reason):
        await setup.bind_account(options)
    http.assert_not_called()
    config = (setup.REPO / "config/wechat-agent-conversation.example.toml").read_text()
    config = config.replace(
        'state_dir = "~/.local/state/lemonbot/agent-wechat-lab"',
        "state_dir = " + json.dumps(str(options.state_dir)),
    ).replace(
        'token_file = "~/.local/state/lemonbot/agent-wechat-reproduction/api-token"',
        "token_file = " + json.dumps(str(options.token_file)),
    )
    private_write(options.config, config.encode())
    commands = []
    monkeypatch.setattr(setup, "command", lambda *argv: commands.append(argv) or "")
    with pytest.raises(ValueError, match=reason):
        setup.install_services(options)
    assert not commands
    with pytest.raises(ValueError, match=reason):
        setup.open_vnc(options)
    with pytest.raises(ValueError, match=reason):
        setup.backend(options)
    assert not (options.state_dir / "binding.json").exists()


def test_arm64_container_rejects_legacy_volumes(tmp_path: Path) -> None:
    options = args(tmp_path)
    value = container(options.token_file, setup.ARM64)
    value["Mounts"][0]["Name"] = setup.AMD64.volumes[0]
    with pytest.raises(ValueError, match="UnexpectedMounts"):
        setup.validate_container(value, options.token_file, setup.ARM64)


async def test_login_initialization_never_repairs_existing_missing_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = args(tmp_path)
    anchor = options.state_dir / "binding.json"
    private_write(anchor, b"synthetic-existing-anchor")
    http = AsyncMock()
    http.request.return_value = {"status": "logged_in", "loggedInUser": None}
    calls = []
    monkeypatch.setattr(setup, "websocket_connect", lambda *a, **kw: calls.append((a, kw)))
    with pytest.raises(setup.LabPaused, match="AccountIdentityMissing"):
        await setup.initialize_login(options, http)
    assert not calls
    assert private_read(anchor) == b"synthetic-existing-anchor"


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        ("login_success", None),
        ("qr", "ManualLoginRequired"),
        ("error", "LoginInitializationFailed"),
        ("login_timeout", "LoginInitializationFailed"),
    ],
)
async def test_login_initialization_uses_candidate_protocol_and_requires_real_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: str, reason: str | None
) -> None:
    options = args(tmp_path)
    http = AsyncMock()
    http.request.side_effect = [
        {"status": "logged_in", "loggedInUser": None},
        {"status": "logged_in", "loggedInUser": "synthetic-account"},
    ]
    calls = []

    async def messages():
        yield json.dumps({"type": event})

    @asynccontextmanager
    async def socket(*argv: str, **kwargs: object):
        calls.append((argv, kwargs))
        yield messages()

    monkeypatch.setattr(setup, "websocket_connect", socket)
    if reason:
        with pytest.raises(setup.LabPaused, match=reason):
            await setup.initialize_login(options, http)
    else:
        await setup.initialize_login(options, http)
        assert http.request.await_count == 2
    assert len(calls) == 1
    assert "newAccount=false" in calls[0][0][0]
    assert calls[0][1]["proxy"] is None
    assert not (options.state_dir / "binding.json").exists()
